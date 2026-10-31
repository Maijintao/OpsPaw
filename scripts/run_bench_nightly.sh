#!/usr/bin/env bash
# 凌晨自动跑批:前置自检 → 跑批(探测/定位/修复) → 收尾复位。
#
# 为什么要有这个壳,而不是直接 crontab 一行 run:
#   1) 跑批要 source .env(模型 key、隧道地址),cron 环境是干净的;
#   2) 跑之前必须自检——2026-10-20 那次就是缺 pytz 导致 9 组归因没跑起来,
#      退出码还是 0,从外面完全看不出来;
#   3) 必须防重入:一轮 70~120 分钟,前一轮没完下一轮又起,会互相踩靶子;
#   4) 无论跑批成功失败,最后都要把 flag 复位,否则第二天靶子还带着故障。
#
# 用法(装 crontab 后自动跑,也可以手工执行):
#   ~/OpsPaw/scripts/run_bench_nightly.sh
#
# 日志:data/aiopslab/logs/nightly_YYYYmmdd_HHMMSS.log(保留 14 天)
set -uo pipefail

ROOT="$HOME/OpsPaw"
LOG_DIR="$ROOT/data/aiopslab/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$LOG_DIR/nightly_${STAMP}.log"

# 全部输出进日志:cron 没有终端,不落盘就等于没跑
exec >>"$LOG" 2>&1
echo "===== 凌晨跑批开始 $(date '+%F %T') ====="

cd "$ROOT" || { echo "进不去 $ROOT,放弃"; exit 1; }

# 防重入:上一轮没跑完就跳过这一轮
exec 9>"$LOG_DIR/.bench.lock"
if ! flock -n 9; then
    echo "已有一轮跑批在进行,本次跳过"
    exit 0
fi

# cron 的环境变量是干净的:必须自己加载,并清掉可能存在的代理
set -a
# shellcheck disable=SC1091
source .env
set +a
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export PYTHONUTF8=1 PYTHONPATH=.

echo "--- 前置自检 ---"
if ! .venv/bin/python scripts/bench_preflight.py; then
    echo "自检未通过,本轮不跑。原因见上方。"
    exit 1
fi

echo "--- 开始跑批(探测 / 定位 / 修复)---"
.venv/bin/python scripts/aiopslab_bench.py run
RC=$?
echo "跑批退出码: $RC"

echo "--- 收尾:靶子开关复位 ---"
.venv/bin/python scripts/aiopslab_bench.py restore || echo "⚠️ 复位失败,需要人工检查靶子"

# 日志只留 14 天
find "$LOG_DIR" -name 'nightly_*.log' -mtime +14 -delete 2>/dev/null || true

echo "===== 结束 $(date '+%F %T') 退出码 $RC ====="
exit "$RC"
