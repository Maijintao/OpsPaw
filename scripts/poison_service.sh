#!/usr/bin/env bash
# 靶机服务投毒/恢复脚本(验收演练用)
# 用法:
#   bash scripts/poison_service.sh poison  payment   # 停掉服务制造故障,并验证症状出现
#   bash scripts/poison_service.sh restore payment   # 恢复服务
# 说明:停容器是最确定性的注入方式——挂死服务自身零遥测,由调用方的失败 client span
# 指认(影子节点场景)。恢复即 docker start,无残留。
set -euo pipefail

HOST="${OTEL_TARGET_HOST:-ubuntu@42.193.188.243}"
ACTION="${1:-}"
SERVICE="${2:-}"
WAIT_SECONDS="${WAIT_SECONDS:-90}"

usage() { echo "用法: bash scripts/poison_service.sh poison|restore <服务名>"; exit 1; }
[ -n "$ACTION" ] && [ -n "$SERVICE" ] || usage

remote() { ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "$@"; }

case "$ACTION" in
  poison)
    echo "[1/3] 停止容器 $SERVICE ..."
    remote "docker stop $SERVICE" >/dev/null
    ST=$(remote "docker inspect $SERVICE --format '{{.State.Status}}'")
    echo "    容器状态: $ST"
    [ "$ST" = "exited" ] || { echo "停止失败"; exit 1; }
    echo "[2/3] 等 ${WAIT_SECONDS}s 让错误 span 累积 ..."
    sleep "$WAIT_SECONDS"
    echo "[3/3] 验证症状(查调用方的错误 span 是否点名 $SERVICE)..."
    NOW=$(remote "date -u +%Y-%m-%dT%H:%M:%SZ")
    OLD=$(remote "date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ")
    HITS=$(remote "curl -s -m 10 'http://localhost:32769/jaeger/ui/api/v3/traces?query.serviceName=checkout&query.searchDepth=20&query.startTimeMin=$OLD&query.startTimeMax=$NOW' | grep -oiE 'oteldemo.[A-Za-z]+Service|payment' | sort | uniq -c | sort -rn | head -5")
    echo "checkout 近5分钟错误特征:"
    echo "$HITS"
    echo ""
    echo ">>> 投毒完成:[$SERVICE] 已停机。去 OpsPaw 对话里发起根因分析,"
    echo ">>> 症状建议说「checkout 下单报错」或「frontend 异常」。"
    echo ">>> 修完恢复: bash scripts/poison_service.sh restore $SERVICE"
    echo ">>> (趁故障现场还在,可攒 fixture: uv run python scripts/eval_diagnosis.py snapshot <症状服务> --root $SERVICE)"
    ;;
  restore)
    echo "恢复容器 $SERVICE ..."
    remote "docker start $SERVICE" >/dev/null
    ST=$(remote "docker inspect $SERVICE --format '{{.State.Status}}'")
    echo "当前状态: $ST"
    echo "已恢复。"
    ;;
  *)
    usage
    ;;
esac
