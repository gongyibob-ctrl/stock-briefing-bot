# A 股行情/K线数据源稳定性评估 — 2026-05-22

## 背景

`brief.py` 当前用 akshare 调用东方财富接口拉个股快照与日 K：

- `ak.stock_individual_info_em` → 今晚返回空响应，触发 `JSONDecodeError`
- `ak.stock_zh_a_hist` → `ConnectionError` 持续

东财对外接口（push2.eastmoney.com / quote.eastmoney.com）整体抽风。需要找一个**完全独立、不依赖东财**的源替代价格 + K 线，否则砍掉 MA/MACD 模块。

测试目标：拉 600519 / 300750 / 002594 的**今日价格 + 涨跌幅 + 成交额**和**近 30 个交易日 K 线**。

## 实测结果（2026-05-22 晚 22:50 北京时间，盘后）

| 数据源 | 安装 | API Key | 实时快照 | 日 K 线 | 含今日数据 | 单只股票耗时 | 字段完整度 | 备注 |
|---|---|---|---|---|---|---|---|---|
| **baostock** | `pip install baostock` | 无 | 无（仅历史 + 当日已收盘 K） | ✅ 20 条 | ✅ 含今日收盘 + pctChg + amount | 0.18 s | 高（OHLCV + 涨跌幅 + 成交额一次到位） | 协议是它自己的 TCP，完全绕开东财/新浪/腾讯 |
| **Sina hq.sinajs.cn** | 内置（requests） | 无 | ✅ 含买卖五档 | ❌ | ✅ 当日 OHLC + 成交量额 + 昨收 | 1.16 s（一次批量 3 只） | 高（盘口完整） | 必须设 `Referer: https://finance.sina.com.cn`，编码 GBK |
| **Tencent qt.gtimg.cn** | 内置（requests） | 无 | ✅ 含买卖五档 + 涨跌幅 + 总市值 + PE/PB | ❌ | ✅ | 1.0 s | 高 | 字段比新浪还多（市值、PE、PB、换手率） |
| **pytdx 通达信** | `pip install pytdx` | 无 | ✅ | ✅ 30 条 | ✅ | 0.32 s | 中（自己拼字段，无涨跌幅需算） | 公开服务器列表会漂移，要 fallback；前两个 IP 已挂，第三个能连 |
| **tushare 免费档** | `pip install tushare` | 需注册拿 token | — | — | — | — | — | 日线接口要积分门槛（120），新号通常不够 |
| **qstock** | `pip install qstock` | 无 | — | — | — | — | — | macOS arm64 上 `py_mini_racer` ctypes 加载失败，import 即崩溃 |

## 关键洞察

1. **东财今晚是孤岛事件**。baostock（自有 TCP）、新浪（hq.sinajs.cn）、腾讯（qt.gtimg.cn）、通达信（pytdx TCP）四套独立基础设施都正常返回，且数值互相印证（茅台 1290.20，宁德 411.16，比亚迪 93.75）。说明用独立源做兜底架构上是 OK 的，不必砍功能。
2. **baostock 单源就够**。返回的日 K 已经含 `pctChg`（涨跌幅）和 `amount`（成交额），意味着 `a_quote` 要的"最新价 / 涨跌幅 / 成交额"和 `a_kline` 要的 30 日 K 线**用一个调用一次取齐**，不需要两个数据源拼。MA5/10/20 和 MACD 本来就在本地 pandas 算，不依赖数据源。
3. **唯一缺口**：baostock 没有"总市值 / 流通市值 / 行业"这种 fundamental 字段。当前 `a_quote` 取了，但 LLM 简报里这些字段每天不变，**建议从 watchlist.json 静态写死**（自用工具，3 只股票手填一次即可）—— 或者用腾讯的实时接口（`qt.gtimg.cn` 返回字段第 9 位是总市值，第 45 位是流通市值），但会引入跨源依赖。

## 建议

**采用 Option A：baostock 替换**。理由：

- 0 配置（无 token）
- 协议完全独立于东财/新浪/腾讯 HTTP，今晚事件中是最稳的
- 一次 API 同时覆盖 quote + kline，删代码不加代码
- 接口稳定（baostock 接口 5+ 年未变）
- 单只 0.18 s，3 只串行 < 1 s，比东财快

**腾讯/新浪做二级 fallback** 保留可选项；pytdx 不推荐做主源（服务器列表维护成本）。

**砍掉 `a_quote` 里的"总市值/流通市值/行业"** —— 改用 watchlist.json 静态字段，或在第一次跑成功时落盘 cache，每周刷一次。

## 实施

替换代码见同目录 `quote_source_patch.py`，可直接覆盖 `brief.py` 的 `a_quote` / `a_kline` / `_enrich_kline`。保持原有 dict schema，下游 LLM prompt 不用改。

进程级别加一行：

```python
import baostock as bs
_BS_LOGGED_IN = False
def _bs_login():
    global _BS_LOGGED_IN
    if not _BS_LOGGED_IN:
        bs.login()
        _BS_LOGGED_IN = True
import atexit; atexit.register(lambda: bs.logout())
```

baostock 要求显式 login（一次即可），脚本退出时 logout。

## 风险与备案

- baostock 历史上偶尔有 1-2 小时维护窗口，发生概率远低于东财今晚这种。一旦出问题，可临时切到腾讯 `qt.gtimg.cn`（patch 文件含 `tencent_quote_fallback` 备用函数）。
- baostock 含"今日已收盘"K 线的前提是收盘后 ≥30 分钟拉取，正常 16:00 之后稳。如果在盘中跑（不是当前用例），需用 5min 频率最后一根近似当日。
