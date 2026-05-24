"""
brief.py Layer 1 追加模块：今日最强行业板块 + 板块内优质股 LLM 评分

整体思路（混合方案 C：规则粗筛 + LLM 精选）：
  1. 同花顺 q.10jqka.com.cn 直接 HTTP（curl_cffi 模拟浏览器 TLS 指纹）拉行业板块涨幅排序
     —— 一次请求拿到全市场涨幅 Top 50 板块，含净流入、上涨家数、领涨股
  2. 取 Top N 板块，每个板块拉详情页（默认按涨跌幅 desc 的 Top 20 成分股）
  3. 规则粗筛：流通市值 ≥ 30 亿、不亏损（PE 显示数字而非 "亏"）、当日上涨
  4. baostock 补 ROE / 净利润 / 净利润同比（最近年报或最新季报）
  5. LLM 综合判断：在每个板块给出 Top 3 优质股 + 理由

数据源选型说明见 docs/sector_strength_eval_2026-05-22.md

依赖：
    pip install curl_cffi baostock
brief.py 顶部已有 baostock；仅需新增 curl_cffi（pip install curl_cffi）

调用方式（在 brief.py 主流程 fetch_market_tables() 之后）：
    sectors = layer1_top_sectors(top_n=5)
    for s in sectors:
        s["top_quality"] = top_quality_stocks_in_sector(s)
    sector_section_md = format_sector_section(sectors)
    # 把 sector_section_md 拼到 Layer 1 模块末尾

返回 schema 见各函数 docstring。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta

import pandas as pd

# baostock 复用 brief.py 里的 _bs_login / _bs_code（patch 时直接调用即可）
import baostock as bs


# ====================================================
# 数据源 1: 同花顺行业板块（主源）
# ====================================================
_THS_BASE = "http://q.10jqka.com.cn/thshy"


def _ths_session():
    """curl_cffi 模拟 chrome120 TLS 指纹 —— 普通 requests 会被 401/403"""
    from curl_cffi import requests as creq
    s = creq.Session(impersonate="chrome120")
    s.headers.update({"Referer": "http://q.10jqka.com.cn/thshy/"})
    return s


def _ths_parse_table(html: str) -> list[list[str]]:
    """通用：从 <tbody> 里抽 <tr> -> cells list"""
    m = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.S)
    if not m:
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        cells = [re.sub(r"<[^>]+>", " ", td).strip() for td in tds]
        rows.append(cells)
    return rows


def _parse_money(s: str) -> float | None:
    """'2150.27亿' → 2150.27 ; '15.32万' → 0.00153 ; 'N/A' → None"""
    if not s or s in ("--", "-", "亏", "N/A"):
        return None
    s = s.strip()
    try:
        if s.endswith("亿"):
            return float(s[:-1])
        if s.endswith("万"):
            return float(s[:-1]) / 10000
        return float(s)
    except ValueError:
        return None


def _parse_pct(s: str) -> float | None:
    if not s or s in ("--", "-"):
        return None
    try:
        return float(s.rstrip("%"))
    except ValueError:
        return None


def _ths_get_with_retry(session, url: str, max_retries: int = 4, base_sleep: float = 2.5):
    """同花顺会间歇 401/403 限频。指数退避重试。
    成功返回 r；全失败返回最后一次 r（让调用方看 status_code）。
    """
    r = None
    for i in range(max_retries):
        try:
            r = session.get(url, timeout=10)
        except Exception as e:
            print(f"  ⚠️ ths fetch err #{i}: {type(e).__name__}: {e}")
            time.sleep(base_sleep * (2 ** i))
            continue
        if r.status_code == 200:
            return r
        # 401/403 是限频
        if r.status_code in (401, 403):
            wait = base_sleep * (2 ** i)
            print(f"  ⚠️ ths {r.status_code} 限频，{wait:.1f}s 后重试 ({i+1}/{max_retries})")
            time.sleep(wait)
            continue
        time.sleep(base_sleep)
    return r


def fetch_ths_top_sectors(top_n: int = 5) -> list[dict]:
    """
    同花顺行业板块涨幅排序 page1（已是全市场前 50）
    返回：
      [{"code": "881270", "name": "元件", "pct": 7.85,
        "net_inflow_亿": 165.32, "up_count": 61, "down_count": 1,
        "leader_name": "强达电路", "leader_pct": 20.00,
        "url": ".../detail/code/881270/"}, ...]
    """
    s = _ths_session()
    url = f"{_THS_BASE}/index/field/199112/order/desc/page/1/ajax/1/"
    r = _ths_get_with_retry(s, url)
    if not r or r.status_code != 200:
        return []
    text = r.content.decode("gbk", errors="replace")
    body_m = re.search(r"<tbody[^>]*>(.*?)</tbody>", text, re.S)
    if not body_m:
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body_m.group(1), re.S):
        href_m = re.search(r'href="(http://q\.10jqka\.com\.cn/thshy/detail/code/(\d+)/)"', tr)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        cells = [re.sub(r"<[^>]+>", " ", td).strip() for td in tds]
        # cells: [序号, 板块, 涨跌幅, 成交量(万手), 成交额(亿), 净流入(亿), 上涨家数, 下跌家数, 均价, 领涨股, 领涨股最新价, 领涨股涨幅]
        if not href_m or len(cells) < 12:
            continue
        out.append({
            "code": href_m.group(2),
            "name": cells[1],
            "pct": _parse_pct(cells[2]),
            "net_inflow_亿": _parse_money(cells[5]),
            "up_count": int(cells[6]) if cells[6].isdigit() else None,
            "down_count": int(cells[7]) if cells[7].isdigit() else None,
            "leader_name": cells[9],
            "leader_pct": _parse_pct(cells[11]),
            "url": href_m.group(1),
        })
        if len(out) >= top_n:
            break
    return out


def fetch_ths_sector_cons(sector_code: str) -> list[dict]:
    """
    某个板块的成分股（默认 page1 是按涨跌幅 desc 的 Top 20）
    返回：
      [{"code", "name", "price", "pctChg", "turnover", "vol_ratio",
        "amplitude", "amount_亿", "float_mktcap_亿", "pe"}, ...]
    """
    s = _ths_session()
    url = f"{_THS_BASE}/detail/code/{sector_code}/"
    r = _ths_get_with_retry(s, url)
    if not r or r.status_code != 200:
        return []
    text = r.content.decode("gbk", errors="replace")
    rows = _ths_parse_table(text)
    out = []
    for cells in rows:
        # thead: 序号, 代码, 名称, 现价, 涨跌幅, 涨跌, 涨速, 换手, 量比, 振幅, 成交额, 流通股, 流通市值, 市盈率
        if len(cells) < 14 or not cells[1].isdigit():
            continue
        out.append({
            "code": cells[1],
            "name": cells[2],
            "price": _parse_money(cells[3]),
            "pctChg": _parse_pct(cells[4]),
            "turnover_pct": _parse_pct(cells[7]),
            "vol_ratio": _parse_money(cells[8]),
            "amplitude_pct": _parse_pct(cells[9]),
            "amount_亿": _parse_money(cells[10]),
            "float_mktcap_亿": _parse_money(cells[12]),
            "pe": _parse_money(cells[13]),  # 亏损时 cells[13]=='亏' → None
        })
    return out


# ====================================================
# 数据源 2: 新浪申万一级聚合（备用源 — 仅在同花顺挂时启用）
# ====================================================
# 申万一级 31 个固定节点（来自 Sina Market_Center.getHQNodes）
_SINA_SW1 = [
    ("美容护理", "sw1_770000"), ("环保", "sw1_760000"), ("石油石化", "sw1_750000"),
    ("煤炭", "sw1_740000"), ("通信", "sw1_730000"), ("传媒", "sw1_720000"),
    ("计算机", "sw1_710000"), ("国防军工", "sw1_650000"), ("机械设备", "sw1_640000"),
    ("电力设备", "sw1_630000"), ("建筑装饰", "sw1_620000"), ("建筑材料", "sw1_610000"),
    ("综合", "sw1_510000"), ("非银金融", "sw1_490000"), ("银行", "sw1_480000"),
    ("社会服务", "sw1_460000"), ("商贸零售", "sw1_450000"), ("房地产", "sw1_430000"),
    ("交通运输", "sw1_420000"), ("公用事业", "sw1_410000"), ("医药生物", "sw1_370000"),
    ("轻工制造", "sw1_360000"), ("纺织服饰", "sw1_350000"), ("食品饮料", "sw1_340000"),
    ("家用电器", "sw1_330000"), ("汽车", "sw1_280000"), ("电子", "sw1_270000"),
    ("有色金属", "sw1_240000"), ("钢铁", "sw1_230000"), ("基础化工", "sw1_220000"),
    ("农林牧渔", "sw1_110000"),
]


def fetch_sina_top_sectors(top_n: int = 5, max_pages_per_sector: int = 2) -> list[dict]:
    """
    备用源：用新浪逐节点拉成分股，本地按市值加权聚合板块涨幅。
    较慢（~30s 拉完 31 个申万一级），且密集请求触发 IP 封 5-60 分钟，慎用。
    """
    import requests as _r
    s = _r.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    results = []
    for name, code in _SINA_SW1:
        stocks = []
        for page in range(1, max_pages_per_sector + 1):
            try:
                r = s.get(
                    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                    params={"page": page, "num": 80, "sort": "changepercent",
                            "asc": 0, "node": code, "_s_r_a": "page"},
                    timeout=10,
                )
                data = json.loads(r.text)
            except Exception:
                break
            if not data:
                break
            stocks.extend(data)
            if len(data) < 80:
                break
            time.sleep(0.3)
        # 过滤异常：N 新股（"N+中文"如 N嘉德）、C 次新、ST、退市、停牌、当日涨幅 > 25%（非创业板/科创板的妖股）
        clean = []
        for x in stocks:
            nm = (x.get("name") or "").strip()
            # N/C 开头新股识别：第一个字符是 N 或 C，第二个字符是中文
            if len(nm) >= 2 and nm[0] in ("N", "C") and "一" <= nm[1] <= "龥":
                continue
            if "ST" in nm.upper() or "退" in nm:
                continue
            try:
                if float(x.get("volume", 0)) == 0:
                    continue
                # 异常涨幅 > 25%（非创业板/科创板 20cm 也容忍）
                pc = float(x.get("changepercent", 0))
                if pc > 25 or pc < -25:
                    continue
            except (ValueError, TypeError):
                continue
            clean.append(x)
        if not clean:
            continue
        try:
            chgs = [float(x["changepercent"]) for x in clean if x.get("changepercent") is not None]
            weights = [float(x.get("mktcap", 0)) for x in clean]
            avg = sum(chgs) / len(chgs)
            wavg = (sum(c * w for c, w in zip(chgs, weights)) / sum(weights)) if sum(weights) > 0 else avg
            up = sum(1 for c in chgs if c > 0)
            down = sum(1 for c in chgs if c < 0)
            # 领涨：流通市值 nmc (万) >= 30 亿
            cand = [x for x in clean if float(x.get("nmc", 0)) >= 300000]
            leader = max(cand or clean, key=lambda x: float(x.get("changepercent", 0)))
        except Exception:
            continue
        results.append({
            "code": code, "name": name,
            "pct": round(wavg, 2),
            "等权涨幅%": round(avg, 2),
            "net_inflow_亿": None,  # 新浪没此字段
            "up_count": up, "down_count": down,
            "leader_name": leader.get("name"),
            "leader_pct": round(float(leader.get("changepercent", 0)), 2),
            "url": None,
            "_source": "sina_sw1",
            "_cons_cache": clean,  # 备用源在这一步就拉到了成分股，缓存避免重复请求
        })
    results.sort(key=lambda x: x["pct"], reverse=True)
    return results[:top_n]


# ====================================================
# 基本面：baostock profit + growth
# ====================================================
def _bs_code(code: str) -> str:
    return f"sh.{code}" if code.startswith("6") else f"sz.{code}"


# ====================================================
# 基本面（lazy-login + akshare 兜底；修 session bug）
# ====================================================
_BS_FUND_LOGGED_IN = False


def _ensure_bs_login() -> None:
    global _BS_FUND_LOGGED_IN
    if _BS_FUND_LOGGED_IN:
        return
    lg = bs.login()
    if lg.error_code == "0":
        _BS_FUND_LOGGED_IN = True


def _fetch_via_baostock(code: str) -> dict:
    bsc = _bs_code(code)
    out: dict = {}
    last_year = datetime.now().year - 1
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
    try:
        rg = bs.query_growth_data(code=bsc, year=y, quarter=q)
        rows = []
        while rg.error_code == "0" and rg.next():
            rows.append(rg.get_row_data())
        if rows:
            r = rows[0]
            out["净利润同比%"] = round(float(r[5]) * 100, 2) if r[5] else None
            out["归母净利润同比%"] = round(float(r[7]) * 100, 2) if r[7] else None
    except Exception:
        pass
    return out


def _fetch_via_akshare(code: str) -> dict:
    try:
        import akshare as ak
        df = ak.stock_financial_abstract(symbol=code)
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    date_cols = [c for c in df.columns if str(c).isdigit() and len(str(c)) == 8]
    if not date_cols:
        return {}
    date_cols.sort(reverse=True)

    def _pick(indicator, dc):
        row = df[df["指标"] == indicator]
        if row.empty:
            return None
        v = row.iloc[0][dc]
        try:
            v = float(v)
            return None if v != v else v
        except (ValueError, TypeError):
            return None

    stat_date, np_val = None, None
    for dc in date_cols:
        v = _pick("归母净利润", dc) or _pick("净利润", dc)
        if v is not None:
            stat_date, np_val = dc, v
            break
    if stat_date is None:
        return {}
    rev = _pick("营业总收入", stat_date) or _pick("营业收入", stat_date)
    roe = _pick("净资产收益率(ROE)", stat_date) or _pick("加权净资产收益率", stat_date)
    npm = _pick("销售净利率", stat_date)
    yoy = None
    same_prev = f"{int(stat_date[:4]) - 1}{stat_date[4:]}"
    if same_prev in df.columns.astype(str).tolist():
        prev = _pick("归母净利润", same_prev) or _pick("净利润", same_prev)
        if prev not in (None, 0):
            yoy = round((np_val - prev) / abs(prev) * 100, 2)
    return {
        "报告期": f"{stat_date[:4]}-{stat_date[4:6]}-{stat_date[6:]}",
        "pubDate": None,
        "净利润(亿)": round(np_val / 1e8, 2) if np_val is not None else None,
        "营收(亿)": round(rev / 1e8, 2) if rev is not None else None,
        "roe%": round(roe, 2) if roe is not None else None,
        "净利率%": round(npm, 2) if npm is not None else None,
        "净利润同比%": yoy,
    }


def fetch_fundamentals(code: str) -> dict:
    """ROE / 净利润 / 同比 — baostock 主源 + akshare 兜底；带 lazy-login 修 session bug"""
    _ensure_bs_login()
    out = _fetch_via_baostock(code)
    if out.get("净利润(亿)") is not None:
        return out
    fallback = _fetch_via_akshare(code)
    if fallback:
        fallback["_source"] = "akshare_fallback"
        return fallback
    return out


# ====================================================
# 板块内优质股粗筛 + 基本面补全
# ====================================================
def top_quality_stocks_in_sector(
    sector: dict,
    pool_size: int = 8,
    min_float_mktcap_亿: float = 30.0,
) -> list[dict]:
    """
    给定板块，返回经过粗筛 + 基本面补全的候选股池（给 LLM 排序用）

    入参：
      sector: fetch_ths_top_sectors() 返回的单个 dict（含 code、name）
      pool_size: 给 LLM 的候选池大小（建议 5-8）
      min_float_mktcap_亿: 流通市值下限，避开微盘股

    出参：每只股票一个 dict，含同花顺成分股字段 + baostock 财务字段
      [{"code", "name", "pctChg", "float_mktcap_亿", "pe",
        "净利润(亿)", "roe%", "净利润同比%", "营收(亿)", ...}, ...]

    粗筛规则：
      1. 流通市值 ≥ min_float_mktcap_亿（剔微盘）
      2. PE 显示数字（剔亏损股；亏损 PE 字段返回 None）
      3. 当日上涨（板块是强势板块，逻辑上龙头应该跟着涨）
      4. 按流通市值降序取前 pool_size 只
    """
    if not sector or not sector.get("code"):
        return []
    # 新浪备用源在 fetch_sina_top_sectors 里已经把成分股缓存在 _cons_cache 了，直接转换字段
    if sector.get("_source") == "sina_sw1" and sector.get("_cons_cache"):
        cons = []
        for x in sector["_cons_cache"]:
            try:
                # 新浪 mktcap / nmc 单位是万元
                cons.append({
                    "code": x.get("code"),
                    "name": x.get("name"),
                    "price": float(x.get("trade") or 0),
                    "pctChg": float(x.get("changepercent") or 0),
                    "turnover_pct": float(x.get("turnoverratio") or 0),
                    "vol_ratio": None,  # 新浪没量比
                    "amplitude_pct": None,
                    "amount_亿": float(x.get("amount") or 0) / 1e8,
                    "float_mktcap_亿": float(x.get("nmc") or 0) / 1e4,
                    "pe": float(x.get("per") or 0) or None,
                })
            except (ValueError, TypeError):
                continue
    else:
        cons = fetch_ths_sector_cons(sector["code"])
    if not cons:
        return []
    filtered = [
        x for x in cons
        if (x["float_mktcap_亿"] or 0) >= min_float_mktcap_亿
        and x["pe"] is not None
        and x["pe"] > 0
        and (x["pctChg"] or 0) > 0
    ]
    # 兜底：如果硬规则过严（小板块或弱反弹日）筛不出，放宽：去掉"当日上涨"约束
    if len(filtered) < 3:
        filtered = [
            x for x in cons
            if (x["float_mktcap_亿"] or 0) >= min_float_mktcap_亿
            and x["pe"] is not None
            and x["pe"] > 0
        ]
    filtered.sort(key=lambda x: x["float_mktcap_亿"] or 0, reverse=True)
    pool = filtered[:pool_size]
    # 补基本面（baostock）
    for x in pool:
        try:
            x["fundamental"] = fetch_fundamentals(x["code"])
        except Exception as e:
            x["fundamental"] = {"_error": f"{type(e).__name__}: {e}"}
    return pool


# ====================================================
# LLM 评分（混合方案 C 的"精选"环节）
# ====================================================
SECTOR_LLM_SYS = """你是给我自己看的 A 股板块分析助理。给你一个今日强势板块的候选股池（已用流通市值/PE/上涨过粗筛），请从里面挑出 **质地最好的 3 只**，并显式说明取舍依据。

# 判断维度（重要性递减）
1. **盈利规模**：净利润绝对值（亿元），同板块内 5 亿 vs 0.5 亿是数量级差异
2. **盈利质量**：ROE（>15% 优秀，10-15% 良好，<8% 偏弱）、净利率
3. **成长性**：净利润同比（>30% 高成长，10-30% 稳健，负值警惕）
4. **估值合理性**：PE 跟同板块/同业相比是否过贵（>100 谨慎，但高成长股可宽容）
5. **流通市值**：避开 < 50 亿微盘（已粗筛过），300-2000 亿优先（流动性好）

# 输出格式（严格遵守）

**🏆 板块 Top 3**

1. **[股票名称]([代码])** — [一句结论 ≤30 字]
   - 净利润 X 亿（同比 Y%）| ROE Z% | PE W
   - **入选理由**：[显式因果，一句话 ≤80 字，引用上面数据]

2. ... 同格式
3. ... 同格式

**🚫 PASS 名单**（板块里看似涨幅好但质地一般的）
- [名称]：[一句理由，引用数据，≤40 字]
（如果候选池都不错，写"候选池整体质地均衡"）

# 硬要求
- 严禁编造数据，只引用 input JSON 里的数字
- 给的是规则筛过后的池子（已去微盘+亏损），不要再说"避开亏损"这种废话
- 中文输出，不要 markdown 代码块包裹"""


def llm_pick_top3(sector: dict, candidates: list[dict]) -> str:
    """让 LLM 在 candidates 里挑 Top 3 + 给理由。失败返回标记字符串，不抛异常。"""
    import litellm
    if not candidates:
        return ""
    # 精简 payload —— 只保留 LLM 需要的字段，省 token
    slim = []
    for x in candidates:
        f = x.get("fundamental") or {}
        slim.append({
            "code": x["code"], "name": x["name"],
            "今日涨幅%": x.get("pctChg"),
            "流通市值(亿)": x.get("float_mktcap_亿"),
            "PE": x.get("pe"),
            "换手%": x.get("turnover_pct"),
            "量比": x.get("vol_ratio"),
            "报告期": f.get("报告期"),
            "净利润(亿)": f.get("净利润(亿)"),
            "营收(亿)": f.get("营收(亿)"),
            "ROE%": f.get("roe%"),
            "净利率%": f.get("净利率%"),
            "净利润同比%": f.get("净利润同比%"),
        })
    user = (
        f"## 板块：{sector['name']} (今日 +{sector['pct']}%)\n\n"
        f"候选股池（已按流通市值排序）：\n```json\n"
        f"{json.dumps(slim, ensure_ascii=False, indent=2)}\n```"
    )
    try:
        resp = litellm.completion(
            model=os.getenv("LLM_MODEL", "deepseek/deepseek-chat"),
            api_base=os.getenv("LLM_BASE_URL"),
            api_key=os.getenv("LLM_API_KEY"),
            messages=[
                {"role": "system", "content": SECTOR_LLM_SYS},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=1800,  # 3 个 Top 加 PASS 名单，~600 字中文 ≈ 1500 token，留余量
            timeout=60,
        )
        content = (resp.choices[0].message.content or "").strip()
        return content or "⚠️ LLM 返回空"
    except Exception as e:
        return f"⚠️ LLM 失败: {type(e).__name__}: {e}"


# ====================================================
# Layer 1 入口 + Markdown 渲染
# ====================================================
def layer1_top_sectors(top_n: int = 5, fallback_to_sina: bool = True) -> list[dict]:
    """
    主入口：拉今日最强 top_n 个行业板块（仅板块层面，不含成分股）。
    主源同花顺，挂了 fallback 到新浪申万一级。
    """
    sectors = fetch_ths_top_sectors(top_n=top_n)
    if sectors:
        for s in sectors:
            s["_source"] = "ths"
        return sectors
    if fallback_to_sina:
        print("  ⚠️ 同花顺板块挂，回退新浪申万一级")
        return fetch_sina_top_sectors(top_n=top_n)
    return []


def enrich_sectors_with_quality(
    sectors: list[dict],
    pool_size: int = 6,
    call_llm: bool = True,
    sleep_between: float = 1.2,
) -> list[dict]:
    """
    给每个板块补 top_quality（候选池） + llm_pick（LLM 精选叙述）

    sleep_between 控制对同花顺的请求间隔，避免触发 401。
    """
    for i, s in enumerate(sectors):
        if i > 0:
            time.sleep(sleep_between)
        s["candidates"] = top_quality_stocks_in_sector(s, pool_size=pool_size)
        if call_llm and s["candidates"]:
            s["llm_pick"] = llm_pick_top3(s, s["candidates"])
        else:
            s["llm_pick"] = ""
    return sectors


def format_sector_section(sectors: list[dict]) -> str:
    """渲染成 Markdown，插到 Layer 1（北向 / 龙虎榜之后）"""
    if not sectors:
        return ""
    src_label = {"ths": "同花顺行业", "sina_sw1": "新浪申万一级（备用源）"}.get(
        sectors[0].get("_source", "ths"), "未知源"
    )
    lines = [f"### 🔥 今日最强行业板块（{src_label}）", ""]
    # 板块概览：list 格式，飞书 markdown 友好
    for i, s in enumerate(sectors, 1):
        ni = s.get("net_inflow_亿")
        ni_part = f" · 净流入 **{ni} 亿**" if ni is not None else ""
        lines.append(
            f"{i}. **{s['name']} {s['pct']:+.2f}%** "
            f"· {s.get('up_count', '?')}↑/{s.get('down_count', '?')}↓"
            f"{ni_part}"
            f" · 领涨 {s.get('leader_name', '')} {s.get('leader_pct', '?')}%"
        )
    lines.append("")

    # 每个板块的 LLM 精选
    for s in sectors:
        if not s.get("llm_pick") and not s.get("candidates"):
            continue
        lines.append(f"#### {s['name']} {s['pct']:+.2f}%")
        llm = s.get("llm_pick") or ""
        # LLM 真正成功的输出 — 显示之；失败/未跑 — 显示候选池兜底
        if llm and not llm.startswith("⚠️"):
            lines.append(llm)
        else:
            if llm.startswith("⚠️"):
                lines.append(f"_{llm}（显示原始候选池）_")
            else:
                lines.append("_LLM 未启用，显示候选池：_")
            for x in (s.get("candidates") or [])[:5]:
                f = x.get("fundamental") or {}
                lines.append(
                    f"- **{x['name']}({x['code']})** {x.get('pctChg','?')}% "
                    f"市值 {x.get('float_mktcap_亿','?')}亿 PE {x.get('pe','?')} "
                    f"| 净利润 {f.get('净利润(亿)','?')}亿 ROE {f.get('roe%','?')}% "
                    f"同比 {f.get('净利润同比%','?')}%"
                )
        lines.append("")
    return "\n".join(lines)


# ====================================================
# 一站式入口 — brief.py 里只调这一个就够
# ====================================================
def run_sector_module(top_n_sectors: int = 5, pool_size: int = 6, call_llm: bool = True) -> tuple[list[dict], str]:
    """
    返回 (sectors_with_quality, markdown_section)

    失败安全：任何一步挂掉都返回空 list + 空串，不抛异常。
    在 brief.py main() 里这样用：
        try:
            sectors, sector_md = run_sector_module()
        except Exception as e:
            print(f"⚠️ 板块模块跳过: {e}")
            sectors, sector_md = [], ""
        # 然后把 sector_md append 到 Layer 1 输出
    """
    try:
        sectors = layer1_top_sectors(top_n=top_n_sectors)
        if not sectors:
            return [], ""
        sectors = enrich_sectors_with_quality(sectors, pool_size=pool_size, call_llm=call_llm)
        md = format_sector_section(sectors)
        return sectors, md
    except Exception as e:
        print(f"⚠️ 板块模块异常: {type(e).__name__}: {e}")
        return [], ""


# ====================================================
# 独立测试入口
# ====================================================
if __name__ == "__main__":
    import socket
    socket.setdefaulttimeout(15)
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent.parent / ".env")

    bs.login()
    try:
        sectors, md = run_sector_module(top_n_sectors=3, pool_size=5, call_llm=True)
        print("\n=== sectors raw ===")
        for s in sectors:
            print(f"\n板块 {s['name']} +{s['pct']}% src={s.get('_source')}")
            for c in (s.get("candidates") or []):
                f = c.get("fundamental") or {}
                print(f"  {c['code']} {c['name']} fund_keys={list(f.keys())} 净利润={f.get('净利润(亿)')}")
        print("\n=== markdown ===\n")
        print(md)
    finally:
        bs.logout()
