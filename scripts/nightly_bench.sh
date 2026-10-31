#!/usr/bin/env bash
# 夜间自动跑批:全量 12 组(检测/定位/修复)。由 systemd timer 在凌晨拉起。
#
# 为什么在服务器上跑、不在本机跑:数据源(19090/32769/3100)是隧道在这一头的端口,
# 本机连不到;跑批进程必须和服务端、隧道在同一台机器上。
#
# 前置检查任何一条不过就原地退出,绝不碰靶子——半夜没人看着,宁可这晚不跑,
# 也不能在数据面断着的情况下跑三小时拿一堆废数据。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export PYTHONUTF8=1

mkdir -p "$ROOT/log"
# 服务器是 UTC,日志统一按北京时间打,早上看不用自己换算
LOG="$ROOT/log/bench_night_$(TZ=Asia/Shanghai date +%Y%m%d-%H%M).log"
exec >> "$LOG" 2>&1

# .env 必须加载:数据库地址、Milvus、模型 key 都在里面,不加载跑批连不上库
if [ -f "$ROOT/.env" ]; then
  set -a
  . "$ROOT/.env"
  set +a
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
fi

say() { echo "[$(TZ=Asia/Shanghai date '+%F %T')] $*"; }
fail() { say "✗ 前置检查失败:$*"; say "本次不跑,靶子未动。"; exit 1; }

say "===== 夜间跑批开始 ====="

# 前置自检统一交给 bench_preflight.py:服务端 / 隧道三口 / 并发跑批 / 归因依赖链 /
# 模型通道 / 历史深度 / 靶子开关。任何一条不过就不跑——半夜没人看着,
# 宁可不跑,也不能跑三小时拿一堆"看着正常其实没考到"的数据。
say "前置自检中…"
if ! .venv/bin/python scripts/bench_preflight.py; then
  fail "前置自检未通过(原因见上面逐条输出)"
fi

# 干跑:只验证前置检查与路径,不真跑(BENCH_DRY=1 ./scripts/nightly_bench.sh)
if [ "${BENCH_DRY:-}" = "1" ]; then
  say "干跑模式:检查全过,不执行跑批"
  exit 0
fi
say "开始跑批:全量 12 组 × 检测/定位/修复,settle=300s,预计 3 小时左右"

.venv/bin/python scripts/aiopslab_bench.py run --settle 300
rc=$?
say "跑批进程结束,退出码 $rc"

# 把总表抄到日志尾部:早上看日志就能看到分数,不用再进目录翻
LATEST="$(ls -td data/aiopslab/runs/*/ 2>/dev/null | head -1)"
if [ -n "${LATEST:-}" ]; then
  say "结果目录:$LATEST"
  if [ -f "${LATEST}summary.json" ]; then
    say "----- summary.json -----"
    cat "${LATEST}summary.json"
    echo
    say "------------------------"
  else
    say "没找到 summary.json(可能中途异常退出,看上面的日志)"
  fi
fi
exit "$rc"
