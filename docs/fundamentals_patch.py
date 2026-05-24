"""
Drop-in patch for sector.py::fetch_fundamentals

Bug: `run_sector_module` 从 brief.py:fetch_market_tables 调起时，baostock
还没 login（_bs_login 在 line 270 才被首次触发），所以 bs.query_profit_data
全部返回 0 行，函数返回 {}，候选股 fundamental 字段全空。

Fix:
  1) 函数入口 lazy-login baostock（模块级 flag 防重复 login）
  2) baostock 拉空时，兜底切 akshare.stock_financial_abstract，保持出参 schema
     不变（{报告期, 净利润(亿), 营收(亿), roe%, 净利率%, 净利润同比%}）

直接整段替换 sector.py 第 290-351 行（从 `def _bs_code` 上方的 baostock 注释
块开始，到 `fetch_fundamentals` 函数结尾）即可。如果嫌粒度大，最小改动版本
就是把 _ensure_bs_login() 加到原函数第一行。
"""

from __future__ import annotations

from datetime import datetime
import baostock as bs


# ====================================================
# 基本面：baostock profit + growth（akshare 兜底）
# ====================================================

_BS_FUND_LOGGED_IN = False


def _ensure_bs_login() -> None:
    """Lazy login，幂等。brief.py 的 _bs_login 是私有 flag，这里独立维护一份避免耦合。"""
    global _BS_FUND_LOGGED_IN
    if _BS_FUND_LOGGED_IN:
        return
    lg = bs.login()
    # 即便 login 失败也别抛，让后续 query 自然返回空，由 akshare 兜底
    if lg.error_code == "0":
        _BS_FUND_LOGGED_IN = True


def _bs_code(code: str) -> str:
    return f"sh.{code}" if code.startswith("6") else f"sz.{code}"


def _fetch_via_baostock(code: str) -> dict:
    """主源。返回 dict（可能部分字段为 None）；完全失败返回 {}."""
    bsc = _bs_code(code)
    out: dict = {}
    last_year = datetime.now().year - 1

    # profit
    profit_row = None
    for y, q in [(last_year, 4), (last_year + 1, 1), (last_year, 3), (last_year, 2), (last_year, 1)]:
        try:
            rs = bs.query_profit_data(code=bsc, year=y, quarter=q)
        except Exception:
            continue
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rows:
            profit_row = (rows[0], y, q)
            break
    if not profit_row:
        return out

    r, y, q = profit_row
    # fields: code,pubDate,statDate,roeAvg,npMargin,gpMargin,netProfit,epsTTM,MBRevenue,totalShare,liqaShare
    try:
        out["报告期"] = f"{y}Q{q}"
        out["pubDate"] = r[1]
        out["roe%"] = round(float(r[3]) * 100, 2) if r[3] else None
        out["净利率%"] = round(float(r[4]) * 100, 2) if r[4] else None
        out["毛利率%"] = round(float(r[5]) * 100, 2) if r[5] else None
        out["净利润(亿)"] = round(float(r[6]) / 1e8, 2) if r[6] else None
        out["营收(亿)"] = round(float(r[8]) / 1e8, 2) if r[8] else None
    except (ValueError, IndexError):
        pass

    # growth — 同周期
    try:
        rg = bs.query_growth_data(code=bsc, year=y, quarter=q)
        rows = []
        while rg.error_code == "0" and rg.next():
            rows.append(rg.get_row_data())
        if rows:
            r = rows[0]
            # fields: code,pubDate,statDate,YOYEquity,YOYAsset,YOYNI,YOYEPSBasic,YOYPNI
            out["净利润同比%"] = round(float(r[5]) * 100, 2) if r[5] else None
            out["归母净利润同比%"] = round(float(r[7]) * 100, 2) if r[7] else None
    except Exception:
        pass

    return out


def _fetch_via_akshare(code: str) -> dict:
    """兜底源。akshare 同花顺财务摘要，按列名 'YYYYMMDD' 取最近一期。"""
    try:
        import akshare as ak
        df = ak.stock_financial_abstract(symbol=code)
    except Exception:
        return {}
    if df is None or df.empty:
        return {}

    # 第 0/1 列是 ['选项','指标']，后面是日期列。挑最近一期（同时要求净利润有值）。
    meta_cols = ["选项", "指标"]
    date_cols = [c for c in df.columns if str(c).isdigit() and len(str(c)) == 8]
    if not date_cols:
        return {}
    date_cols.sort(reverse=True)  # 最新日期在前

    def _pick(indicator: str, date_col: str):
        row = df[df["指标"] == indicator]
        if row.empty:
            return None
        v = row.iloc[0][date_col]
        try:
            v = float(v)
            if v != v:  # NaN
                return None
            return v
        except (ValueError, TypeError):
            return None

    # 找最近一期"归母净利润"非空的日期当 statDate
    stat_date = None
    np_val = None
    for dc in date_cols:
        v = _pick("归母净利润", dc) or _pick("净利润", dc)
        if v is not None:
            stat_date = dc
            np_val = v
            break
    if stat_date is None:
        return {}

    rev = _pick("营业总收入", stat_date) or _pick("营业收入", stat_date)
    roe = _pick("净资产收益率(ROE)", stat_date) or _pick("加权净资产收益率", stat_date)
    npm = _pick("销售净利率", stat_date)

    # 同比：找去年同期，算 (本期 - 去年) / |去年|
    yoy = None
    same_period_last_year = f"{int(stat_date[:4]) - 1}{stat_date[4:]}"
    if same_period_last_year in df.columns.astype(str).tolist():
        prev = _pick("归母净利润", same_period_last_year) or _pick("净利润", same_period_last_year)
        if prev not in (None, 0):
            yoy = round((np_val - prev) / abs(prev) * 100, 2)

    out = {
        "报告期": f"{stat_date[:4]}-{stat_date[4:6]}-{stat_date[6:]}",
        "pubDate": None,
        "净利润(亿)": round(np_val / 1e8, 2) if np_val is not None else None,
        "营收(亿)": round(rev / 1e8, 2) if rev is not None else None,
        "roe%": round(roe, 2) if roe is not None else None,
        "净利率%": round(npm, 2) if npm is not None else None,
        "净利润同比%": yoy,
    }
    return out


def fetch_fundamentals(code: str) -> dict:
    """
    最新年报（或可用最近季报）的 ROE / 净利润 / 净利润同比。

    主源：baostock profit + growth（季度颗粒，5 月起多数 2025 年报已披露）
    兜底：akshare.stock_financial_abstract（同花顺源，按列名最近一期）

    出参 schema（与原函数兼容）：
      {报告期, pubDate, 净利润(亿), 营收(亿), roe%, 净利率%, 净利润同比%, 归母净利润同比%, 毛利率%}

    Bug fix: 函数入口 lazy-login baostock，避免 sector pipeline 在
    brief.py 的 _bs_login 之前被调用时，bs.query_* 全部返回空。
    """
    _ensure_bs_login()

    out = _fetch_via_baostock(code)
    # 主源至少有"净利润"才算成功；否则切兜底
    if out.get("净利润(亿)") is not None:
        return out

    fallback = _fetch_via_akshare(code)
    if fallback:
        fallback["_source"] = "akshare_fallback"
        return fallback
    return out  # 全失败就返回 baostock 拉到的（可能为空）


# ====================================================
# 自检：5 只样本股，要求全部至少拿到 ROE + 净利润 + 同比
# 运行：cd stock-briefing && .venv/bin/python docs/fundamentals_patch.py
# ====================================================
if __name__ == "__main__":
    samples = [
        ("600519", "贵州茅台"),
        ("002475", "立讯精密"),
        ("600183", "生益科技"),
        ("600176", "中国巨石"),
        ("002001", "新和成"),
    ]
    ok = 0
    for code, name in samples:
        d = fetch_fundamentals(code)
        has_core = d.get("roe%") is not None and d.get("净利润(亿)") is not None and d.get("净利润同比%") is not None
        flag = "OK" if has_core else "MISS"
        print(f"[{flag}] {code} {name} → 报告期={d.get('报告期')} 净利润={d.get('净利润(亿)')}亿 ROE={d.get('roe%')}% 同比={d.get('净利润同比%')}% src={d.get('_source', 'baostock')}")
        if has_core:
            ok += 1
    print(f"\n{ok}/{len(samples)} 通过")
