#!/usr/bin/env bash
# ch02 验收演示:三条验收标准。需数据库 + make seed + make dev 就绪。
set -euo pipefail
BASE=http://localhost:8000

echo "== 验收1:服务监控(应选中 query_cpu_metrics 并按返回结果回答)=="
curl -s $BASE/api/agent -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","message":"查一下 3003 的 CPU 曲线"}' | python3 -m json.tool

echo ""
echo "== 验收2:值班规范(query_faq 命中)=="
curl -s $BASE/api/agent -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","message":"告警阈值是多少"}' | python3 -m json.tool

echo ""
echo "== 验收3:CPU 多少算高(语义鸿沟:答案在「告警阈值是多少」的 answer,但 keyword「CPU 多少算高」LIKE question 查不到 → 漏召回=预期,留 ch03 向量检索)=="
curl -s $BASE/api/agent -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","message":"CPU 多少算高"}' | python3 -m json.tool
