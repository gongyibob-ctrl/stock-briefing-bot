"""
brief.py 的 a_quote / a_kline 替换补丁 — 用 baostock 替代东方财富。

直接复制 _bs_login / _bs_code / a_quote / a_kline / _enrich_kline 到 brief.py，
覆盖原同名函数；删掉 brief.py 顶部 import akshare as ak 中 a_quote/a_kline 用到的部分（其它接口仍用 ak）。

依赖：pip install baostock
登录：模块加载时自动 login，atexit 自动 logout。

保持原有返回 schema 不变 → 下游 _enrich_kline / LLM prompt 完全兼容。
"""
from __future__ import annotations

import atexit
import json
from datetime import datetime, timedelta
from pathlib import Path

import baostock as bs
import pandas as pd

# ---- 进程级 baostock session ----
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
    """600519 → sh.600519 ; 300750 → sz.300750"""
    code = code.strip()
    return f"sh.{code}" if code.startswith("6") else f"sz.{code}"


# ---- 静态基本面缓存 ----
# 总市值/流通市值/行业 baostock 没有，且每日基本不变。
# 用 watchlist.json 同目录的 stock_meta.json 静态写死即可。自用工具不需要动态拉。
_META_CACHE: dict | None = None


def _load_meta() -> dict:
    global _META_CACHE
    if _META_CACHE is not None:
        return _META_CACHE
    meta_path = Path(__file__).parent / "stock_meta.json"
    if meta_path.exists():
        _META_CACHE = json.loads(meta_path.read_text())
    else:
        _META_CACHE = {}
    return _META_CACHE


# ====================================================
# 替换后的 a_quote
# ====================================================
def a_quote(code: str) -> dict:
    """最新价 + 总市值/流通市值/行业（基本面从 stock_meta.json 取）"""
    def _fetch():
        _bs_login()
        # 取最近 3 个交易日的日 K，最后一根 = 最新已收盘价
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            _bs_code(code),
            "date,close,pctChg",
            start_date=start, end_date=end,
            frequency="d", adjustflag="3",
        )
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return {}
        last_close = float(rows[-1][1])
        meta = _load_meta().get(code, {})
        return {
            "最新价": last_close,
            "总市值(亿)": meta.get("总市值(亿)"),
            "流通市值(亿)": meta.get("流通市值(亿)"),
            "行业": meta.get("行业"),
        }
    return safe(f"行情快照 {code}", _fetch, {}, retries=3, backoff=2.0)


# ====================================================
# 替换后的 a_kline
# ====================================================
def a_kline(code: str) -> dict:
    """近 ~30 个交易日 K 线 → MA5/10/20 + MACD + 涨跌幅 + 成交额"""
    def _fetch():
        _bs_login()
        end = datetime.now().strftime("%Y-%m-%d")
        # 自然日 50 天 ≈ 35 交易日，够算 MA20
        start = (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            _bs_code(code),
            "date,open,high,low,close,volume,amount,pctChg",
            start_date=start, end_date=end,
            frequency="d", adjustflag="3",   # 3 = 不复权
        )
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        if not rows or len(rows) < 5:
            return {}
        df = pd.DataFrame(rows, columns=rs.fields)
        # baostock 返回都是字符串
        for col in ("open", "high", "low", "close", "volume", "amount", "pctChg"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        # 用与原代码一致的列名（中文），让 _enrich_kline 不用改
        df = df.rename(columns={
            "date": "日期", "close": "收盘", "amount": "成交额", "pctChg": "涨跌幅",
        })
        return _enrich_kline(df)
    return safe(f"K线 {code}", _fetch, {}, retries=3, backoff=2.5)


# _enrich_kline / _ma_pattern / _macd_cross 不用改 —— 它们只依赖
# df["收盘"] df["日期"] df["涨跌幅"] df["成交额"] 这几列，命名已对齐。
