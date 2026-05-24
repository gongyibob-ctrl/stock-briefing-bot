# 基本面数据源评估 — 2026-05-23

## TL;DR

**baostock 没坏，函数逻辑也没坏。** Bug 是 `fetch_market_tables()` 里 `run_sector_module()` 调用时 **baostock 还没 login**，所以 `bs.query_profit_data` 全部返回 0 行 + stdout 喷 `you don't login.`。`fetch_fundamentals` 静默吞下去，返回 `{}`，候选股的 `fundamental` 字段就全是空。

修复 = 一行：在 `fetch_fundamentals` 入口 lazy-login baostock。配套加一个 akshare 兜底，防止某天 baostock 宕机。

## 1. Bug 定位

### 复现路径
`brief.py:fetch_market_tables()` 第 161-162 行：
```python
from sector import run_sector_module
_, sector_md = run_sector_module(top_n_sectors=5, pool_size=6, call_llm=True)
```
而 `_bs_login()` 直到第 270 行（K 线拉取）才被首次调用。所以 `run_sector_module` → `top_quality_stocks_in_sector` → `fetch_fundamentals` 这条链上，baostock 全程未登录。

### 实测
**手动 `bs.login()` 后直接调 `fetch_fundamentals`**（5 只样本股全部命中 2025 年报）：

| code | 报告期 | 净利润(亿) | ROE% | 净利率% | 同比% |
|---|---|---|---|---|---|
| 600519 茅台 | 2025Q4 | 853.10 | 34.46 | 50.53 | -4.50 |
| 002475 立讯 | 2025Q4 | 181.70 | 21.52 | 5.47 | 24.63 |
| 600183 生益 | 2025Q4 | 38.92 | 21.08 | 13.69 | 108.37 |
| 600176 巨石 | 2025Q4 | 34.15 | 10.75 | 18.09 | 35.02 |
| 002001 新和成 | 2025Q4 | 68.02 | 21.77 | 30.57 | 15.35 |

**未登录直接调**：baostock 在 stdout 打 `you don't login.`，返回 `{}` × 5。

bug 性质 = session 管理漏洞，不是数据源问题。

## 2. 替代源测试矩阵

虽然不需要切源，但顺手测了一下，留作 baostock 真宕机时的备胎：

| 源 | 可用 | 字段覆盖 | 单股请求耗时 | 备注 |
|---|---|---|---|---|
| `bs.query_profit_data` + `query_growth_data` | OK | ROE/净利润/营收/净利率/同比 全有 | ~0.3s ×2 | 现有主源，没毛病 |
| `ak.stock_financial_abstract` | OK | 同花顺源，80+ 指标 ×多期 | ~1.5s | 列名是 `20251231` 这种字符串日期，需要按期挑列 |
| `ak.stock_financial_analysis_indicator` | OK | 杜邦/ROE 全套 | ~2s | 数据稍滞后 |
| `ak.stock_financial_abstract_ths` | OK | 同花顺另一接口 | ~1.5s | 备选 |
| 新浪 `vip.stock.finance.sina.com.cn` | 跳过 | — | — | akshare 已经封过，直接用 akshare |
| 腾讯 `qt.gtimg.cn` | 跳过 | 只有快照 PE/PB，没结构化 ROE/同比 | — | 不够用 |
| tushare 免费档 | 跳过 | 需 token，新账号 fina_indicator 受限 | — | 不符合"无门槛"约束 |

**结论**：baostock 主源 + akshare `stock_financial_abstract` 兜底就够。

## 3. 推荐方案

**Patch `fetch_fundamentals` 做两件事**：

1. **Lazy login**：函数顶部加 `_ensure_bs_login()`，调 `bs.login()` 一次（用模块级 flag 防重复）。即便 `brief.py` 流程改了顺序、或者第三方代码独立调用 `sector.py`，也都能 work。

2. **兜底切 akshare**：如果 baostock profit 拉空（网络抖动 / baostock 临时挂），自动切 `ak.stock_financial_abstract` 解析最近一期。保持出参 schema 不变。

这两个改动加起来约 60 行，drop-in 替换原函数即可，不动其他代码。

## 4. 验收

5 只样本股全部能拉到完整字段（见 §1 表格）。pipeline 流跑：从 cold start（无 login state）调 `run_sector_module()`，候选股 `fundamental` 字段非空率 = 100%。

不需要改 `brief.py` 调用顺序、不需要改 `top_quality_stocks_in_sector`，最小侵入。
