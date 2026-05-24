"""
akshare 探针脚本 — 验证 6 个核心数据接口能拉到真实数据。

每个接口独立 try/except，单个失败不影响其他。
输出：终端友好预览 + data/probe_<timestamp>.json 留存完整字段。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT = Path(__file__).parent
WATCHLIST = json.loads((ROOT / "watchlist.json").read_text())["watchlist"]
TODAY = datetime.now().strftime("%Y%m%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
LAST_WEEK = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def preview(df: pd.DataFrame, n: int = 5) -> dict:
    if df is None or df.empty:
        print("  ⚠️  空数据")
        return {"columns": [], "rows": 0, "sample": []}
    print(f"  字段（{len(df.columns)} 列）：{list(df.columns)}")
    print(f"  行数：{len(df)}")
    print(df.head(n).to_string(index=False, max_colwidth=30))
    return {
        "columns": list(df.columns),
        "rows": len(df),
        "sample": df.head(n).to_dict(orient="records"),
    }


def probe(label: str, fn) -> dict:
    section(label)
    try:
        df = fn()
        return preview(df)
    except Exception as e:
        print(f"  ❌ 失败：{type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


results: dict = {"probed_at": datetime.now().isoformat(), "interfaces": {}}

# 1. watchlist 实时行情（带重试，akshare 偶发连接抖动）
def fetch_watchlist_quotes():
    import time
    last_err = None
    for _ in range(3):
        try:
            spot = ak.stock_zh_a_spot_em()
            codes = [s["code"] for s in WATCHLIST]
            return spot[spot["代码"].isin(codes)][
                ["代码", "名称", "最新价", "涨跌幅", "成交额", "市盈率-动态", "总市值"]
            ]
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise last_err

results["interfaces"]["1_watchlist_quotes"] = probe(
    "① watchlist 实时行情（stock_zh_a_spot_em 过滤）", fetch_watchlist_quotes
)

# 2. 龙虎榜（最近一个交易日）
def fetch_lhb():
    return ak.stock_lhb_detail_daily_sina(date=YESTERDAY)

results["interfaces"]["2_lhb"] = probe(
    f"② 龙虎榜（{YESTERDAY}）", fetch_lhb
)

# 3. 北向资金净流入历史（最近 30 个交易日）
def fetch_hsgt_hist():
    df = ak.stock_hsgt_hist_em(symbol="北向资金")
    return df.tail(10)  # 最近 10 行

results["interfaces"]["3_hsgt_hist"] = probe(
    "③ 北向资金净流入历史（stock_hsgt_hist_em 北上）", fetch_hsgt_hist
)

# 4. 个股北向持股（茅台示例）
def fetch_hsgt_individual():
    return ak.stock_hsgt_individual_em(symbol="600519").tail(10)

results["interfaces"]["4_hsgt_individual"] = probe(
    "④ 个股北向持股（茅台 600519，最近 10 行）", fetch_hsgt_individual
)

# 5. 大宗交易（最近 7 日）
def fetch_dzjy():
    return ak.stock_dzjy_mrtj(start_date=LAST_WEEK, end_date=TODAY).head(20)

results["interfaces"]["5_dzjy"] = probe(
    f"⑤ 大宗交易每日统计（{LAST_WEEK}~{TODAY}）", fetch_dzjy
)

# 6. 公告（巨潮，茅台）
def fetch_announcements():
    return ak.stock_zh_a_disclosure_report_cninfo(
        symbol="600519",
        market="沪深京",
        category="",
        start_date=LAST_WEEK,
        end_date=TODAY,
    ).head(10)

results["interfaces"]["6_announcements"] = probe(
    "⑥ 巨潮公告（茅台 600519，近 7 日）", fetch_announcements
)

# 7. （加餐）东财新闻 - 看看 daily_stock_analysis 没用的这个接口长啥样
def fetch_news_em():
    return ak.stock_news_em(symbol="600519").head(5)

results["interfaces"]["7_news_em"] = probe(
    "⑦ 东财个股新闻（stock_news_em 茅台 600519）", fetch_news_em
)


# 保存完整结果
out = ROOT / "data" / f"probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
out.parent.mkdir(exist_ok=True)


def _coerce(o):
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    return str(o)


out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=_coerce))
print(f"\n\n✅ 完整结果已保存：{out}")

# 摘要
print(f"\n{'=' * 60}\n探针总结\n{'=' * 60}")
for k, v in results["interfaces"].items():
    if "error" in v:
        print(f"  ❌ {k}: {v['error']}")
    else:
        print(f"  ✅ {k}: {v['rows']} 行, {len(v['columns'])} 字段")
