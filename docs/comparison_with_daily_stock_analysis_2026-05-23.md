# stock-briefing-bot ↔ daily_stock_analysis 对比与借鉴报告

日期：2026-05-23
对比对象：`ZhuLinsen/daily_stock_analysis`（本地仓库快照 + GitHub README）
我项目：`stock-briefing-bot`（自用，ship-fast，单机 launchd 定时）

---

## 0. TL;DR

daily_stock_analysis 是想做 SaaS / 桌面端 / Product Hunt 的"全套产品工程"，代码量约 2-3 万行，主程序 `main.py` 单文件就 35K，`src/analyzer.py` 146K，`src/notification.py` 105K，`src/config.py` 121K——典型"功能多 + 单文件巨型 + 抽象厚"。

我项目是自用刚需工具，总代码 ~2100 行（`brief.py` 1067 + `sector.py` 713 + `bot.py` 327），所有产品决策围绕"我每天 3 分钟看完关键信号"。

**真正值得抄、而且抄完不会拖累我的，只有 5 件事**（详见 §4 推荐路径）：

1. **持仓账本（轻量版）** ——只做 watchlist 加 `cost_basis` + `shares`，每日简报里输出"实时浮盈 / 距止损线"，**不**做 CSV 导入 / FIFO / 多账户。
2. **大盘复盘（独立模块）** ——把 Layer 1 抽成 `market_review()` 函数，单独可调，跟自选股 brief 解耦。
3. **markdown 报告归档元数据**——给 `reports/brief_*.md` 加 frontmatter（日期 / 区间收益 / 命中信号），将来回头查"3 月 12 日我看到什么"很关键。
4. **席位标签外置 + 别名**——把 SEAT_TAGS 从 `brief.py` 抽到 `data/seats.json`，加别名匹配。daily_stock_analysis 的 `name_to_code_resolver.py` 给了路子。
5. **`--dry-run` / `--no-notify` / `--stocks` CLI 参数**——daily_stock_analysis 的 main.py argparse 设计抄一小段就够。

**坚决不抄**：Web 前端 / Electron 桌面 / FastAPI / 15 种策略 prompt 池 / 多 LLM 路由 / 多通知渠道矩阵 / GitHub Actions 跑分析（本地 launchd 已够）/ 图片报告（飞书原生 markdown 已经好看）/ portfolio 多账户 + FIFO + 公司行动调整。

---

## 1. 对比对象架构速览

```
daily_stock_analysis/
├── main.py                    # 35K，单文件主入口（argparse / dry-run / schedule / serve / market-review）
├── server.py                  # FastAPI uvicorn 入口
├── webui.py                   # WebUI 启动器（拉前端 + 启后端）
├── api/
│   ├── app.py                 # FastAPI 工厂 + CORS + 静态托管 SPA
│   ├── deps.py
│   ├── middlewares/           # auth, error_handler
│   └── v1/endpoints/          # agent / alerts / analysis / auth / backtest /
│                              # health / history / portfolio / stocks /
│                              # system_config / usage
├── bot/
│   ├── dispatcher.py          # 26K，命令路由 + RateLimiter
│   ├── handler.py             # 7K
│   ├── commands/              # analyze / ask / batch / chat / help /
│   │                          # history / market / research / status / strategies
│   └── platforms/             # 飞书 / 钉钉 / Discord (Stream + Webhook)
├── apps/
│   ├── dsa-web/               # Vite + React + TS + Tailwind + Playwright
│   │   └── src/pages/         # Alerts / Backtest / Chat / Home / Login /
│   │                          # Portfolio / Settings
│   └── dsa-desktop/           # Electron
├── src/
│   ├── analyzer.py            # 146K，单文件分析器
│   ├── market_analyzer.py     # 51K
│   ├── stock_analyzer.py      # 31K
│   ├── notification.py        # 105K（含所有渠道路由）
│   ├── notification_sender/   # feishu / discord / dingtalk / email / gotify /
│   │                          # ntfy / pushover / pushplus / serverchan3 /
│   │                          # slack / telegram / wechat / webhook / astrbot
│   ├── md2img.py              # imgkit + markdown-to-file 转 PNG
│   ├── feishu_doc.py          # 推飞书云文档
│   ├── search_service.py      # 150K，多搜索引擎聚合（serpapi / tavily / bocha / brave / minimax / searxng）
│   ├── report_language.py     # 27K，中英双语
│   ├── storage.py             # 83K，SQLAlchemy ORM + 全部表定义
│   ├── config.py              # 121K，单文件全部 config schema
│   ├── scheduler.py           # 12K
│   ├── agent/
│   │   ├── orchestrator.py
│   │   ├── research.py        # 多轮分解 + 子问题 + token budget
│   │   ├── agents/            # decision / intel / portfolio / risk / technical
│   │   ├── tools/             # analysis / backtest / data / market / search
│   │   └── skills/            # aggregator / router / defaults / skill_agent
│   ├── services/              # 27 个服务文件，含 portfolio_* / alert_* /
│   │                          # backtest / history / task_queue / report_renderer
│   ├── repositories/          # alert / analysis / backtest / portfolio / stock
│   ├── core/
│   │   ├── market_review.py
│   │   ├── pipeline.py
│   │   ├── backtest_engine.py
│   │   └── trading_calendar.py
│   └── schemas/
├── data_provider/             # 15 个 fetcher: akshare / baostock / tushare /
│                              # pytdx / efinance / tickflow / longbridge /
│                              # yfinance / finnhub / alphavantage
├── strategies/                # 15 个 YAML 策略 prompt
├── .github/workflows/         # 10 个：ci / 00-daily-analysis / auto-tag /
│                              # docker-publish / desktop-release / pr-review …
└── docs/                      # 30+ 文档，中英双语
```

我项目结构（对照）：

```
stock-briefing/
├── brief.py                   # 主流程：Layer 1 全市场 + Layer 2 自选股
├── sector.py                  # 板块强势 + 板块内 Top 3
├── bot.py                     # 飞书 @ 机器人长连接
├── probe.py                   # 数据源诊断
├── watchlist.json             # 自选股
├── stock_meta.json            # 市值 / 行业静态缓存
├── data/                      # 缓存
├── reports/                   # 历史 brief markdown
├── docs/                      # 数据源评估文档
└── com.yibo.stock-briefing.plist  # launchd 定时
```

---

## 2. 功能对比矩阵

| # | 能力维度 | daily_stock_analysis | 我项目 (stock-briefing) | 借鉴价值 | 工作量 | 推荐 |
|---|---|---|---|---|---|---|
| 1 | 自选股日报推送 | ✅ A/HK/US，决策仪表盘 | ✅ A 股深度 + HK/US 简版 | — | — | — |
| 2 | 全市场态势（北向 / 龙虎榜 / 板块） | ⚠️ 有 `market_review` 模块但口径偏宏观 | ✅ 北向 + 龙虎榜温度 + **游资席位聚合** + 板块 + Top3 | **负**（我更专业）| — | 不抄 |
| 3 | 大盘复盘**独立模块化** | ✅ `src/core/market_review.py` 独立 | ❌ 跟 brief 耦合 | 中 | 2-3h | V2 |
| 4 | 持仓账本 | ✅ 4 个 service + repo + DB schema + CSV 导入 + FIFO + 公司行动 | ❌ 完全没有 | **高（轻量版）** | 4-6h（简版）/ 40h（全套）| **立刻做轻量版** |
| 5 | 持仓告警（止损 / 集中度 / 回撤） | ✅ `portfolio_alerts.py` 4 类规则 | ❌ | 中 | 3-4h | V2 |
| 6 | 策略问股 / Agent | ✅ `src/agent/`，多 agent + tool registry + token budget | ⚠️ `bot.py` 命令式（板块 / 龙虎榜 / 单股）| **负**（我的命令式更可控）| — | 不抄 |
| 7 | 15 种策略 prompt 池（缠论 / 波浪 / 趋势）| ✅ `strategies/*.yaml` | ❌ | **负**（已被审查 PASS）| — | 不抄 |
| 8 | 回测 | ✅ `backtest_engine.py` + 关键词匹配 LLM | ❌ | **负**（关键词回测 = 幻觉）| — | 不抄 |
| 9 | LLM "目标价 1364.94" | ✅（但不靠谱） | ❌ 明确拒绝 | **负** | — | 不抄 |
| 10 | 多 LLM 渠道 + fallback | ✅ Gemini / OpenAI / Claude / DeepSeek / Anspire / AIHubMix / Ollama | ⚠️ litellm + DeepSeek-v4-pro 单渠道 | 中 | 1-2h | V2 |
| 11 | 多通知渠道 | ✅ 14 个 sender | ✅ 飞书 webhook + 应用机器人 | **负**（自用一渠道够）| — | 不抄 |
| 12 | Markdown → 图片 (imgkit) | ✅ `md2img.py` | ❌ | 低（飞书 markdown 原生 OK）| 1h | V2 选做 |
| 13 | Web UI (Vite + React) | ✅ 7 页 SPA | ❌ | **低**（自用无需要）| 40h+ | 永远不做 |
| 14 | 桌面端 (Electron) | ✅ `apps/dsa-desktop` | ❌ | **低**（自用无需要）| 30h+ | 永远不做 |
| 15 | FastAPI 服务 | ✅ `api/v1/endpoints/*` | ❌ | **低**（不上线无需要）| 20h+ | 永远不做 |
| 16 | 任务编排 + SSE | ✅ `task_queue.py` + 进度回调 | ⚠️ stdout print | 低（自用看终端 OK）| — | 不抄 |
| 17 | CSV 券商账单导入 | ✅ 华泰 / 中信 / 招商 parser | ❌ | **低**（自选 7 只，手填）| 8h | 不抄 |
| 18 | 中英双语 | ✅ `report_language.py` | ❌ 只中文 | **负**（自用单语）| — | 不抄 |
| 19 | 历史报告归档 | ✅ DB 存 + Web 查 | ✅ `reports/*.md` 文件归档 | — | — | — |
| 20 | 报告 frontmatter / 元数据 | ⚠️ 存 DB 字段 | ❌ 纯 markdown 无元数据 | 中 | 1-2h | **立刻做** |
| 21 | CLI 参数（dry-run / no-notify / 单股）| ✅ argparse 完整 | ❌ 一律全跑 | 中 | 1-2h | **立刻做** |
| 22 | 交易日历 | ⚠️ `trading_calendar.py` 自实现 | ✅ baostock query_trade_dates | — | — | — |
| 23 | 席位识别 | ⚠️ 没有专用模块 | ✅ `SEAT_TAGS` in `brief.py`，含孙哥 / 章盟主 / 作手新一 等 13 个 | **负**（我更专业）| — | 不抄 |
| 24 | 席位**外置 + 别名** | ❌ | ❌ 但 daily_stock_analysis 的 `name_to_code_resolver.py` 给了路子 | 中 | 1h | **立刻做** |
| 25 | 推送降噪 (`notification_noise.py`) | ✅ 15K 抑制重复 | ❌ | 低 | 2h | V2 |
| 26 | 报告渲染层 (`report_renderer.py`) | ✅ | ⚠️ 散在 brief.py 中 | 中 | 3-4h（解耦）| V2 |
| 27 | GitHub Actions 跑分析 | ✅ `00-daily-analysis.yml` | ❌ launchd | **负**（launchd 已经稳）| — | 不抄 |
| 28 | 自动 tag + Release | ✅ `auto-tag.yml` + `create-release.yml` | ❌ | 低（自用无 release）| 2h | V2 看心情 |
| 29 | AI 协作治理 (`AGENTS.md` + `CLAUDE.md` 软链 + skills) | ✅ 12K AGENTS.md + 多 skill | ⚠️ 我们用 ~/.claude/CLAUDE.md，无项目级 | 低 | 1h | V2 看心情 |
| 30 | 数据源 fallback 链式 | ✅ 10 个 fetcher，含 longbridge / finnhub / alphavantage | ✅ baostock + akshare + 腾讯 + 同花顺 + 新浪 | — | — | — |
| 31 | 港股 / 美股深度 | ✅ longbridge OpenAPI + yfinance + finnhub | ⚠️ 我之前删了 | **低**（我不交易港美股）| 6h | 永远不做 |
| 32 | 概念 / 板块 LLM 选股 | ⚠️ 通过策略 prompt 池 | ✅ 强势板块 + 板块内 LLM 挑 Top 3 | **负**（我更专业）| — | 不抄 |
| 33 | 股东户数 4 期趋势 | ❌ | ✅ | — | — | — |
| 34 | 大宗交易折溢率 | ❌ | ✅ | — | — | — |
| 35 | 公募重仓 / 北向 7 日 | ❌ | ✅ | — | — | — |

数一下：

- 我已经赢了的：游资席位 / 股东户数 / 大宗 / 公募重仓 / 北向 7 日累计 / 显式因果链 LLM prompt
- 它赢了的、且对我有意义的：**持仓账本（轻量版） / 大盘复盘解耦 / 报告元数据 / CLI 参数 / 席位外置**（5 项）
- 它赢了的、但对我没意义的：Web / 桌面 / API / 多通道 / 多 LLM / 多语言 / 回测 / 策略池 / GitHub Actions（9+ 项 PASS）

---

## 3. 详细借鉴评估（按推荐优先级排序）

### 3.1 持仓账本（轻量版）—— 优先级 **立刻做**

**daily_stock_analysis 的实现**：
- `src/repositories/portfolio_repo.py` 1089 行，SQLAlchemy ORM，表：`PortfolioAccount` / `PortfolioPosition` / `PortfolioPositionLot` / `PortfolioTrade` / `PortfolioCashLedger` / `PortfolioCorporateAction` / `PortfolioDailySnapshot` / `PortfolioFxRate`
- `src/services/portfolio_service.py` 1610 行，业务逻辑（账户 CRUD / 事件写入 / 快照重放 / FIFO 成本 / 公司行动调整）
- `src/services/portfolio_import_service.py` 448 行，华泰 / 中信 / 招商 CSV 列名映射
- `src/services/portfolio_alerts.py` 616 行，4 类规则：`portfolio_stop_loss` / `portfolio_concentration` / `portfolio_drawdown` / `portfolio_price_stale`
- `src/services/portfolio_risk_service.py` 441 行
- 加起来 ~4200 行，对应 SaaS / 多用户 / 多账户 / 多币种场景

**借鉴价值**：高，但**不要直接抄**。它解的是"用户从券商导出账单"的问题，我解的是"我自己 7 只股，买入价我知道"。

**自用极简版**：

```json
// watchlist.json 升级
{
  "watchlist": [
    {
      "code": "600519",
      "name": "贵州茅台",
      "market": "a",
      "shares": 100,           // 当前持仓股数（0 = 仅观察）
      "cost_basis": 1620.0,    // 成本均价
      "stop_loss": 1450.0      // 止损位（可选）
    }
  ]
}
```

每日简报里增加一段（在自选股 Layer 2 头部）：

```
### 📦 持仓盘点（2026-05-23）

| 股票 | 持仓 | 成本 | 现价 | 浮盈% | 距止损 |
|------|-----|------|-----|------|--------|
| 贵州茅台 | 100 | 1620.0 | 1538.0 | -5.06% | +6.07% (1450) |
| 宁德时代 | 200 | 230.5 | 248.3 | +7.72% | — |
| ...

合计：成本 ¥XX 万，市值 ¥XX 万，**浮盈 ¥XX 元 (X.X%)**
```

并且在 LLM 推理时把"你当前持有 X 股，成本 ¥Y，浮盈 Z%，止损位 ¥W"塞进 prompt，让建议**带持仓视角**（同样的"业绩低于预期"，对未持仓的人是"观望"，对持有人是"考虑减仓"）。

**工作量**：4-6 小时。改动面：`watchlist.json` schema + `brief.py` 加 `compute_pnl()` + LLM prompt 多一段 context + 简报渲染加表格。

**不要做的**：FIFO 多批次成本 / 多账户 / CSV 导入 / 公司行动调整 / 多币种 / SQLite + ORM / 8 张表。

---

### 3.2 大盘复盘独立模块化 —— 优先级 **V2 做**

**daily_stock_analysis 的实现**：`src/core/market_review.py` 232 行，`run_market_review(notifier, analyzer, search_service, send_notification, merge_notification, override_region, query_id)` 一个函数把 A / HK / US 三个市场拼起来。

**我项目的现状**：Layer 1 全市场逻辑写在 `brief.py` 里。如果我想"早上 8:30 只看大盘 + 板块，不看自选股"或"只看自选股不算大盘"，得手改代码。

**借鉴价值**：中。**早 8:30 看盘前**实际上自选股深度没那么重要（开盘前市场没动），主要看昨晚游资动作 + 板块预判；**16:00 收盘**才需要自选股深度。当前一刀切跑 5-7 分钟有点浪费。

**做法**：

```python
# brief.py 拆成
def run_market_review() -> str:     # Layer 1 only，1-2 分钟
def run_watchlist_brief() -> str:   # Layer 2，5 分钟
def run_full_brief() -> str:        # 两个拼起来
```

launchd 配两个 plist，早 8:30 跑 `--market-only`，晚 16:00 跑 `--full`。

**工作量**：2-3 小时。

---

### 3.3 报告 frontmatter 元数据 —— 优先级 **立刻做**

**daily_stock_analysis 的实现**：把 `AnalysisResult` 落 SQLAlchemy DB，字段含 `sentiment_score / trend_prediction / operation_advice / context_snapshot` 等。SaaS 需要支持 Web 查询历史。

**我项目的现状**：`reports/brief_20260522_213352.md` 这种命名，纯 markdown，回头看"3 月 12 日我看到什么"全靠 grep。

**自用极简版**：

```markdown
---
date: 2026-05-23
type: full | market-only | watchlist | adhoc
session: morning | afternoon
holdings_market_value: 285643.0
holdings_pnl_pct: -1.23
signals_hit:
  - "600519: 游资 章盟主 净买 1.2 亿"
  - "300750: 北向 7 日净增 +2.3 亿"
llm_model: deepseek-v4-pro
llm_cost_cny: 0.42
---

# 股民简报 2026-05-23 ...
```

将来要做"按月看哪天我的 watchlist 命中的游资席位最多"这种事，用 `yaml.safe_load(report.read_text().split('---')[1])` 就能批量读 metadata，**不需要 DB**。

**工作量**：1-2 小时。

---

### 3.4 CLI 参数 —— 优先级 **立刻做**

**daily_stock_analysis 的实现**：

```bash
python main.py
python main.py --debug
python main.py --dry-run
python main.py --stocks 600519,hk00700,AAPL
python main.py --market-review
python main.py --schedule
python main.py --serve
python main.py --serve-only
```

**我项目的现状**：`python brief.py` 一种跑法，要调试只能改代码。

**借鉴价值**：中。配合 §3.2 大盘复盘解耦更顺手。

**做法**：
- `python brief.py`               → full
- `python brief.py --market-only` → 仅 Layer 1
- `python brief.py --stocks 600519,300750` → 仅指定股
- `python brief.py --no-notify`   → 跑通流程不推送（调试）
- `python brief.py --dry-run`     → 不打 LLM 不推送（看数据流）

**工作量**：1-2 小时（argparse 30 行 + 4 个分支）。

---

### 3.5 席位标签外置 + 别名 —— 优先级 **立刻做**

**daily_stock_analysis 的实现**：`src/services/name_to_code_resolver.py` 处理"宁德时代 / 宁王 / 300750 / cATL"映射到统一 code。

**我项目的现状**：`SEAT_TAGS = [(("华泰证券", "深圳益田路"), "🔥 孙哥"), ...]` 硬编码在 `brief.py` 第 50 行。每次新增一个游资席位（江湖里隔几个月就有新名字），要改代码 → commit → 重启 launchd。

**做法**：抽到 `data/seats.json`：

```json
{
  "hot_money": [
    {
      "name": "孙哥",
      "match": [["华泰证券", "深圳益田路"]],
      "aliases": ["益田路孙哥", "深圳孙哥"]
    },
    ...
  ],
  "generic": [
    {"name": "拉萨散户", "match": [["拉萨"]]},
    {"name": "机构", "match": [["机构专用"]]},
    ...
  ]
}
```

`brief.py` 加 `def load_seat_tags() -> list: ...`，每次 brief 开始读一次，**不需要重启**。

**工作量**：1 小时。

---

### 3.6 多 LLM 渠道 fallback —— 优先级 **V2 做**

**daily_stock_analysis 的实现**：litellm + 多家 key 排列。

**我项目的现状**：`litellm.completion(model=..., api_base=..., api_key=...)`，DeepSeek-v4-pro 单渠道。已经记过教训：pplabs.tech 对 pa/gpt-5.5 大输入会 silently truncate（见全局 MEMORY）。

**借鉴价值**：中。目前 DeepSeek 稳定，但**一旦哪天它挂了或限流，我的早晚两次 brief 就废**。

**做法**：`.env` 里配 `LLM_FALLBACK_CHAIN=deepseek/deepseek-chat,gpt-4o-mini,claude-3-5-haiku`，每个 model 三次重试，失败转下一个。

**工作量**：1-2 小时。

---

### 3.7 持仓告警（止损 / 集中度 / 回撤）—— 优先级 **V2 做**

依赖 §3.1。等 §3.1 落地一两周看实际场景，再决定是否需要。

**轻量版的本质**：每天 brief 渲染时，加一句"⚠️ 贵州茅台 距止损线仅 +0.4%（成本 ¥1620，止损 ¥1450，现 ¥1456）"。**不需要独立 `alert_service.py`**。

**工作量**：2-3 小时，但是 §3.1 + 简单 if/else。

---

## 4. 推荐执行路径

按"立刻做 → V2 做 → 永远不做"分桶：

### 立刻做（合计 ~10 小时，一周内完成）

1. **席位外置**（1h）→ `data/seats.json`
2. **CLI 参数**（1-2h）→ `argparse` + 4 个 mode
3. **大盘复盘解耦**（2-3h）→ `run_market_review() / run_watchlist_brief() / run_full_brief()`
4. **持仓账本轻量版**（4-6h）→ `watchlist.json` 加字段 + brief 输出表格 + LLM prompt 加 context
5. **报告 frontmatter**（1-2h）→ YAML 头部

落地后：
- 早 8:30 launchd 调 `--market-only`（1-2 分钟跑完）
- 晚 16:00 launchd 调 `--full`（5-7 分钟）
- 飞书简报里多一段"📦 持仓盘点"
- `reports/*.md` 自带元数据，将来可批量统计

### V2 做（一两个月后看需求）

- 持仓告警（依赖轻量版账本 + 真实用一阵子）
- 多 LLM 渠道 fallback（依赖 DeepSeek 不挂）
- markdown→图片（依赖飞书展示真的撑不住，目前 markdown OK）
- 报告渲染层解耦（依赖 §3.1 / §3.2 之后 `brief.py` 变重）

### 永远不做（自用 + ship-fast 哲学）

- Web UI / Electron 桌面端 / FastAPI 服务（自用一个 markdown 推送 + @ 机器人就够）
- GitHub Actions 跑分析（launchd 已稳，且 GH Actions 拉国内数据源会慢 / 失败）
- 多通知渠道（自用只飞书）
- 中英双语（自用单语）
- 15 种策略 prompt 池（上次审查 PASS）
- 关键词回测（上次审查 PASS）
- "目标价 1364.94"（上次审查 PASS）
- CSV 券商账单导入 / FIFO / 多账户（自选 7 只，手填够了）
- 港股 / 美股深度数据源（不交易就不维护）
- 桌面端打包（自用无需要）

---

## 5. daily_stock_analysis 的反面教材

最后说几个**值得警惕**的设计模式，daily_stock_analysis 踩了，我项目不要踩：

1. **单文件巨型**：`src/analyzer.py` 146K / `src/config.py` 121K / `src/notification.py` 105K / `src/storage.py` 83K。已经远超人类一次能装下的复杂度，搜代码 + 改 bug 成本极高。我项目目前 `brief.py` 1067 行已经偏大，加完轻量持仓账本后必须考虑拆 `pnl.py` / `seats.py` / `feishu.py`。
2. **抽象层次错配**：portfolio 一个能力做了 4 个 service + 1 个 repo + 8 张 DB 表 + 3 家券商 CSV parser，远超它实际用户量需要。我自用版本只需要 30 行代码。**复杂度要跟实际场景对齐**。
3. **多渠道矩阵爆炸**：14 个通知 sender + 10 个数据 fetcher + 7 个搜索引擎 + 多 LLM 渠道。每个组合都要测试 + 兜底 + 文档。我项目"飞书 + baostock + akshare + 同花顺/腾讯/新浪"已经是 sweet spot，再加渠道收益递减。
4. **AI 协作治理过度**：12K 的 AGENTS.md / `CLAUDE.md` 软链 / `.claude/skills/` / `scripts/check_ai_assets.py` 校验脚本。对开源项目防止外部贡献者写飞了有意义，对我自用项目 0 价值。
5. **报告里堆"AI 决策仪表盘"**：README 示例里塞了"评分 65 / 看多 / 风险点 1 2 3 / 利好催化 1 2"——量化感很强但**没数据来源**，全靠 LLM 编。我项目"📋 数据清点 + 📰 关键消息 + 🧠 推理链"的格式（每一条都有数据 + 来源）应当继续坚持。

---

## 6. 一句话总结

daily_stock_analysis 是"想做产品上 Product Hunt 的 SaaS 工程"，工程含量高、产品定位散；我项目是"自用刚需 + ship-fast"，专业度高、scope 收敛。

**真正能从对方学到东西的，只是 5 件 ~10 小时就能落地的轻量改动**，集中在持仓账本、模块解耦、报告元数据、CLI 体验、配置外置。其它能力（Web / 桌面 / 多渠道 / Agent / 回测 / 策略池）**抄过来反而拖累我自用的快**。
