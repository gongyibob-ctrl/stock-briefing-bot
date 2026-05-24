# stock-briefing-bot

自用每日股票简报工具 —— A 股自选股 + 全市场态势 + LLM 推理 → 推送飞书。

设计原则：**给数据 + 讲因果 + 不预测**。

## 它做什么

每天定时跑（早 8:30 + 晚 16:00），输出一份 markdown 简报，自动推送到飞书群。同时支持 `@ 机器人` 临时查询。

### Layer 1：全市场态势
- **北向资金** 当日 / 5 日累计净买
- **龙虎榜温度** 共多少只上榜 + 触发条件统计
- **今日资金动向** 昨日 Top 20 上榜股的席位明细聚合，识别游资 / 机构 / 北向 / 散户（孙哥/章盟主/作手新一/赵老哥/拉萨散户）
- **强势板块 Top 5** 同花顺主源 + 新浪申万备用，每板块 LLM 挑 Top 3 优质股（基于盈利规模 / ROE / 同比 / PE / 流通市值）

### Layer 2：自选股深度
每只 A 股拉：
- 行情 / K 线 / MA5/10/20 / MACD（baostock 主源 + 腾讯兜底）
- 北向持股 7 日累计变化
- 龙虎榜命中 + 席位明细
- 大宗交易折溢率
- 融资融券余额
- 股东户数趋势（近 4 期环比）
- 近 30 日公告 + 近期新闻（带原文内容）
- baostock 财报：ROE / 净利润 / 同比 / 净利率（akshare 兜底）

LLM 输出格式：
- **📋 数据清点** 5-8 条 bullet
- **📰 关键消息解读** 🟢/⚪/🔴 标签 + 一句话解读 + 来源
- **🧠 推理链** 显式因果（"因为 A 所以 B，叠加 C 说明 D，综合看 …"）
- **🎯 建议** 关注/观望/谨慎/减仓/止损/加仓

## 数据源

完全免费 + 多源 fallback：

| 数据 | 主源 | 备用 |
|---|---|---|
| 行情 / K 线 | baostock | 腾讯 qt + ifzq |
| 财报 / ROE / 同比 | baostock query_profit/growth | akshare stock_financial_abstract |
| 板块涨幅 + 成分股 | 同花顺 q.10jqka.com.cn | 新浪申万 sw1_xxx |
| 主力数据 | akshare（巨潮 / 新浪 / 东财） | — |
| 新闻 / 公告 | akshare stock_news_em / stock_zh_a_disclosure_report_cninfo | — |

## 启动

```bash
git clone https://github.com/<you>/stock-briefing-bot.git
cd stock-briefing-bot

# 装依赖（推荐 Python 3.11+）
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 配 env
cp .env.example .env
# 编辑 .env，填 LLM_API_KEY 和 FEISHU_WEBHOOK_URL（应用机器人可选）

# 自选股 + 基本面缓存
# watchlist.json：你的自选股
# stock_meta.json：每只股的市值/行业（每季度刷新一次）

# 跑一次完整 brief（约 5-7 分钟）
.venv/bin/python brief.py

# 启动 @ 机器人（长连接，常驻进程）
.venv/bin/python bot.py
```

## 定时跑（macOS launchd）

`com.yibo.stock-briefing.plist` 示例：早 8:30 + 16:00 自动跑，非交易日自动跳过（baostock 交易日历）。

```bash
cp com.yibo.stock-briefing.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.yibo.stock-briefing.plist
```

## 自定义

- **加股票**：编辑 `watchlist.json` 加一行 `{"code": "...", "name": "...", "market": "a"}`，同步在 `stock_meta.json` 补市值 / 行业
- **换 LLM**：改 `.env` 的 `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY`（OpenAI 兼容协议即可）
- **换通知**：`push_feishu()` 改成你的 webhook 实现（钉钉 / Slack / Discord 都行）

## 设计取舍

- ✅ 主力数据接齐了 7 维（北向 / 龙虎榜席位 / 大宗 / 融资融券 / 股东户数 / 公募重仓 / 板块同业）
- ✅ LLM 必须显式因果 + 数据可溯源
- ❌ 不预测涨跌 / 不给目标价 / 不机械给买卖信号（这些 LLM 没 edge）
- ❌ 不集成券商下单接口（自用，手动下单避免风险）

## 谁适合用

- 长线持仓的散户，每天想 3 分钟看完关键信号
- 不想自己看龙虎榜 + 板块榜 + 新闻 + 公告的人

不适合：日内炒手 / 量化高频。

## License

MIT
