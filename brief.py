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
# 著名游资 / 散户席位识别（用于龙虎榜数据增强）
# ====================================================
# 顺序：先匹配 hot money（具体到城市路名），再匹配 generic（"拉萨"/"机构专用"/"沪股通"）
SEAT_TAGS = [
    (("华泰证券", "深圳益田路"), "🔥 孙哥"),
    (("东方财富", "上海打浦路"), "🔥 章盟主"),
    (("中信证券", "上海溧阳路"), "🔥 作手新一"),
    (("国泰君安", "无锡北塘大街"), "🔥 赵老哥"),
    (("中信证券", "海宁海昌南路"), "🔥 涛哥"),
    (("财通证券", "杭州马塍路"), "🔥 小鳄鱼"),
    (("华泰证券", "深圳彩田路"), "🔥 华泰彩田路"),
    (("国泰君安", "上海江苏路"), "🔥 炒股养家"),
    (("中国国际金融", "上海分公司"), "🔥 中金上海"),
    (("华泰证券", "上海武定路"), "🔥 华泰武定路"),
    (("银河证券", "绍兴解放北路"), "🔥 银河绍兴"),
    (("拉萨",), "👤 拉萨散户"),
    (("深股通专用",), "🌏 北向（深）"),
    (("沪股通专用",), "🌏 北向（沪）"),
    (("机构专用",), "🏛 机构"),
]


def classify_seat(name: str) -> str | None:
    if not name:
        return None
    for keywords, tag in SEAT_TAGS:
        if all(kw in name for kw in keywords):
            return tag
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


def layer1_hot_money_summary(lhb_df: pd.DataFrame, date: str, top_n: int = 20) -> dict:
    """聚合昨日 Top N 上榜股的席位 → 找出最活跃的游资 / 机构 / 北向"""
    if lhb_df is None or lhb_df.empty:
        return {}
    # 按成交额排前 N，按股票代码去重（一股可能多个指标）
    top = lhb_df.sort_values("成交额", ascending=False).drop_duplicates("股票代码").head(top_n)
    print(f"  · 拉 Top {len(top)} 龙虎榜股票席位明细（聚合游资动作）...")

    seat_aggr: dict = {}
    for _, row in top.iterrows():
        code = row["股票代码"]
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
                rec = seat_aggr.setdefault(seat, {
                    "tag": classify_seat(seat),
                    "买入": 0.0, "卖出": 0.0, "股票数": 0, "股票": [],
                })
                rec["买入"] += _num(r.get("买入金额"))
                rec["卖出"] += _num(r.get("卖出金额"))
                if code not in rec["股票"]:
                    rec["股票"].append(code)
                    rec["股票数"] += 1

    rows = []
    for seat, rec in seat_aggr.items():
        net = rec["买入"] - rec["卖出"]
        rows.append({
            "席位": seat,
            "标签": rec["tag"],
            "买入(万)": round(rec["买入"] / 1e4, 1),
            "卖出(万)": round(rec["卖出"] / 1e4, 1),
            "净额(万)": round(net / 1e4, 1),
            "命中股票数": rec["股票数"],
            "命中股票": rec["股票"][:3],
        })
    # 只留有名号或命中 ≥ 2 只票的"显著"席位
    significant = [r for r in rows if r["标签"] or r["命中股票数"] >= 2]
    significant.sort(key=lambda x: abs(x["净额(万)"]), reverse=True)
    return {"top_seats": significant[:15], "总股票数": len(top)}


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
    def _fetch():
        df = ak.stock_hsgt_hist_em(symbol="北向资金")
        if df is None or df.empty:
            return {}
        recent = df.tail(5)
        valid = recent.dropna(subset=["当日成交净买额"])
        if valid.empty:
            return {"最近交易日": str(recent.iloc[-1]["日期"]), "数据状态": "近 5 日净买额数据未发布"}
        return {
            "最近交易日": str(valid.iloc[-1]["日期"]),
            "当日净买额(亿)": round(float(valid.iloc[-1]["当日成交净买额"]) / 100, 2),
            "5日累计净买(亿)": round(float(valid["当日成交净买额"].sum()) / 100, 2),
        }
    return safe("北向总览", _fetch, {})


# ====================================================
# A 股 — Layer 2 per stock
# ====================================================
def _tencent_prefix(code: str) -> str:
    """A 股代码 → 腾讯前缀：6/688/689 = sh，其他 sz"""
    return "sh" if code.startswith("6") else "sz"


def _a_quote_tencent(code: str) -> dict:
    """fallback: qt.gtimg.cn 实时盘口"""
    import requests
    sym = f"{_tencent_prefix(code)}{code}"
    r = requests.get(f"http://qt.gtimg.cn/q={sym}", timeout=8)
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


def a_quote(code: str) -> dict:
    """baostock 主源 → 腾讯 qt 兜底；基本面从 stock_meta.json 取"""
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

    out = safe(f"行情快照 {code}[baostock]", _via_baostock, {}, retries=2, backoff=1.5)
    if out.get("最新价") is not None:
        return out
    # baostock 挂了 → 腾讯
    return safe(f"行情快照 {code}[腾讯兜底]", lambda: _a_quote_tencent(code), {}, retries=2, backoff=1.5)


def _a_kline_tencent(code: str) -> dict:
    """fallback: ifzq.gtimg.cn 历史日 K（40 天 OHLCV，纯 HTTP 不走 baostock）"""
    import requests
    sym = f"{_tencent_prefix(code)}{code}"
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,40,qfq"
    r = requests.get(url, timeout=10)
    data = r.json()
    if data.get("code") != 0:
        return {}
    kdata = data.get("data", {}).get(sym, {})
    key = next((k for k in kdata if "day" in k.lower()), None)
    if not key or not kdata[key]:
        return {}
    rows = kdata[key]
    # tencent 字段：[date, open, close, high, low, volume, ...]
    df = pd.DataFrame(rows)
    df = df.iloc[:, :6]
    df.columns = ["日期", "open", "收盘", "high", "low", "volume"]
    for col in ("open", "收盘", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["涨跌幅"] = df["收盘"].pct_change() * 100
    df["成交额"] = df["收盘"] * df["volume"]  # 估算，tencent 这接口没直接给成交额
    if len(df) < 5:
        return {}
    out = _enrich_kline(df)
    out["_source"] = "tencent_ifzq"
    return out


def a_kline(code: str) -> dict:
    """baostock K 线 → 腾讯 ifzq 兜底；返回 MA + MACD + 涨跌幅"""
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

    out = safe(f"K线 {code}[baostock]", _via_baostock, {}, retries=2, backoff=2.0)
    if out and out.get("收盘") is not None:
        return out
    # baostock 挂了 → 腾讯 ifzq
    return safe(f"K线 {code}[腾讯兜底]", lambda: _a_kline_tencent(code), {}, retries=2, backoff=1.5)


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
def push_feishu(title: str, content_md: str) -> bool:
    """推送 Markdown 到飞书。每个章节(### 单股 / ## 大区块)一条独立消息，确保完整渲染不被截断。"""
    webhook = os.getenv("FEISHU_WEBHOOK_URL")
    if not webhook:
        print("⚠️  未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    sections = _split_markdown(content_md)
    total = len(sections)
    sent = 0
    for i, sec in enumerate(sections, 1):
        sec_title = f"{title} ({i}/{total})"
        if _post_one_card(webhook, sec_title, sec):
            sent += 1
        time.sleep(0.5)  # 防止飞书限频

    print(f"✅ 飞书推送：{sent}/{total} 段成功")
    return sent == total


def _post_one_card(webhook: str, title: str, body_md: str) -> bool:
    """单条飞书卡片消息"""
    # 飞书 markdown element 单个上限 ~5000 字符；超长仍截断
    if len(body_md) > 4800:
        body_md = body_md[:4800] + "\n\n... (内容截断，完整版见本地 reports/)"
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": body_md}],
        },
    }
    try:
        import requests
        r = requests.post(webhook, json=payload, timeout=10)
        result = r.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            return True
        print(f"  ⚠️  飞书推送失败：{result}")
        return False
    except Exception as e:
        print(f"  ⚠️  飞书推送异常：{type(e).__name__}: {e}")
        return False


def _split_markdown(md: str, layer1_max: int = 4500) -> list[str]:
    """切段：
    - Layer 1（全市场态势）默认 1 段；超过 layer1_max 字符则按 ### 切
    - 自选股深度里每个 ### 永远 = 1 段
    """
    lines = md.split("\n")
    layer1_lines = []
    layer2_chunks = []  # 每只股一段
    in_individual = False
    cur = []

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

    sections = []
    layer1_md = "\n".join(layer1_lines).strip()
    if layer1_md:
        if len(layer1_md) <= layer1_max:
            sections.append(layer1_md)
        else:
            # Layer 1 超长，按 ### 切
            sub = []
            for l in layer1_lines:
                if l.startswith("### ") and sub and any(x.strip() for x in sub):
                    s = "\n".join(sub).strip()
                    if s and len(s) > 30:
                        sections.append(s)
                    sub = []
                sub.append(l)
            if sub:
                s = "\n".join(sub).strip()
                if s and len(s) > 30:
                    sections.append(s)
    sections.extend(layer2_chunks)
    return sections


# ====================================================
# LLM
# ====================================================
SYS_PROMPT = """你是给我自己看的股票信息助理。基于结构化数据生成简报，**必须讲清楚因果链 + 解读公告/新闻**。

# 输出格式（严格遵守）

**📋 数据清点**（5-8 条 bullet，每条挂具体数据）
- 行情：[价格、涨跌幅、5 日累计、MA 排列、MACD]
- 主力（北向、龙虎榜、大宗、融资融券）
- 股东户数（最新户数 + 环比变化%）
- 板块同业（板块涨跌幅 + 你这只 vs 板块）
（缺失项直接写"无数据"，不要省略 bullet）

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

# 硬要求
1. 不允许编造数据，所有数字必须来自我给的输入
2. 推理链必须显式说"因为...所以..."，不能只列事实
3. 公告/新闻解读必须给出 🟢/⚪/🔴 + 一句解读，不能只重复标题
4. 信息真的不足时（多数关键字段为空）才说『信息不足，不下判断』
5. 中文输出，不要 markdown 代码块包裹"""


def llm_summarize(stock_name: str, code: str, payload: dict) -> str:
    from litellm import completion
    user_prompt = f"## {stock_name}（{code}）\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n```"
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
def gather_a(code: str, market: dict) -> dict:
    quote = a_quote(code)
    lhb_hit = a_lhb_hit(code, market["lhb"])
    payload = {
        "行情快照": quote,
        "K线技术": a_kline(code),
        "北向持股": a_hsgt(code),
        "龙虎榜命中": lhb_hit,
        "大宗交易": a_dzjy_hit(code, market["dzjy"]),
        "融资融券(亿元)": a_margin_hit(code, market["margin_sse"], market["margin_szse"]),
        "股东户数趋势": a_gdhs(code),
        "板块同业": a_board(quote.get("行业"), market["industry_list"]),
        "近 30 日公告": a_announcements(code),
        "近期新闻": a_news(code),
    }
    # 如果上龙虎榜，加拉该股席位明细
    if lhb_hit:
        payload["龙虎榜席位"] = a_lhb_seats(code, YESTERDAY)
    return payload


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


def main():
    print("=" * 60)
    print(f"📰 股民简报 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    if not is_trading_day_today():
        wd = datetime.now().strftime("%A")
        print(f"\n📅 今天非 A 股交易日（{wd}），跳过本次 brief\n")
        return

    # A 股需要才拉公共表
    needs_a = any(s.get("market", "a") == "a" for s in WATCHLIST)
    market = fetch_market_tables() if needs_a else {}

    deep = []
    for i, stock in enumerate(WATCHLIST, 1):
        code = stock["code"]
        name = stock["name"]
        mkt = stock.get("market", "a")
        t0 = time.time()
        print(f"\n  [{i}/{len(WATCHLIST)}] [{mkt.upper()}] {name}({code}) 抓数据...")
        if mkt == "a":
            payload = gather_a(code, market)
        elif mkt == "hk":
            payload = gather_hk(code)
        elif mkt == "us":
            payload = gather_us(code)
        else:
            payload = {}
        t1 = time.time()
        print(f"    数据拿到（耗时 {t1-t0:.1f}s），调 LLM...")
        try:
            summary = llm_summarize(name, code, payload)
        except Exception as e:
            summary = f"⚠️ LLM 失败: {e}"
        print(f"    ✅ LLM 完成（耗时 {time.time()-t1:.1f}s）")
        deep.append({"code": code, "name": name, "market": mkt, "payload": payload, "summary": summary})

    md = render_markdown(market, deep)
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"brief_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out_file.write_text(md)
    print(f"\n✅ 保存：{out_file}")

    # 推送飞书（失败不阻塞）
    push_feishu(
        title=f"股民简报 · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        content_md=md,
    )

    print("\n" + "=" * 60 + "\n" + md)


def render_markdown(market, deep) -> str:
    out = [f"# 股民简报 · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]

    # ===== Layer 1（只在 A 股有 watchlist 时显示）=====
    if market:
        out.append("## 🌐 全市场态势\n")
        hs = market.get("hsgt_summary") or {}
        out.append("### 北向资金")
        if hs:
            if "数据状态" in hs:
                out.append(f"- 最近交易日 {hs['最近交易日']}（{hs['数据状态']}）\n")
            else:
                out.append(f"- {hs['最近交易日']} 当日净买 **{hs['当日净买额(亿)']} 亿** | 5 日累计 **{hs['5日累计净买(亿)']} 亿**\n")
        else:
            out.append("（无数据）\n")

        lhb = market.get("lhb")
        if lhb is not None and not lhb.empty:
            # 市场温度：统计触发条件
            indicators = lhb["指标"].value_counts().head(5).to_dict()
            ind_brief = "; ".join(f"{k} **{v}** 只" for k, v in indicators.items())
            out.append(f"### 龙虎榜温度（{YESTERDAY}）")
            out.append(f"- 共 **{len(lhb)}** 只上榜：{ind_brief}\n")

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

        # 今日强势板块 + 优质企业（由 sector.py 提供完整 Markdown）
        sector_md = market.get("sector_md")
        if sector_md:
            out.append(sector_md)
            out.append("")

    # ===== Layer 2 =====
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
