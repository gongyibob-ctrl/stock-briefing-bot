"""
bot.py — 飞书 @ 机器人，长连接监听 + 命令路由

支持命令（在群里 @股民简报 后接）：
- `watchlist` / `自选股` / `列表`   → 列当前自选股
- `板块`                           → 推今日强势板块
- `龙虎榜` / `游资`                → 推昨日游资动作
- `<6位代码>` / `<股票名>`          → 单股完整分析
- `brief` / `完整简报`              → 触发完整 brief（约 5-7 分钟）
- 其他                            → 显示帮助

启动：
    cd stock-briefing && .venv/bin/python bot.py
"""
from __future__ import annotations

import functools
import json
import os
import re
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path

import lark_oapi as lark
from dotenv import load_dotenv
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

# 让 print flush + 全局 socket timeout
socket.setdefaulttimeout(20)
print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
if not APP_ID or not APP_SECRET:
    print("❌ 缺 FEISHU_APP_ID / FEISHU_APP_SECRET，无法启动")
    sys.exit(1)

# 全局飞书 client（reply 用）
_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

# 自选股缓存（每次 reload 文件）
def _load_watchlist():
    return json.loads((ROOT / "watchlist.json").read_text())["watchlist"]


# =================================================
# 推送辅助
# =================================================
def reply_text(message_id: str, text: str) -> None:
    """快速回一段纯文本"""
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .msg_type("text")
            .build()
        ).build()
    resp = _client.im.v1.message.reply(req)
    if not resp.success():
        print(f"⚠️ reply_text 失败: code={resp.code} msg={resp.msg}")


def reply_card(message_id: str, title: str, markdown_body: str) -> None:
    """回一张 interactive card，Markdown 内容"""
    if len(markdown_body) > 4800:
        markdown_body = markdown_body[:4800] + "\n\n... (内容截断)"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": [{"tag": "markdown", "content": markdown_body}],
    }
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .msg_type("interactive")
            .build()
        ).build()
    resp = _client.im.v1.message.reply(req)
    if not resp.success():
        print(f"⚠️ reply_card 失败: code={resp.code} msg={resp.msg}")


# =================================================
# 命令处理
# =================================================
HELP_TEXT = """🤖 股民简报机器人，支持命令：

📋 自选股
  @股民简报 watchlist   — 列当前自选股
  @股民简报 茅台        — 单股分析（也可用代码）
  @股民简报 600519      — 同上

🌐 全市场
  @股民简报 板块        — 今日强势板块 + Top 3
  @股民简报 龙虎榜      — 昨日游资动作
  @股民简报 brief       — 跑完整简报（5-7 分钟）"""


def resolve_stock(text: str) -> tuple[str, str, str] | None:
    """识别用户输入 → (code, name, market)；找不到返回 None"""
    text = text.strip()
    wl = _load_watchlist()
    # 6 位代码
    if re.fullmatch(r"\d{6}", text):
        for s in wl:
            if s["code"] == text:
                return s["code"], s["name"], s.get("market", "a")
        # 不在 watchlist 里但代码有效 → 默认按 A 股
        return text, text, "a"
    # 名字模糊匹配
    for s in wl:
        if s["name"] == text or text in s["name"] or s["name"] in text:
            return s["code"], s["name"], s.get("market", "a")
    return None


def handle_command(text: str, message_id: str, chat_id: str) -> None:
    """命令路由（同步快回 + 重活后台跑）"""
    text = text.strip()
    low = text.lower()

    # watchlist
    if text in ("watchlist", "自选股", "列表", "list"):
        wl = _load_watchlist()
        lines = [f"📋 当前自选股（{len(wl)} 只）："]
        for s in wl:
            lines.append(f"  · {s['name']}（{s['code']}） [{s.get('market','a').upper()}]")
        reply_text(message_id, "\n".join(lines))
        return

    # 板块强势
    if text in ("板块", "强势板块", "sector", "板块强势"):
        reply_text(message_id, "⏳ 拉今日强势板块 + Top 3 中（约 30-60 秒）...")
        threading.Thread(target=_task_sectors, args=(message_id,), daemon=True).start()
        return

    # 龙虎榜
    if text in ("龙虎榜", "游资", "lhb"):
        reply_text(message_id, "⏳ 拉昨日游资动作中（约 30-60 秒）...")
        threading.Thread(target=_task_hot_money, args=(message_id,), daemon=True).start()
        return

    # 完整 brief
    if text in ("brief", "完整简报", "全量"):
        reply_text(message_id, "⏳ 跑完整简报中（约 5-7 分钟，会自动推送到飞书 webhook 群）...")
        threading.Thread(target=_task_full_brief, args=(message_id,), daemon=True).start()
        return

    # 单股
    hit = resolve_stock(text)
    if hit:
        code, name, market = hit
        if market != "a":
            reply_text(message_id, f"⚠️ 暂只支持 A 股深度分析，{name}({code}) 是 {market.upper()}")
            return
        reply_text(message_id, f"⏳ 分析 {name}({code}) 中（约 60-90 秒，含 LLM）...")
        threading.Thread(target=_task_stock_analysis, args=(message_id, code, name), daemon=True).start()
        return

    # 默认帮助
    reply_text(message_id, HELP_TEXT)


# =================================================
# 后台任务（独立线程跑，避免阻塞事件循环）
# =================================================
def _task_sectors(message_id: str):
    try:
        from sector import run_sector_module
        _, md = run_sector_module(top_n_sectors=5, pool_size=6, call_llm=True)
        if not md:
            reply_text(message_id, "⚠️ 板块模块没拉到数据")
            return
        reply_card(message_id, "🔥 今日强势板块", md)
    except Exception as e:
        reply_text(message_id, f"⚠️ 板块拉取失败: {type(e).__name__}: {e}")


def _task_hot_money(message_id: str):
    try:
        from brief import fetch_market_tables, YESTERDAY, layer1_hot_money_summary
        import akshare as ak
        lhb = ak.stock_lhb_detail_daily_sina(date=YESTERDAY)
        hm = layer1_hot_money_summary(lhb, YESTERDAY, top_n=20)
        seats = hm.get("top_seats") or []
        if not seats:
            reply_text(message_id, "⚠️ 龙虎榜无数据（可能昨日非交易日）")
            return
        # 复用 brief 的渲染逻辑
        lines = [f"### 💰 昨日游资动作（{YESTERDAY} Top 20 上榜股聚合）\n"]
        buyers = sorted([s for s in seats if s["净额(万)"] > 0], key=lambda x: -x["净额(万)"])
        sellers = sorted([s for s in seats if s["净额(万)"] < 0], key=lambda x: x["净额(万)"])

        def _fmt(wan):
            return f"{wan/10000:+.2f} 亿" if abs(wan) >= 10000 else f"{wan:+.0f} 万"

        def _nick(name, tag):
            return tag or (name[:18] + "…" if len(name) > 18 else name)

        if buyers:
            lines.append("**🟢 净买入 Top**")
            for s in buyers[:7]:
                scope = f"命中 {s['命中股票数']} 只（{', '.join(s['命中股票'][:3])}）"
                lines.append(f"- {_nick(s['席位'], s['标签'])} **{_fmt(s['净额(万)'])}** · {scope}")
            lines.append("")
        if sellers:
            lines.append("**🔴 净卖出 Top**")
            for s in sellers[:5]:
                lines.append(f"- {_nick(s['席位'], s['标签'])} **{_fmt(s['净额(万)'])}** · 命中 {s['命中股票数']} 只")
        reply_card(message_id, "💰 昨日游资动作", "\n".join(lines))
    except Exception as e:
        reply_text(message_id, f"⚠️ 龙虎榜拉取失败: {type(e).__name__}: {e}")


def _task_stock_analysis(message_id: str, code: str, name: str):
    try:
        from brief import (
            fetch_market_tables, gather_a, llm_summarize, render_markdown,
        )
        # 单股分析需要 Layer 1 公共表（用于 lookup 龙虎榜命中 / 大宗 / 融资融券）
        market = fetch_market_tables()
        payload = gather_a(code, market)
        summary = llm_summarize(name, code, payload)
        # 包装成 markdown
        k = payload.get("K线技术") or {}
        h = payload.get("北向持股") or {}
        head = [f"### {name}（{code}）\n"]
        if k:
            head.append(f"**今日**：¥{k.get('收盘')} ({k.get('当日涨跌幅%')}%) | 5 日 {k.get('5日累计涨跌幅%')}% | MA: {k.get('均线排列')} | MACD: {k.get('MACD金叉死叉')} (柱 {k.get('MACD柱')})\n")
        if h:
            head.append(f"**北向**：占 A 股 {h.get('持股占A股%')}% | 7 日累计 **{h.get('7日累计增持(亿元)')} 亿**\n")
        body = "\n".join(head) + "\n---\n" + summary
        reply_card(message_id, f"📊 {name} 分析 · {datetime.now().strftime('%H:%M')}", body)
    except Exception as e:
        reply_text(message_id, f"⚠️ 分析 {name}({code}) 失败: {type(e).__name__}: {e}")


def _task_full_brief(message_id: str):
    try:
        import subprocess
        subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "python"), "-u", "brief.py"],
            cwd=str(ROOT),
            stdout=open(ROOT / "logs" / "bot_brief.log", "w"),
            stderr=subprocess.STDOUT,
        )
        reply_text(message_id, "✅ brief 已在后台启动，跑完会自动推送到 webhook 群（约 5-7 分钟）")
    except Exception as e:
        reply_text(message_id, f"⚠️ 启动 brief 失败: {e}")


# =================================================
# 事件入口
# =================================================
def on_message_receive(data: P2ImMessageReceiveV1) -> None:
    """收到 @ 机器人的消息时触发"""
    try:
        msg = data.event.message
        msg_id = msg.message_id
        chat_id = msg.chat_id
        msg_type = msg.message_type

        if msg_type != "text":
            return  # 暂只处理文本

        content = json.loads(msg.content)
        raw_text = content.get("text", "")

        # 去掉 @ 前缀，飞书格式如 "@_user_1 600519"
        cleaned = re.sub(r"@_user_\d+", "", raw_text).strip()
        # 也去掉可能的中文 @
        cleaned = re.sub(r"^@\S+\s*", "", cleaned).strip()

        print(f"📩 [{datetime.now().strftime('%H:%M:%S')}] 收到 chat={chat_id} text={cleaned!r}")

        if not cleaned:
            reply_text(msg_id, HELP_TEXT)
            return

        handle_command(cleaned, msg_id, chat_id)
    except Exception as e:
        print(f"❌ on_message_receive 异常: {type(e).__name__}: {e}")


def main():
    print(f"🤖 股民简报机器人启动 (app_id={APP_ID[:12]}...)")
    print("   监听 @ 消息中... Ctrl+C 退出")

    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message_receive) \
        .build()

    ws = lark.ws.Client(
        APP_ID, APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    ws.start()


if __name__ == "__main__":
    main()
