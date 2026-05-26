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


_A_SHARE_CACHE = ROOT / "data" / "cache" / "a_share_universe.json"


@functools.lru_cache(maxsize=1)
def _all_a_stocks() -> list[dict]:
    """全 A 股 code/name 列表（磁盘缓存 + 多源 fallback）。
    用于非自选股按名字 @ 时的 fallback 查询。"""
    # 1) 磁盘缓存（bot 重启秒读）
    if _A_SHARE_CACHE.exists():
        try:
            cached = json.loads(_A_SHARE_CACHE.read_text())
            if isinstance(cached, list) and cached:
                return cached
        except Exception:
            pass
    # 2) 在线拉：EM 主源（快 ~2-3s）→ 新浪 spot 备源（~30s 但更稳）
    import akshare as ak
    for label, fn in [
        ("EM code_name", lambda: ak.stock_info_a_code_name()),
        ("Sina spot",    lambda: ak.stock_zh_a_spot()),
    ]:
        try:
            df = fn()
            # 规范化：EM 给 code/name；新浪 spot 给 代码(sz002480)/名称
            if "code" in df.columns:
                rows = [{"code": str(r["code"]), "name": str(r["name"])} for _, r in df.iterrows()]
            else:
                rows = [{"code": str(r["代码"])[-6:], "name": str(r["名称"])} for _, r in df.iterrows()]
            print(f"✅ 全 A 股拉取成功 ({label}): {len(rows)} 只")
            _A_SHARE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _A_SHARE_CACHE.write_text(json.dumps(rows, ensure_ascii=False))
            return rows
        except Exception as e:
            print(f"⚠️  全 A 股 {label} 拉取失败: {type(e).__name__}: {e}")
    return []


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

📊 单股分析（盘中拿实时价 + 主力净流入）
  @股民简报 600519      — 任意 6 位 A 股代码（不必在自选股）
  @股民简报 万华化学    — 完整公司名（非自选股需写全名避免歧义）
  @股民简报 茅台        — 自选股可用简称

🌐 全市场
  @股民简报 watchlist   — 列当前自选股
  @股民简报 板块        — 今日强势板块 + Top 3
  @股民简报 龙虎榜      — 昨日游资动作
  @股民简报 brief       — 跑完整简报（5-7 分钟）"""


_FILLER_PATTERNS = [
    r"^分析一下\s*", r"^看一下\s*", r"^看看\s*", r"^分析\s*", r"^查一下\s*",
    r"\s*怎么样$", r"\s*如何$", r"\s*怎么办$", r"^请\s*",
]


def _strip_fillers(s: str) -> str:
    for p in _FILLER_PATTERNS:
        s = re.sub(p, "", s).strip()
    return s.strip(" ，,。.?？!！")


def _find_name_in_text(name: str, text: str) -> str | None:
    """在 text 里找 name 或其简称，返回实际命中的子串"""
    if name in text:
        return name
    if len(name) >= 3:
        # 后 2-3 字（"茅台" of "贵州茅台"、"科技" of "蓝思科技"）
        for n in (name[-3:], name[-2:], name[:3], name[:2]):
            if n and n in text:
                return n
    # text 自身是 name 的子串（用户输入"珂玛"，name 是"珂玛科技"）
    cleaned = text.strip()
    if 2 <= len(cleaned) <= len(name) and cleaned in name:
        return cleaned
    return None


def resolve_stock(text: str) -> tuple[str, str, str, str] | None:
    """识别用户输入 → (code, name, market, user_context)；找不到 → None
    user_context = 用户在股票代码/名字外说的其他话（"我有 1000 股 75 入"之类）"""
    text = text.strip()
    wl = _load_watchlist()

    # 1) 嵌入的 6 位代码
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if m:
        code = m.group(1)
        s = next((x for x in wl if x["code"] == code), None)
        name = s["name"] if s else code
        market = s.get("market", "a") if s else "a"
        remaining = (text[:m.start()] + text[m.end():]).strip()
        return code, name, market, _strip_fillers(remaining)

    # 2) 名字模糊匹配（含简称）。选 hit 子串最长的（避免歧义短匹配优先长完整名）
    matches = []
    for s in wl:
        hit = _find_name_in_text(s["name"], text)
        if hit:
            matches.append((s, hit))
    if matches:
        best_s, best_hit = max(matches, key=lambda x: len(x[1]))
        remaining = text.replace(best_hit, "", 1).strip()
        return best_s["code"], best_s["name"], best_s.get("market", "a"), _strip_fillers(remaining)

    # 3) Fallback: 全 A 股完整名字匹配（要求 stock.name 完整出现在 text 中，避免歧义）
    all_a = _all_a_stocks()
    full_hits = [s for s in all_a if s["name"] and s["name"] in text]
    if full_hits:
        # 多个匹配选名字最长的（更具体），单个就直接用
        best = max(full_hits, key=lambda s: len(s["name"]))
        remaining = text.replace(best["name"], "", 1).strip()
        return best["code"], best["name"], "a", _strip_fillers(remaining)

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
        code, name, market, user_context = hit
        if market != "a":
            reply_text(message_id, f"⚠️ 暂只支持 A 股深度分析，{name}({code}) 是 {market.upper()}")
            return
        hint = f"（含个人 context: 「{user_context}」）" if user_context else ""
        reply_text(message_id, f"⏳ 分析 {name}({code}){hint} 中（约 60-90 秒，含 LLM）...")
        threading.Thread(
            target=_task_stock_analysis,
            args=(message_id, code, name, user_context),
            daemon=True,
        ).start()
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


def _task_stock_analysis(message_id: str, code: str, name: str, user_context: str = ""):
    try:
        import akshare as ak
        import pandas as pd
        from brief import (
            YESTERDAY, TODAY, gather_a, llm_summarize, safe_df,
        )
        # 单股只需要 lookup 用的几张表（不拉板块 + 游资聚合，那是 Layer 1 全市场动作）
        market = {
            "lhb":         safe_df("龙虎榜", lambda: ak.stock_lhb_detail_daily_sina(date=YESTERDAY)),
            "dzjy":        safe_df("大宗交易", lambda: ak.stock_dzjy_mrtj(start_date=YESTERDAY, end_date=TODAY)),
            "margin_sse":  safe_df("融资融券-沪", lambda: ak.stock_margin_detail_sse(date=YESTERDAY)),
            "margin_szse": safe_df("融资融券-深", lambda: ak.stock_margin_detail_szse(date=YESTERDAY)),
            "industry_list": pd.DataFrame(),  # 板块同业跳过
        }
        # 盘中场景：腾讯 qt 实时优先，baostock 兜底（baostock 只给日 K 收盘价）
        payload = gather_a(code, market, prefer_realtime=True)
        summary = llm_summarize(name, code, payload, user_context=user_context)
        # 包装成 markdown
        k = payload.get("K线技术") or {}
        h = payload.get("北向持股") or {}
        head = [f"### {name}（{code}）\n"]
        if k:
            head.append(f"**今日**：¥{k.get('收盘')} ({k.get('当日涨跌幅%')}%) | 5 日 {k.get('5日累计涨跌幅%')}% | MA: {k.get('均线排列')} | MACD: {k.get('MACD金叉死叉')} (柱 {k.get('MACD柱')})\n")
        if h:
            head.append(f"**北向**：占 A 股 {h.get('持股占A股%')}% | 7 日累计 **{h.get('7日累计增持(亿元)')} 亿**\n")
        if user_context:
            head.append(f"**🧑 你的 context**：{user_context}\n")
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

    # 后台预热全 A 股列表（首次 @ 非自选股名字时不会卡 30 秒）
    threading.Thread(target=_all_a_stocks, daemon=True).start()

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
