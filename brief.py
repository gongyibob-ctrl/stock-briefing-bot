"""
brief.py — V2 简报生成器

A 股（market="a"）：
  Layer 1 全市场预拉：龙虎榜 / 大宗交易 / 融资融券 sse+szse / 北向总览 / 行业板块
  Layer 2 per stock：行情 + K 线技术指标 + 北向 + 龙虎榜命中 + 大宗 + 融资融券
                     + 股东户数趋势 + 板块同业对比 + 公告 + 新闻
港股（market="hk"）：行情 + 技术指标 + 港股通持股（如有） + 新闻
美股（market="us"）：行情 + 技术指标 + 新闻（走 yfinance）

LLM 输出：数据清点 → 显式因果推理 → 结论建议（带数据依据）
"""
from __future__ import annotations

import atexit
import functools
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import baostock as bs
import litellm
import pandas as pd
from dotenv import load_dotenv

# 关键：akshare 不设 timeout 会无限 hang。全局兜底。
socket.setdefaulttimeout(15)
# print 立即 flush
print = functools.partial(print, flush=True)  # noqa: A001

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
litellm.drop_params = True

WATCHLIST = json.loads((ROOT / "watchlist.json").read_text())["watchlist"]
TODAY = datetime.now().strftime("%Y%m%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
LAST_MONTH = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")  # K 线要 30 天才能算 MA20


# ====================================================
# 席位识别（从 data/seats.json 动态加载，无需重启）
# ====================================================
_SEATS_CACHE: dict | None = None
_SEATS_MTIME: float | None = None


def _load_seats() -> dict:
    """加载 seats.json，文件改动自动刷新（mtime 检测）"""
    global _SEATS_CACHE, _SEATS_MTIME
    seats_path = ROOT / "data" / "seats.json"
    if not seats_path.exists():
        return {"hot_money": [], "generic": [], "discovery": {}}
    try:
        mtime = seats_path.stat().st_mtime
        if _SEATS_CACHE is None or mtime != _SEATS_MTIME:
            _SEATS_CACHE = json.loads(seats_path.read_text())
            _SEATS_MTIME = mtime
    except Exception as e:
        print(f"⚠️  seats.json 解析失败: {e}")
        return {"hot_money": [], "generic": [], "discovery": {}}
    return _SEATS_CACHE


def classify_seat(name: str) -> str | None:
    """返回席位的江湖标签，无匹配返回 None"""
    if not name:
        return None
    seats = _load_seats()
    # hot_money 优先（具体路名比 generic 关键词更精确）
    for s in seats.get("hot_money", []):
        for keywords in s.get("match", []):
            if all(kw in name for kw in keywords):
                return s["name"]
    for s in seats.get("generic", []):
        for keywords in s.get("match", []):
            if all(kw in name for kw in keywords):
                return s["name"]
    return None


def _num(v) -> float:
    """安全转 float，NaN/None/异常都返回 0.0"""
    try:
        n = float(v)
        return 0.0 if n != n else n  # NaN check
    except (TypeError, ValueError):
        return 0.0


# ====================================================
# baostock 会话管理（A 股行情主源）
# ====================================================
_BS_LOGGED_IN = False


def _bs_login():
    global _BS_LOGGED_IN
    if _BS_LOGGED_IN:
        return
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    _BS_LOGGED_IN = True


@atexit.register
def _bs_logout():
    global _BS_LOGGED_IN
    if _BS_LOGGED_IN:
        try:
            bs.logout()
        except Exception:
            pass
        _BS_LOGGED_IN = False


def _bs_code(code: str) -> str:
    return f"sh.{code}" if code.startswith("6") else f"sz.{code}"


_META_CACHE: dict | None = None


def _load_meta() -> dict:
    global _META_CACHE
    if _META_CACHE is not None:
        return _META_CACHE
    meta_path = ROOT / "stock_meta.json"
    _META_CACHE = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return _META_CACHE


def safe(label: str, fn, default=None, retries: int = 3, backoff: float = 2.0):
    last = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(backoff * (i + 1))
    print(f"  ⚠️  {label}: {type(last).__name__}: {last}")
    return default


# ====================================================
# A 股 — Layer 1 全市场预拉
# ====================================================
def fetch_market_tables() -> dict:
    print("  · 龙虎榜 / 大宗 / 融资融券 / 北向 / 板块...")
    tables = {
        "lhb":         safe("龙虎榜", lambda: ak.stock_lhb_detail_daily_sina(date=YESTERDAY), pd.DataFrame()),
        "dzjy":        safe("大宗交易", lambda: ak.stock_dzjy_mrtj(start_date=YESTERDAY, end_date=TODAY), pd.DataFrame()),
        "margin_sse":  safe("融资融券-沪", lambda: ak.stock_margin_detail_sse(date=YESTERDAY), pd.DataFrame()),
        "margin_szse": safe("融资融券-深", lambda: ak.stock_margin_detail_szse(date=YESTERDAY), pd.DataFrame()),
        "hsgt_summary": _hsgt_summary(),
        "industry_list": safe("行业列表", ak.stock_board_industry_name_em, pd.DataFrame()),
    }
    # 全市场游资动作聚合（拉 Top 20 上榜股的席位明细）
    tables["hot_money"] = layer1_hot_money_summary(tables["lhb"], YESTERDAY, top_n=20)

    # 今日最强板块 + 板块内优质股（同花顺主源 + 新浪备用 + LLM 选 Top3）
    print("  · 拉今日强势板块 + 优质企业筛选...")
    try:
        from sector import run_sector_module
        _, sector_md = run_sector_module(top_n_sectors=5, pool_size=6, call_llm=True)
        tables["sector_md"] = sector_md
    except Exception as e:
        print(f"  ⚠️  板块模块失败: {type(e).__name__}: {e}")
        tables["sector_md"] = ""

    return tables


# 知名游资席位 tag 判断（来自 seats.json hot_money 区）
_HOT_MONEY_TAGS = {"孙哥", "章盟主", "作手新一", "赵老哥", "涛哥", "小鳄鱼", "炒股养家", "彩田路", "武定路", "中金上海", "银河绍兴"}


def _classify_signal_role(tag: str) -> str:
    """席位 tag → 资金角色（机构 / 知名游资 / 北向 / 散户 / 其他）"""
    if not tag:
        return "其他"
    if "机构" in tag:
        return "机构"
    if "北向" in tag:
        return "北向"
    if "拉萨" in tag:
        return "散户"
    if any(name in tag for name in _HOT_MONEY_TAGS):
        return "知名游资"
    return "其他"


def layer1_hot_money_summary(lhb_df: pd.DataFrame, date: str, top_n: int = 20) -> dict:
    """复用一次席位拉取，同时返回：
    1. top_seats - 席位聚合（哪个游资/机构今日最活跃）
    2. lhb_diagnose - 个股诊断（每只上榜股是看好/看空/博弈）
    3. 待命名席位 - 发现新游资候选
    """
    if lhb_df is None or lhb_df.empty:
        return {}
    top = lhb_df.sort_values("成交额", ascending=False).drop_duplicates("股票代码").head(top_n)
    print(f"  · 拉 Top {len(top)} 龙虎榜股票席位明细（席位聚合 + 个股诊断）...")

    seat_aggr: dict = {}
    per_stock: dict = {}  # 股票代码 → {机构买/机构卖/游资买列表/散户买/北向买/北向卖/触发}

    for _, row in top.iterrows():
        code = row["股票代码"]
        name = row["股票名称"]
        per_stock[code] = {
            "代码": code,
            "名称": name,
            "收盘": row.get("收盘价"),
            "对应值": row.get("对应值"),
            "指标": row.get("指标", ""),
            "机构买": 0.0, "机构卖": 0.0,
            "知名游资买": [],  # list of (tag_name, amount_yuan)
            "知名游资卖": [],
            "散户买": 0.0, "散户卖": 0.0,
            "北向买": 0.0, "北向卖": 0.0,
        }
        for flag in ("买入", "卖出"):
            df = safe(
                f"席位 {code} {flag}",
                lambda c=code, f=flag: ak.stock_lhb_stock_detail_em(symbol=c, date=date, flag=f),
                pd.DataFrame(), retries=2, backoff=1.0,
            )
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                seat = r.get("交易营业部名称") or ""
                if not seat:
                    continue
                tag = classify_seat(seat)
                buy_yuan = _num(r.get("买入金额"))
                sell_yuan = _num(r.get("卖出金额"))

                # 席位 → 股票聚合（不变）
                rec = seat_aggr.setdefault(seat, {
                    "tag": tag,
                    "买入": 0.0, "卖出": 0.0, "股票数": 0, "股票": [],
                })
                rec["买入"] += buy_yuan
                rec["卖出"] += sell_yuan
                if code not in rec["股票"]:
                    rec["股票"].append(code)
                    rec["股票数"] += 1

                # 股票 → 角色聚合（新增）
                role = _classify_signal_role(tag)
                st = per_stock[code]
                if role == "机构":
                    st["机构买"] += buy_yuan
                    st["机构卖"] += sell_yuan
                elif role == "北向":
                    st["北向买"] += buy_yuan
                    st["北向卖"] += sell_yuan
                elif role == "散户":
                    st["散户买"] += buy_yuan
                    st["散户卖"] += sell_yuan
                elif role == "知名游资":
                    if buy_yuan > 1e6:  # 100 万门槛过滤噪音
                        st["知名游资买"].append((tag, buy_yuan))
                    if sell_yuan > 1e6:
                        st["知名游资卖"].append((tag, sell_yuan))

    # === 席位维度聚合（保持原有 top_seats / 待命名） ===
    rows = []
    for seat, rec in seat_aggr.items():
        net = rec["买入"] - rec["卖出"]
        rows.append({
            "席位": seat, "标签": rec["tag"],
            "买入(万)": round(rec["买入"] / 1e4, 1),
            "卖出(万)": round(rec["卖出"] / 1e4, 1),
            "净额(万)": round(net / 1e4, 1),
            "命中股票数": rec["股票数"],
            "命中股票": rec["股票"][:3],
        })
    significant = [r for r in rows if r["标签"] or r["命中股票数"] >= 2]
    significant.sort(key=lambda x: abs(x["净额(万)"]), reverse=True)

    disc_cfg = _load_seats().get("discovery", {})
    min_stocks = disc_cfg.get("min_stocks", 3)
    min_net_wan = disc_cfg.get("min_net_amount_yuan", 100_000_000) / 1e4
    max_show = disc_cfg.get("max_show", 5)
    discoveries = [
        r for r in rows
        if not r["标签"] and r["命中股票数"] >= min_stocks and abs(r["净额(万)"]) >= min_net_wan
    ]
    discoveries.sort(key=lambda x: abs(x["净额(万)"]), reverse=True)

    # === 个股诊断（🟢🔴⚪） ===
    diagnose_threshold_inst = 1e8       # 机构净买/卖 1 亿
    diagnose_threshold_hot = 5e7        # 知名游资买入 5000 万
    diagnose_threshold_retail = 5e7     # 拉萨散户买入 5000 万
    diagnoses = []
    for code, s in per_stock.items():
        inst_net = s["机构买"] - s["机构卖"]
        hot_buy_total = sum(amt for _, amt in s["知名游资买"])
        hot_sell_total = sum(amt for _, amt in s["知名游资卖"])
        retail_buy = s["散户买"]

        verdict, reason = "⚪", "博弈不明"
        if inst_net >= diagnose_threshold_inst:
            verdict, reason = "🟢", f"机构净买 +{inst_net/1e8:.2f} 亿"
        elif hot_buy_total >= diagnose_threshold_hot:
            top_hot = max(s["知名游资买"], key=lambda x: x[1])
            verdict, reason = "🟢", f"{top_hot[0]} 买入 {top_hot[1]/1e4:.0f} 万"
        elif inst_net <= -diagnose_threshold_inst:
            verdict, reason = "🔴", f"机构净卖 -{abs(inst_net)/1e8:.2f} 亿"
        elif retail_buy >= diagnose_threshold_retail and inst_net < 0:
            verdict, reason = "🔴", f"散户接盘（拉萨买 {retail_buy/1e4:.0f} 万 + 机构 -{abs(inst_net)/1e4:.0f} 万）"
        elif hot_sell_total >= diagnose_threshold_hot:
            top_hot = max(s["知名游资卖"], key=lambda x: x[1])
            verdict, reason = "🔴", f"{top_hot[0]} 卖出 {top_hot[1]/1e4:.0f} 万"

        diagnoses.append({
            "代码": s["代码"], "名称": s["名称"],
            "对应值": s["对应值"], "指标": s["指标"],
            "判断": verdict, "理由": reason,
        })

    # 排序：🟢 在前，🔴 中间，⚪ 在后；同 verdict 内按指标排（涨跌 / 换手）
    order_key = {"🟢": 0, "🔴": 1, "⚪": 2}
    diagnoses.sort(key=lambda d: (order_key[d["判断"]], d["代码"]))

    return {
        "top_seats": significant[:15],
        "总股票数": len(top),
        "待命名席位": discoveries[:max_show],
        "lhb_diagnose": diagnoses,
    }


def a_lhb_seats(code: str, date: str) -> dict:
    """单股席位明细（买入卖出前 5 + 江湖名号）"""
    def _fetch(flag):
        df = ak.stock_lhb_stock_detail_em(symbol=code, date=date, flag=flag)
        return df if df is not None else pd.DataFrame()

    def _rows(df):
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.head(5).iterrows():
            seat = r.get("交易营业部名称") or ""
            out.append({
                "席位": seat,
                "标签": classify_seat(seat),
                "买入(万)": round(_num(r.get("买入金额")) / 1e4, 1),
                "卖出(万)": round(_num(r.get("卖出金额")) / 1e4, 1),
            })
        return out

    buy = safe(f"席位 {code} 买", lambda: _fetch("买入"), pd.DataFrame(), retries=2)
    sell = safe(f"席位 {code} 卖", lambda: _fetch("卖出"), pd.DataFrame(), retries=2)
    return {"买入前5": _rows(buy), "卖出前5": _rows(sell)}


def _hsgt_summary() -> dict:
    """北向资金当日实时（fund_flow_summary）+ 历史累计（hist_em）双源。"""
    # 主：当日实时（fund_flow_summary）
    def _from_summary():
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is None or df.empty:
            return {}
        north = df[df.get("资金方向", pd.Series([], dtype=str)) == "北向"]
        if north.empty:
            return {}
        sh = north[north["板块"].str.contains("沪股通", na=False)]
        sz = north[north["板块"].str.contains("深股通", na=False)]
        sh_net = float(sh["成交净买额"].iloc[0]) if not sh.empty else 0.0
        sz_net = float(sz["成交净买额"].iloc[0]) if not sz.empty else 0.0
        sh_idx = float(sh["指数涨跌幅"].iloc[0]) if not sh.empty else None
        sz_idx = float(sz["指数涨跌幅"].iloc[0]) if not sz.empty else None
        date = str(north.iloc[0]["交易日"])
        return {
            "最近交易日": date,
            "当日净买额(亿)": round(sh_net + sz_net, 2),
            "沪股通净买(亿)": round(sh_net, 2),
            "深股通净买(亿)": round(sz_net, 2),
            "上证涨幅%": sh_idx,
            "深证涨幅%": sz_idx,
        }

    # 备：历史累计（hist_em，近 N 日有效数据）
    def _from_hist():
        df = ak.stock_hsgt_hist_em(symbol="北向资金")
        if df is None or df.empty:
            return {}
        recent = df.tail(10)  # 拉宽窗口提高拿到有效数据的概率
        valid = recent.dropna(subset=["当日成交净买额"])
        if valid.empty:
            return {}
        return {
            "5日累计净买(亿)": round(float(valid.tail(5)["当日成交净买额"].sum()) / 100, 2),
            "10日累计净买(亿)": round(float(valid["当日成交净买额"].sum()) / 100, 2),
        }

    out = safe("北向当日", _from_summary, {}, retries=2, backoff=1.5)
    hist = safe("北向累计", _from_hist, {}, retries=2, backoff=1.5)
    if hist:
        out.update(hist)
    return out


# ====================================================
# A 股 — Layer 2 per stock
# ====================================================
def _tencent_prefix(code: str) -> str:
    """A 股代码 → 腾讯前缀：6/688/689 = sh，其他 sz"""
    return "sh" if code.startswith("6") else "sz"


def _a_quote_tencent(code: str) -> dict:
    """fallback 2: qt.gtimg.cn 实时盘口"""
    import requests
    sym = f"{_tencent_prefix(code)}{code}"
    r = requests.get(f"http://qt.gtimg.cn/q={sym}", timeout=12)
    raw = r.content.decode("gbk", errors="ignore")
    if "=" not in raw or '""' in raw:
        return {}
    fields = raw.split('"', 2)[1].split("~") if '"' in raw else raw.split("~")
    if len(fields) < 46:
        return {}
    meta = _load_meta().get(code, {})

    def _f(idx):
        try:
            return float(fields[idx]) if fields[idx] else None
        except (ValueError, IndexError):
            return None

    return {
        "最新价": _f(3),
        "总市值(亿)": _f(45) or meta.get("总市值(亿)"),
        "流通市值(亿)": _f(44) or meta.get("流通市值(亿)"),
        "行业": meta.get("行业"),
        "_source": "tencent_qt",
    }


def _a_quote_sina(code: str) -> dict:
    """fallback 3: 新浪 hq.sinajs.cn 实时盘口（基础设施独立于腾讯）"""
    import re as _re
    import requests
    sym = f"{_tencent_prefix(code)}{code}"
    headers = {"Referer": "http://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}
    r = requests.get(f"http://hq.sinajs.cn/list={sym}", timeout=12, headers=headers)
    raw = r.content.decode("gbk", errors="ignore")
    m = _re.search(r'"([^"]+)"', raw)
    if not m or len(m.group(1)) < 5:
        return {}
    fields = m.group(1).split(",")
    if len(fields) < 4:
        return {}
    meta = _load_meta().get(code, {})
    try:
        return {
            "最新价": float(fields[3]),
            "总市值(亿)": meta.get("总市值(亿)"),
            "流通市值(亿)": meta.get("流通市值(亿)"),
            "行业": meta.get("行业"),
            "_source": "sina_hq",
        }
    except (ValueError, IndexError):
        return {}


def a_quote(code: str, prefer_realtime: bool = False) -> dict:
    """baostock 主源 → 腾讯 qt 兜底；基本面从 stock_meta.json 取
    prefer_realtime=True 时腾讯实时优先（盘中拿当前价 + 委比 + 主力净流入）"""
    def _via_baostock():
        _bs_login()
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            _bs_code(code),
            "date,close,pctChg",
            start_date=start, end_date=end,
            frequency="d", adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return {}
        meta = _load_meta().get(code, {})
        return {
            "最新价": float(rows[-1][1]),
            "总市值(亿)": meta.get("总市值(亿)"),
            "流通市值(亿)": meta.get("流通市值(亿)"),
            "行业": meta.get("行业"),
        }

    sources = [
        ("baostock", _via_baostock, 1, 1.0),
        ("腾讯",     lambda: _a_quote_tencent(code), 3, 2.0),
        ("新浪",     lambda: _a_quote_sina(code), 3, 2.0),
    ]
    if prefer_realtime:
        # 盘中：腾讯/新浪 实时优先，baostock 兜底（baostock 只给日 K 收盘价）
        sources = [sources[1], sources[2], sources[0]]

    for label, fn, retries, backoff in sources:
        out = safe(f"行情快照 {code}[{label}]", fn, {}, retries=retries, backoff=backoff)
        if out.get("最新价") is not None:
            return out
    return {}


def _a_kline_tencent(code: str) -> dict:
    """fallback 2: ifzq.gtimg.cn 历史日 K（40 天 OHLCV，纯 HTTP）"""
    import requests
    sym = f"{_tencent_prefix(code)}{code}"
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,40,qfq"
    r = requests.get(url, timeout=12)
    data = r.json()
    if data.get("code") != 0:
        return {}
    kdata = data.get("data", {}).get(sym, {})
    key = next((k for k in kdata if "day" in k.lower()), None)
    if not key or not kdata[key]:
        return {}
    rows = kdata[key]
    df = pd.DataFrame(rows)
    df = df.iloc[:, :6]
    df.columns = ["日期", "open", "收盘", "high", "low", "volume"]
    for col in ("open", "收盘", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["涨跌幅"] = df["收盘"].pct_change() * 100
    df["成交额"] = df["收盘"] * df["volume"]
    if len(df) < 5:
        return {}
    out = _enrich_kline(df)
    out["_source"] = "tencent_ifzq"
    return out


def _a_kline_sina(code: str) -> dict:
    """fallback 3: 新浪历史 K 线（json_v2 接口，独立基础设施）"""
    import requests
    sym = f"{_tencent_prefix(code)}{code}"
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sym}&scale=240&datalen=40"
    r = requests.get(url, timeout=12)
    data = r.json()
    if not data:
        return {}
    df = pd.DataFrame(data)
    df = df.rename(columns={"day": "日期", "close": "收盘"})
    for col in ("open", "high", "low", "收盘", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["涨跌幅"] = df["收盘"].pct_change() * 100
    df["成交额"] = df["收盘"] * df["volume"]
    if len(df) < 5:
        return {}
    out = _enrich_kline(df)
    out["_source"] = "sina_kline"
    return out


def a_kline(code: str, prefer_realtime: bool = False) -> dict:
    """baostock K 线 → 腾讯 ifzq 兜底；返回 MA + MACD + 涨跌幅
    prefer_realtime=True 时腾讯优先（盘中最后一根日 K 是今日实时不完整 bar）"""
    def _via_baostock():
        _bs_login()
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            _bs_code(code),
            "date,open,high,low,close,volume,amount,pctChg",
            start_date=start, end_date=end,
            frequency="d", adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows or len(rows) < 5:
            return {}
        df = pd.DataFrame(rows, columns=rs.fields)
        for col in ("open", "high", "low", "close", "volume", "amount", "pctChg"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.rename(columns={
            "date": "日期", "close": "收盘", "amount": "成交额", "pctChg": "涨跌幅",
        })
        return _enrich_kline(df)

    sources = [
        ("baostock", _via_baostock, 1, 1.0),
        ("腾讯",     lambda: _a_kline_tencent(code), 3, 2.0),
        ("新浪",     lambda: _a_kline_sina(code), 3, 2.0),
    ]
    if prefer_realtime:
        sources = [sources[1], sources[2], sources[0]]

    for label, fn, retries, backoff in sources:
        out = safe(f"K线 {code}[{label}]", fn, {}, retries=retries, backoff=backoff)
        if out and out.get("收盘") is not None:
            return out
    return {}


def _enrich_kline(df: pd.DataFrame) -> dict:
    """从 K 线 DF 计算 MA + MACD + 近期统计"""
    close = df["收盘"].astype(float)
    df["MA5"]  = close.rolling(5).mean()
    df["MA10"] = close.rolling(10).mean()
    df["MA20"] = close.rolling(20).mean()
    # MACD: EMA12 - EMA26, signal=EMA9(MACD)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    latest = df.iloc[-1]
    last_close = float(latest["收盘"])
    return {
        "最近交易日": str(latest["日期"]),
        "收盘": round(last_close, 2),
        "当日涨跌幅%": round(float(latest["涨跌幅"]), 2),
        "成交额(亿)": round(float(latest["成交额"]) / 1e8, 2),
        "5日累计涨跌幅%": round((last_close - float(df.iloc[-5]["收盘"])) / float(df.iloc[-5]["收盘"]) * 100, 2) if len(df) >= 5 else None,
        "MA5":  round(float(df.iloc[-1]["MA5"]), 2) if not pd.isna(df.iloc[-1]["MA5"]) else None,
        "MA10": round(float(df.iloc[-1]["MA10"]), 2) if not pd.isna(df.iloc[-1]["MA10"]) else None,
        "MA20": round(float(df.iloc[-1]["MA20"]), 2) if not pd.isna(df.iloc[-1]["MA20"]) else None,
        "均线排列": _ma_pattern(df.iloc[-1]),
        "MACD柱": round(float(hist.iloc[-1]), 3),
        "MACD金叉死叉": _macd_cross(macd, signal),
    }


def _ma_pattern(row) -> str:
    try:
        m5, m10, m20 = float(row["MA5"]), float(row["MA10"]), float(row["MA20"])
        c = float(row["收盘"])
        if c > m5 > m10 > m20:
            return "多头排列（价>MA5>MA10>MA20）"
        if c < m5 < m10 < m20:
            return "空头排列（价<MA5<MA10<MA20）"
        return "纠缠"
    except Exception:
        return "N/A"


def _macd_cross(macd: pd.Series, signal: pd.Series) -> str:
    if len(macd) < 2:
        return "N/A"
    prev_diff = macd.iloc[-2] - signal.iloc[-2]
    curr_diff = macd.iloc[-1] - signal.iloc[-1]
    if prev_diff < 0 and curr_diff > 0:
        return "今日金叉"
    if prev_diff > 0 and curr_diff < 0:
        return "今日死叉"
    return "无交叉"


def a_hsgt(code: str) -> dict:
    def _fetch():
        df = ak.stock_hsgt_individual_em(symbol=code)
        if df is None or df.empty:
            return {}
        recent = df.tail(7)
        return {
            "最新持股日": str(recent.iloc[-1]["持股日期"]),
            "持股占A股%": round(float(recent.iloc[-1]["持股数量占A股百分比"]), 2),
            "7日累计增持(亿元)": round(float(recent["今日增持资金"].sum()) / 1e8, 2),
        }
    return safe(f"北向 {code}", _fetch, {})


def a_lhb_hit(code: str, lhb_df: pd.DataFrame) -> list[dict]:
    if lhb_df is None or lhb_df.empty:
        return []
    hits = lhb_df[lhb_df["股票代码"] == code]
    if hits.empty:
        return []
    return hits[["股票名称", "收盘价", "对应值", "成交额", "指标"]].to_dict("records")


def a_dzjy_hit(code: str, dzjy_df: pd.DataFrame) -> list[dict]:
    if dzjy_df is None or dzjy_df.empty:
        return []
    hits = dzjy_df[dzjy_df["证券代码"] == code]
    if hits.empty:
        return []
    return hits[["交易日期", "收盘价", "成交价", "折溢率", "成交总额"]].to_dict("records")


def a_margin_hit(code: str, sse_df: pd.DataFrame, szse_df: pd.DataFrame) -> dict:
    df = sse_df if code.startswith("6") else szse_df
    if df is None or df.empty:
        return {}
    code_col = next((c for c in df.columns if "代码" in c), None)
    if not code_col:
        return {}
    hits = df[df[code_col].astype(str).str.strip() == code]
    if hits.empty:
        return {}
    row = hits.iloc[0].to_dict()
    keep = {}
    for k, v in row.items():
        if any(kw in k for kw in ["融资", "融券", "余额"]):
            try:
                keep[k] = round(float(v) / 1e8, 2)  # 转亿元
            except (TypeError, ValueError):
                keep[k] = v
    return keep


def a_gdhs(code: str) -> dict:
    """股东户数最新趋势 — 按时间降序取最近 4 期"""
    def _fetch():
        df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
        if df is None or df.empty:
            return {}
        df = df.sort_values("股东户数统计截止日", ascending=False).head(4)
        latest = df.iloc[0]
        prev_changes = df["股东户数-增减比例"].tolist()
        return {
            "最近统计日": str(latest["股东户数统计截止日"]),
            "股东户数": int(latest["股东户数-本次"]),
            "环比变化%": round(float(latest["股东户数-增减比例"]), 2),
            "近 4 期环比%": [round(float(x), 2) for x in prev_changes],
        }
    return safe(f"股东户数 {code}", _fetch, {})


def a_board(industry_name: str, market_industries: pd.DataFrame) -> dict:
    """板块涨跌幅 + 板块前 3 名同业"""
    if not industry_name or market_industries is None or market_industries.empty:
        return {}
    # 行业名清洗：'白酒Ⅱ' 在板块列表里可能是 '白酒'
    candidates = [industry_name, industry_name.rstrip("Ⅰ Ⅱ Ⅲ").strip(), industry_name[:2]]
    row = None
    for n in candidates:
        match = market_industries[market_industries["板块名称"].str.contains(n, na=False)]
        if not match.empty:
            row = match.iloc[0]
            break
    if row is None:
        return {"未匹配到板块": industry_name}
    # 拿同业 Top 3
    def _cons():
        return ak.stock_board_industry_cons_em(symbol=row["板块名称"])
    cons_df = safe(f"行业 {row['板块名称']} 成分", _cons, pd.DataFrame())
    if cons_df is None or cons_df.empty:
        top3 = []
    else:
        top3 = cons_df.nlargest(3, "涨跌幅")[["名称", "涨跌幅", "最新价"]].to_dict("records")
    return {
        "板块名称": row["板块名称"],
        "板块涨跌幅%": round(float(row["涨跌幅"]), 2),
        "板块今日主力净流入(亿)": round(float(row.get("总市值", 0)) / 1e8, 2) if "总市值" in row else None,
        "今日 Top3 同业": top3,
    }


def a_announcements(code: str) -> list[dict]:
    def _fetch():
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京", category="",
            start_date=LAST_MONTH, end_date=TODAY,
        )
        if df is None or df.empty:
            return []
        cols = [c for c in df.columns if "时间" in c or "标题" in c]
        if len(cols) < 2:
            return []
        return df[cols].head(8).to_dict("records")
    return safe(f"公告 {code}", _fetch, [])


def a_news(code: str) -> list[dict]:
    def _fetch():
        df = ak.stock_news_em(symbol=code)
        if df is None or df.empty:
            return []
        # 把新闻内容也拿过来（截断到 300 字够 LLM 判断利好/利空）
        out = []
        for _, r in df.head(8).iterrows():
            content = str(r.get("新闻内容", "") or "").strip()
            if len(content) > 300:
                content = content[:300] + "…"
            out.append({
                "发布时间": r.get("发布时间"),
                "新闻标题": r.get("新闻标题"),
                "文章来源": r.get("文章来源"),
                "新闻内容": content,
            })
        return out
    return safe(f"新闻 {code}", _fetch, [])


# ====================================================
# 港股
# ====================================================
def hk_kline(code: str) -> dict:
    """港股 K 线 + 技术指标 — 用 yfinance 替代 akshare（东财限频严重）"""
    def _fetch():
        import yfinance as yf
        ticker = f"{int(code):04d}.HK"  # 00700 → 0700.HK
        df = yf.Ticker(ticker).history(period="2mo")
        if df is None or df.empty or len(df) < 5:
            return {}
        df = df.reset_index().rename(columns={
            "Date": "日期", "Close": "收盘", "Volume": "成交量",
        })
        df["涨跌幅"] = df["收盘"].pct_change() * 100
        df["成交额"] = df["收盘"] * df["成交量"]
        return _enrich_kline(df)
    return safe(f"港股 K线 {code}", _fetch, {}, retries=3, backoff=2.0)


def hk_news(code: str) -> list[dict]:
    """港股新闻：东财接口同支持，传带前缀代码"""
    def _fetch():
        df = ak.stock_news_em(symbol=code)
        if df is None or df.empty:
            return []
        return df[["发布时间", "新闻标题", "文章来源"]].head(6).to_dict("records")
    return safe(f"港股新闻 {code}", _fetch, [])


# ====================================================
# 美股
# ====================================================
def us_kline(code: str) -> dict:
    """美股 K 线：用 yfinance 拉，akshare 美股接口代码格式麻烦"""
    def _fetch():
        import yfinance as yf
        ticker = yf.Ticker(code)
        df = ticker.history(period="2mo")
        if df is None or df.empty or len(df) < 5:
            return {}
        # 重命名以匹配 _enrich_kline
        df = df.reset_index()
        df = df.rename(columns={"Date": "日期", "Close": "收盘", "Volume": "成交量"})
        df["涨跌幅"] = df["收盘"].pct_change() * 100
        df["成交额"] = df["收盘"] * df["成交量"]
        return _enrich_kline(df)
    return safe(f"美股 K线 {code}", _fetch, {}, retries=3, backoff=2.0)


def us_news(code: str) -> list[dict]:
    """美股新闻 - yfinance.Ticker.news"""
    def _fetch():
        import yfinance as yf
        items = yf.Ticker(code).news or []
        out = []
        for it in items[:6]:
            content = it.get("content", {}) if isinstance(it, dict) else {}
            out.append({
                "发布时间": content.get("pubDate", it.get("providerPublishTime", "")),
                "新闻标题": content.get("title") or it.get("title", ""),
                "文章来源": (content.get("provider") or {}).get("displayName", it.get("publisher", "")),
            })
        return out
    return safe(f"美股新闻 {code}", _fetch, [])


# ====================================================
# 飞书推送
# ====================================================
_LARK_CLIENT = None


def _get_lark_client():
    """懒加载应用机器人 client；失败返回 None"""
    global _LARK_CLIENT
    if _LARK_CLIENT is not None:
        return _LARK_CLIENT
    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        return None
    try:
        import lark_oapi as lark
        _LARK_CLIENT = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        return _LARK_CLIENT
    except Exception as e:
        print(f"⚠️  飞书 client 初始化失败：{type(e).__name__}: {e}")
        return None


def push_feishu(title: str, content_md: str) -> bool:
    """通过应用机器人发到指定群（FEISHU_CHAT_ID），每段独立消息防截断"""
    client = _get_lark_client()
    chat_id = os.getenv("FEISHU_CHAT_ID")
    if not client or not chat_id:
        print("⚠️  未配置 FEISHU_APP_ID/SECRET/CHAT_ID，跳过推送")
        return False

    sections = _split_markdown(content_md)
    total = len(sections)
    sent = 0
    for i, sec in enumerate(sections, 1):
        sec_title = f"{title} ({i}/{total})"
        if _post_card_via_app(client, chat_id, sec_title, sec):
            sent += 1
        time.sleep(0.5)

    print(f"✅ 飞书推送（应用机器人）：{sent}/{total} 段成功")
    return sent == total


def _post_card_via_app(client, chat_id: str, title: str, body_md: str) -> bool:
    """单条飞书 interactive card via 应用机器人 API"""
    if len(body_md) > 4800:
        body_md = body_md[:4800] + "\n\n... (内容截断，完整版见本地 reports/)"
    card = {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": [{"tag": "markdown", "content": body_md}],
    }
    try:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
        )
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            ).build()
        resp = client.im.v1.message.create(req)
        if resp.success():
            return True
        print(f"  ⚠️  推送失败：code={resp.code} msg={resp.msg}")
        return False
    except Exception as e:
        print(f"  ⚠️  推送异常：{type(e).__name__}: {e}")
        return False


def _is_only_header(s: str) -> bool:
    """段内只剩 H1/H2 标题和空行 → True（这种段不该独立成卡）"""
    for line in s.split("\n"):
        l = line.strip()
        if not l:
            continue
        if l.startswith("# ") or l.startswith("## "):
            continue
        return False
    return True


def _split_markdown(md: str, layer1_max: int = 4500) -> list[str]:
    """切段：
    - Layer 1 默认 1 段；超过 layer1_max 按 ### 切
    - 自选股深度里每个 ### 永远 = 1 段
    - 纯标题段（无具体内容）自动 merge 到下一段，不独立成卡
    """
    lines = md.split("\n")
    layer1_lines: list[str] = []
    layer2_chunks: list[str] = []
    in_individual = False
    cur: list[str] = []

    def push_cur():
        if cur:
            s = "\n".join(cur).strip()
            if s and len(s) > 30:
                layer2_chunks.append(s)
            cur.clear()

    for line in lines:
        if line.startswith("## ") and "自选股深度" in line:
            in_individual = True
            continue
        if not in_individual:
            layer1_lines.append(line)
        else:
            if line.startswith("### "):
                push_cur()
            cur.append(line)
    push_cur()

    # Layer 1 切段
    layer1_chunks: list[str] = []
    layer1_md = "\n".join(layer1_lines).strip()
    if layer1_md:
        if len(layer1_md) <= layer1_max:
            layer1_chunks.append(layer1_md)
        else:
            sub: list[str] = []
            for l in layer1_lines:
                if l.startswith("### ") and sub and any(x.strip() for x in sub):
                    s = "\n".join(sub).strip()
                    if s and len(s) > 30:
                        layer1_chunks.append(s)
                    sub = []
                sub.append(l)
            if sub:
                s = "\n".join(sub).strip()
                if s and len(s) > 30:
                    layer1_chunks.append(s)

    # 关键修复：纯标题段（H1/H2 only）merge 到下一段头部，不独立成卡
    merged_layer1: list[str] = []
    pending_header = ""
    for chunk in layer1_chunks:
        if _is_only_header(chunk):
            pending_header = (pending_header + "\n\n" + chunk).strip() if pending_header else chunk
        else:
            if pending_header:
                merged_layer1.append(pending_header + "\n\n" + chunk)
                pending_header = ""
            else:
                merged_layer1.append(chunk)
    if pending_header:
        merged_layer1.append(pending_header)

    sections = merged_layer1 + layer2_chunks
    return sections


# ====================================================
# LLM
# ====================================================
SYS_PROMPT = """你是给我自己看的股票信息助理。基于结构化数据生成简报，**必须讲清楚因果链 + 解读公告/新闻**。

# 输出格式（严格遵守）

**📋 数据清点**（每条挂具体数据）
- 行情：[价格、涨跌幅、5 日累计、MA 排列、MACD]
- 主力：[北向、龙虎榜、大宗、融资融券 - 只写有信号的]
- 股东户数：[最新户数 + 环比变化%]
- 其他出现在 payload 里的字段

**重要**：payload 没出现的字段表示无相关数据 → **直接省略对应 bullet**，禁止凭空写"板块同业（无数据）/大宗交易（无数据）"这种空陈述

**📰 关键消息解读**（合并同主题，过滤无关榜单类）
格式：`🟢/⚪/🔴 [日期] 一句话内容（不抄标题，浓缩信息）→ 影响判断 _(来源)_`
- 只挑有意义的 3-5 条
- 标题次要，**内容才是重点**：必须基于"新闻内容"字段做摘要
- 必须给 🟢 利好 / ⚪ 中性 / 🔴 利空 标签 + 一句话解读
- 例子：
  - 🟢 [5/22] 公司与控股股东续签商标许可协议，维持品牌使用权延续 → 业务连续性确认 _(财联社)_
  - 🔴 [4/28] 2025 年净利润 2.89 亿，同比 -7.04% → 业绩下滑，估值面临压力 _(界面新闻)_
  - ⚪ [5/19] 2025 年度股东会决议，常规治理事项 → 无实质影响 _(巨潮)_

**🧠 推理链**（一段话 80-180 字，**必须显式因果**）
格式：「因为 [数据 A]，所以 [推论]。叠加 [数据 B]，说明 [另一推论]。同时 [数据 C] 表明 [推论]。综合看 [总体判断]。」

不要只罗列数据。要让我看完知道你**怎么想的**。

**🎯 建议**：[关注/观望/谨慎/减仓/止损/加仓] 之一 —— [一句话总结，引用上面推理链的关键数据点]

# 个性化（如果输入里有"🧑 用户告知的持仓/计划"段）
- 建议必须**针对持有者本人**，不要给路人通用判断
- 已浮亏 → 算"距止损线还有 X%" / 是否分批减仓 / 何种条件下清仓
- 准备买 → 给入场点位 + 主要风险点
- 重仓 → 仓位是否过度集中、何种信号触发减仓
- 满仓 / 长持 → 短期波动可忽略，给长期逻辑判断
- 如果用户给的 context 不包含持仓信息（如"看看怎么样"），按通用分析给

# 硬要求
1. 不允许编造数据，所有数字必须来自我给的输入
2. 推理链必须显式说"因为...所以..."，不能只列事实
3. 公告/新闻解读必须给出 🟢/⚪/🔴 + 一句解读，不能只重复标题
4. 信息真的不足时（多数关键字段为空）才说『信息不足，不下判断』
5. 中文输出，不要 markdown 代码块包裹"""


def llm_summarize(stock_name: str, code: str, payload: dict, user_context: str = "") -> str:
    from litellm import completion
    parts = [f"## {stock_name}（{code}）\n"]
    if user_context:
        parts.append(
            f"### 🧑 用户告知的持仓/计划 / 现状\n"
            f"> {user_context}\n\n"
            f"**重要**：基于这个真实持仓 / 计划，给出**针对持有者本人**的建议（不是路人通用建议）。"
            f"比如：已浮亏 → 是否止损 / 是否分批减仓 / 距止损线还有多远；"
            f"准备买 → 给入场点位 / 风险点；"
            f"重仓持有 → 仓位调整 / 拐点信号。\n"
        )
    parts.append(f"### 数据\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n```")
    user_prompt = "\n".join(parts)
    resp = completion(
        model=os.getenv("LLM_MODEL", "deepseek/deepseek-chat"),
        api_base=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("LLM_API_KEY"),
        messages=[
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=2000,
    )
    content = (resp.choices[0].message.content or "").strip()
    return content or "（LLM 输出为空）"


# ====================================================
# 主流程
# ====================================================
def gather_a(code: str, market: dict, prefer_realtime: bool = False) -> dict:
    quote = a_quote(code, prefer_realtime=prefer_realtime)
    lhb_hit = a_lhb_hit(code, market["lhb"])
    raw = {
        "行情快照": quote,
        "K线技术": a_kline(code, prefer_realtime=prefer_realtime),
        "北向持股": a_hsgt(code),
        "龙虎榜命中": lhb_hit,
        "大宗交易": a_dzjy_hit(code, market["dzjy"]),
        "融资融券(亿元)": a_margin_hit(code, market["margin_sse"], market["margin_szse"]),
        "股东户数趋势": a_gdhs(code),
        "板块同业": a_board(quote.get("行业"), market["industry_list"]),
        "近 30 日公告": a_announcements(code),
        "近期新闻": a_news(code),
    }
    if lhb_hit:
        raw["龙虎榜席位"] = a_lhb_seats(code, YESTERDAY)
    # 删空字段：LLM 看不到 → 不会写"无数据"陈述。空值定义：None / {} / []
    return {k: v for k, v in raw.items() if v not in (None, {}, [])}


def gather_hk(code: str) -> dict:
    return {
        "K线技术": hk_kline(code),
        "近期新闻": hk_news(code),
        # 港股通持股、港股大宗交易暂留待后续
    }


def gather_us(code: str) -> dict:
    return {
        "K线技术": us_kline(code),
        "近期新闻": us_news(code),
    }


def is_trading_day_today() -> bool:
    """A 股今天是不是交易日（周末/法定节假日自动 False）— baostock query_trade_dates"""
    try:
        _bs_login()
        today = datetime.now().strftime("%Y-%m-%d")
        rs = bs.query_trade_dates(start_date=today, end_date=today)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return False
        # fields: calendar_date, is_trading_day（"1" 交易 / "0" 非交易）
        return rows[0][1] == "1"
    except Exception as e:
        print(f"⚠️  交易日检查失败 ({type(e).__name__})，默认按交易日处理")
        return True


def _build_frontmatter(brief_type: str, market: dict | None, deep: list | None) -> dict:
    """报告文件头的 YAML 元数据；未来批量查询历史用 yaml.safe_load 就能读"""
    import re as _re
    now = datetime.now()
    hour = now.hour
    session = "morning" if 6 <= hour < 12 else ("afternoon" if 12 <= hour < 18 else "evening")
    fm: dict = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(timespec="seconds"),
        "type": brief_type,
        "session": session,
        "llm_model": os.getenv("LLM_MODEL", "unknown"),
    }
    # Layer 1 信号
    if market:
        hm = market.get("hot_money") or {}
        seats = hm.get("top_seats") or []
        fm["top_seats"] = [
            {
                "tag": s["标签"] or "未命名",
                "net_yi": round(s["净额(万)"] / 10000, 2),
                "stocks_hit": s["命中股票数"],
            }
            for s in seats[:5]
        ]
        fm["unidentified_seats_count"] = len(hm.get("待命名席位") or [])
    # Layer 2 信号
    if deep:
        wl_advice = []
        for d in deep:
            adv_m = _re.search(r"🎯\s*建议[：:]\s*([关注观望谨慎减仓止损加仓]+)", d.get("summary") or "")
            wl_advice.append({
                "code": d["code"],
                "name": d["name"],
                "advice": adv_m.group(1) if adv_m else "unknown",
            })
        fm["watchlist_advice"] = wl_advice
    return fm


def _render_frontmatter(fm: dict) -> str:
    import yaml
    body = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{body}---\n\n"


def _fetch_lookup_tables_only() -> dict:
    """轻量版 market 表：只拉 watchlist lookup 需要的（跳过游资聚合 + 板块，省 1-2 分钟）"""
    return {
        "lhb":           safe("龙虎榜", lambda: ak.stock_lhb_detail_daily_sina(date=YESTERDAY), pd.DataFrame()),
        "dzjy":          safe("大宗交易", lambda: ak.stock_dzjy_mrtj(start_date=YESTERDAY, end_date=TODAY), pd.DataFrame()),
        "margin_sse":    safe("融资融券-沪", lambda: ak.stock_margin_detail_sse(date=YESTERDAY), pd.DataFrame()),
        "margin_szse":   safe("融资融券-深", lambda: ak.stock_margin_detail_szse(date=YESTERDAY), pd.DataFrame()),
        "industry_list": pd.DataFrame(),
    }


def _process_stocks(market: dict, stocks_filter: list | None = None, dry_run: bool = False) -> list:
    """跑 watchlist 每只股的 gather + LLM。stocks_filter = code 列表（可选）"""
    target = WATCHLIST
    if stocks_filter:
        target = [s for s in WATCHLIST if s["code"] in stocks_filter]
        if not target:
            print(f"⚠️  --stocks {stocks_filter} 在 watchlist 里没找到")
            return []

    deep = []
    for i, stock in enumerate(target, 1):
        code = stock["code"]
        name = stock["name"]
        mkt = stock.get("market", "a")
        t0 = time.time()
        print(f"\n  [{i}/{len(target)}] [{mkt.upper()}] {name}({code}) 抓数据...")
        if mkt == "a":
            payload = gather_a(code, market)
        elif mkt == "hk":
            payload = gather_hk(code)
        elif mkt == "us":
            payload = gather_us(code)
        else:
            payload = {}
        t1 = time.time()
        if dry_run:
            print(f"    数据拿到（耗时 {t1-t0:.1f}s），⏭ DRY-RUN 跳过 LLM")
            summary = "_(dry-run 模式跳过 LLM)_\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2, default=str)[:2000] + "\n```"
        else:
            print(f"    数据拿到（耗时 {t1-t0:.1f}s），调 LLM...")
            try:
                summary = llm_summarize(name, code, payload)
            except Exception as e:
                summary = f"⚠️ LLM 失败: {e}"
            print(f"    ✅ LLM 完成（耗时 {time.time()-t1:.1f}s）")
        deep.append({"code": code, "name": name, "market": mkt, "payload": payload, "summary": summary})
    return deep


def run_market_review() -> tuple[str, dict]:
    """只跑 Layer 1 全市场态势（约 1-2 分钟）。返回 (md, frontmatter_dict)。"""
    market = fetch_market_tables()
    md = render_brief(market=market, deep=None)
    fm = _build_frontmatter("market-only", market, None)
    return md, fm


def run_watchlist_brief(stocks_filter: list | None = None, dry_run: bool = False) -> tuple[str, dict]:
    """只跑 Layer 2 自选股深度（跳过板块/游资聚合）。返回 (md, frontmatter_dict)。"""
    market = _fetch_lookup_tables_only()
    deep = _process_stocks(market, stocks_filter=stocks_filter, dry_run=dry_run)
    md = render_brief(market=None, deep=deep)
    fm = _build_frontmatter("watchlist-filter" if stocks_filter else "watchlist-only", None, deep)
    return md, fm


def run_full_brief(stocks_filter: list | None = None, dry_run: bool = False) -> tuple[str, dict]:
    """Layer 1 + Layer 2 完整简报（约 5-7 分钟）。返回 (md, frontmatter_dict)。"""
    market = fetch_market_tables()
    deep = _process_stocks(market, stocks_filter=stocks_filter, dry_run=dry_run)
    md = render_brief(market=market, deep=deep)
    fm = _build_frontmatter("full", market, deep)
    return md, fm


def main():
    import argparse
    parser = argparse.ArgumentParser(description="股民简报：A 股自选股 + 全市场态势 + LLM 推理 → 飞书")
    parser.add_argument("--market-only", action="store_true", help="只跑 Layer 1 全市场态势（约 1-2 分钟）")
    parser.add_argument("--stocks", type=str, default=None, help="只跑指定股票（逗号分隔，如 600519,300750）")
    parser.add_argument("--no-notify", action="store_true", help="跑通流程但不推送飞书")
    parser.add_argument("--dry-run", action="store_true", help="不调 LLM 不推送，仅看数据流（最省 token）")
    parser.add_argument("--debug", action="store_true", help="多打日志")
    parser.add_argument("--skip-trading-check", action="store_true", help="跳过交易日检查（强制跑）")
    args = parser.parse_args()

    print("=" * 60)
    print(f"📰 股民简报 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.market_only:
        print("模式：仅 Layer 1 全市场态势")
    elif args.stocks:
        print(f"模式：单股深度 ({args.stocks})")
    else:
        print("模式：完整简报（Layer 1 + Layer 2）")
    if args.dry_run:
        print("⚠️ DRY-RUN: 不调 LLM 不推送")
    elif args.no_notify:
        print("⏭ 跳过飞书推送")
    print("=" * 60)

    if not args.skip_trading_check and not is_trading_day_today():
        wd = datetime.now().strftime("%A")
        print(f"\n📅 今天非 A 股交易日（{wd}），跳过本次 brief\n")
        return

    stocks_filter = [s.strip() for s in args.stocks.split(",")] if args.stocks else None

    if args.market_only:
        md, fm = run_market_review()
    elif args.stocks:
        md, fm = run_watchlist_brief(stocks_filter=stocks_filter, dry_run=args.dry_run)
    else:
        md, fm = run_full_brief(dry_run=args.dry_run)

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"brief_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    # 文件含 YAML frontmatter（方便日后批量统计）；飞书推送不含
    out_file.write_text(_render_frontmatter(fm) + md)
    print(f"\n✅ 保存（{fm['type']}）：{out_file}")

    if args.no_notify or args.dry_run:
        print("⏭ 跳过飞书推送")
    else:
        if args.market_only:
            mode_label = "🌅 早盘"
        elif args.stocks:
            mode_label = "🎯 自选股"
        else:
            mode_label = "🌆 收盘"
        push_feishu(
            title=f"{mode_label}简报 · {datetime.now().strftime('%m-%d %H:%M')}",
            content_md=md,
        )

    if args.debug:
        print("\n" + "=" * 60 + "\n" + md)


def render_brief(market=None, deep=None) -> str:
    """run_* 入口的统一渲染包装：market/deep 任一可以为 None。"""
    return render_markdown(market or {}, deep or [])


def render_markdown(market, deep) -> str:
    if market and deep:
        mode_label = "🌆 收盘简报"
    elif deep:
        mode_label = "🎯 自选股简报"
    else:
        mode_label = "🌅 早盘简报"
    out = [f"# {mode_label} · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]

    # ===== Layer 1（只在 A 股有 watchlist 时显示）=====
    if market:
        out.append("## 🌐 全市场态势\n")
        hs = market.get("hsgt_summary") or {}
        if hs:
            out.append(f"### 北向资金（{hs.get('最近交易日', '近期')}）")
            day_net = hs.get("当日净买额(亿)")
            sh = hs.get("沪股通净买(亿)")
            sz = hs.get("深股通净买(亿)")
            # 当日净买非零 → 显示完整明细
            if day_net is not None and abs(day_net) > 0.01:
                parts = [f"当日净买 **{day_net:+.2f} 亿**"]
                if sh is not None and sz is not None:
                    parts.append(f"沪 {sh:+.2f} / 深 {sz:+.2f}")
                if hs.get("上证涨幅%") is not None:
                    parts.append(f"上证 {hs['上证涨幅%']:+.2f}% / 深证 {hs.get('深证涨幅%', 0):+.2f}%")
                out.append("- " + " ｜ ".join(parts))
            else:
                # 当日为 0 → 数据未发布（周末 / 盘前 / 当日数据未结算）
                out.append("- _当日数据未发布（非交易日或盘前），累计数据见下_")
            if hs.get("5日累计净买(亿)") is not None:
                out.append(f"- 5 日累计：**{hs['5日累计净买(亿)']:+.2f} 亿** ｜ 10 日累计：**{hs.get('10日累计净买(亿)', 0):+.2f} 亿**")
            out.append("")

        lhb = market.get("lhb")
        if lhb is not None and not lhb.empty:
            indicators = lhb["指标"].value_counts().head(5).to_dict()
            ind_brief = "; ".join(f"{k} **{v}** 只" for k, v in indicators.items())
            out.append(f"### 龙虎榜温度（{YESTERDAY}）")
            out.append(f"- 共 **{len(lhb)}** 只上榜：{ind_brief}\n")

            # 个股诊断：分类成 🟢/🔴/⚪
            diagnoses = (market.get("hot_money") or {}).get("lhb_diagnose") or []
            if diagnoses:
                good = [d for d in diagnoses if d["判断"] == "🟢"]
                bad = [d for d in diagnoses if d["判断"] == "🔴"]
                neutral = [d for d in diagnoses if d["判断"] == "⚪"]
                out.append(f"### 龙虎榜诊断（Top {len(diagnoses)} 上榜股）\n")
                out.append("**判断标准**：🟢 机构净买 > 1 亿 或 知名游资买 > 5000 万 ｜ 🔴 机构净卖 > 1 亿 或 散户接盘 ｜ ⚪ 博弈不明\n")
                if good:
                    out.append(f"**🟢 看好（{len(good)} 只）**")
                    for d in good[:10]:
                        change = f" {float(d['对应值']):+.1f}%" if d.get("对应值") not in (None, "") else ""
                        out.append(f"- {d['名称']} ({d['代码']}){change} · **{d['理由']}**")
                    out.append("")
                if bad:
                    out.append(f"**🔴 看空（{len(bad)} 只）**")
                    for d in bad[:10]:
                        change = f" {float(d['对应值']):+.1f}%" if d.get("对应值") not in (None, "") else ""
                        out.append(f"- {d['名称']} ({d['代码']}){change} · **{d['理由']}**")
                    out.append("")
                if neutral:
                    sample = ", ".join(f"{d['名称']}({d['代码']})" for d in neutral[:5])
                    more = "…" if len(neutral) > 5 else ""
                    out.append(f"**⚪ 博弈不明（{len(neutral)} 只）**：{sample}{more}\n")

        # 今日全市场游资动作（Option A 聚合）—— 用 list 格式不用表格
        hm = market.get("hot_money") or {}
        top_seats = hm.get("top_seats") or []
        if top_seats:
            out.append(f"### 💰 今日资金动向（昨日 Top {hm.get('总股票数', 0)} 上榜股聚合）\n")
            buyers = sorted([s for s in top_seats if s["净额(万)"] > 0], key=lambda x: -x["净额(万)"])
            sellers = sorted([s for s in top_seats if s["净额(万)"] < 0], key=lambda x: x["净额(万)"])

            def _fmt_amt(wan):
                if abs(wan) >= 10000:
                    return f"{wan/10000:+.2f} 亿"
                return f"{wan:+.0f} 万"

            def _seat_short(name, tag):
                if tag:
                    return tag
                short = name[:18] + ("…" if len(name) > 18 else "")
                return short

            if buyers:
                out.append("**🟢 净买入 Top**")
                for s in buyers[:7]:
                    nick = _seat_short(s["席位"], s["标签"])
                    amt = _fmt_amt(s["净额(万)"])
                    if s["命中股票数"] >= 2:
                        scope = f"命中 {s['命中股票数']} 只（{', '.join(s['命中股票'][:3])}）"
                    else:
                        scope = f"重仓 {s['命中股票'][0]}" if s["命中股票"] else ""
                    out.append(f"- {nick} **{amt}** · {scope}")
                out.append("")

            if sellers:
                out.append("**🔴 净卖出 Top**")
                for s in sellers[:5]:
                    nick = _seat_short(s["席位"], s["标签"])
                    amt = _fmt_amt(s["净额(万)"])
                    if s["命中股票数"] >= 2:
                        scope = f"命中 {s['命中股票数']} 只"
                    else:
                        scope = f"重仓 {s['命中股票'][0]}" if s["命中股票"] else ""
                    out.append(f"- {nick} **{amt}** · {scope}")
                out.append("")

            # V1.5: 待命名席位（频繁活跃但无江湖名号 → 提示用户人工 vetting）
            discoveries = hm.get("待命名席位") or []
            if discoveries:
                out.append("**🔍 待命名席位**（多股活跃 + 净额显著，可能是新游资 / 机构。看到熟悉的名字请加入 `data/seats.json`）")
                for s in discoveries:
                    seat_full = s["席位"]
                    amt = _fmt_amt(s["净额(万)"])
                    scope = f"命中 {s['命中股票数']} 只（{', '.join(s['命中股票'][:3])}）"
                    out.append(f"- `{seat_full[:40]}` **{amt}** · {scope}")
                out.append("")

        # 今日强势板块 + 优质企业（由 sector.py 提供完整 Markdown）
        sector_md = market.get("sector_md")
        if sector_md:
            out.append(sector_md)
            out.append("")

    # ===== Layer 2 =====
    if not deep:
        return "\n".join(out)
    out.append("\n## 📊 自选股深度\n")
    for d in deep:
        p = d["payload"]
        market_badge = {"a": "🇨🇳", "hk": "🇭🇰", "us": "🇺🇸"}.get(d["market"], "")
        out.append(f"### {market_badge} {d['name']}（{d['code']}）\n")

        # 行情条
        k = p.get("K线技术") or {}
        if k:
            line = f"**今日**：¥{k.get('收盘')} ({k.get('当日涨跌幅%')}%) | 5 日 {k.get('5日累计涨跌幅%')}% | MA: {k.get('均线排列')} | MACD: {k.get('MACD金叉死叉')} (柱 {k.get('MACD柱')})"
            out.append(line + "\n")
        elif p.get("行情快照", {}).get("最新价"):
            q = p["行情快照"]
            out.append(f"**今日**：¥{q['最新价']} | 行业 {q.get('行业')} | 市值 {q.get('总市值(亿)')} 亿\n")

        # 主力（A 股专属）
        if d["market"] == "a":
            h = p.get("北向持股") or {}
            if h:
                out.append(f"**北向**：占 A 股 {h.get('持股占A股%')}% | 7 日累计 **{h.get('7日累计增持(亿元)')} 亿**")
            lh = p.get("龙虎榜命中") or []
            if lh:
                out.append(f"**龙虎榜命中**：" + ", ".join(x.get('指标','') for x in lh))
                seats = p.get("龙虎榜席位") or {}
                buy_rows = seats.get("买入前5") or []
                sell_rows = seats.get("卖出前5") or []
                if buy_rows:
                    parts = []
                    for r in buy_rows[:3]:
                        tag = r['标签'] or r['席位'][:15]
                        parts.append(f"{tag} +{r['买入(万)']}万")
                    out.append(f"  · 买入 Top3：{' / '.join(parts)}")
                if sell_rows:
                    parts = []
                    for r in sell_rows[:3]:
                        tag = r['标签'] or r['席位'][:15]
                        parts.append(f"{tag} -{r['卖出(万)']}万")
                    out.append(f"  · 卖出 Top3：{' / '.join(parts)}")
            dz = p.get("大宗交易") or []
            if dz:
                out.append(f"**大宗交易（{len(dz)} 笔）**：" + "; ".join(
                    f"{x.get('交易日期')} 折溢率 {float(x.get('折溢率',0))*100:.2f}% / 成交 {float(x.get('成交总额',0)):.0f} 万"
                    for x in dz[:3]
                ))
            mg = p.get("融资融券(亿元)") or {}
            if mg:
                out.append(f"**融资融券（亿元）**：" + ", ".join(f"{k}={v}" for k, v in list(mg.items())[:4]))
            gd = p.get("股东户数趋势") or {}
            if gd:
                out.append(f"**股东户数**：{gd.get('最近统计日')} {gd.get('股东户数')} 户，环比 **{gd.get('环比变化%')}%**，近 4 期环比 {gd.get('近 4 期环比%')}")
            bd = p.get("板块同业") or {}
            if bd and "未匹配到板块" not in bd:
                out.append(f"**板块**：{bd.get('板块名称')} 今日 {bd.get('板块涨跌幅%')}% | 板块涨幅 Top3：" + ", ".join(
                    f"{x['名称']} {x['涨跌幅']}%" for x in (bd.get('今日 Top3 同业') or [])
                ))
            out.append("")

        # 公告/新闻原始清单已迁移至 LLM 的"📰 公告/新闻解读"段落（带 🟢⚪🔴 标签 + 一句解读）
        anns = p.get("近 30 日公告") or []
        news = p.get("近期新闻") or []
        if anns or news:
            out.append(f"_原始：{len(anns)} 条公告 + {len(news)} 条新闻 → 见 LLM 解读_\n")

        out.append("---\n" + d["summary"] + "\n\n---\n")

    return "\n".join(out)


if __name__ == "__main__":
    main()
