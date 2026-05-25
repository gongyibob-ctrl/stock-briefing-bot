#!/bin/bash
# 按北京时间触发不同 brief 模式
# - 北京 6:00-11:59 → --market-only（早盘前 1-2 分钟简报）
# - 北京 14:00-22:00 → default full（收盘后 5-7 分钟完整简报）
# - 其他时间 → 跳过
#
# launchd 在 macOS 本地时区触发本脚本（一天 2 次足够覆盖两个时段），
# 由脚本自己用 TZ=Asia/Shanghai 判断北京时间走哪条路径。

set -u
cd "$(dirname "$0")/.."

# 整个 brief.py 在北京时区跑，避免 PDT 的"今天"和北京"今天"错位
# （否则 PDT Sun 17:30 = 北京 Mon 08:30 时，is_trading_day_today() 会把 Sunday
# 判成非交易日跳过）
export TZ=Asia/Shanghai

HOUR_CN=$(date "+%H")
NOW_CN=$(date "+%Y-%m-%d %H:%M:%S %Z")
echo "[$(TZ=America/Los_Angeles date '+%F %T %Z')] 北京时间 $NOW_CN"

# strip leading zero (08 -> 8) for shell numeric compare
HOUR_CN=$((10#$HOUR_CN))

if [ "$HOUR_CN" -ge 6 ] && [ "$HOUR_CN" -lt 12 ]; then
    echo "→ 北京早盘窗口，运行 brief.py --market-only"
    exec .venv/bin/python -u brief.py --market-only
elif [ "$HOUR_CN" -ge 14 ] && [ "$HOUR_CN" -lt 22 ]; then
    echo "→ 北京收盘后窗口，运行 brief.py（默认 full）"
    exec .venv/bin/python -u brief.py
else
    echo "→ 非触发窗口（北京 $HOUR_CN 时），跳过本次"
    exit 0
fi
