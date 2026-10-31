#!/usr/bin/env bash
# 验收:两轮流式对话,第二轮须接住第一轮上下文
set -euo pipefail
SID="demo-$$"

echo "=== 第一轮:自报家门+报故障 ==="
curl -sN http://localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d "{\"session_id\": \"$SID\", \"message\": \"我是值班的小麦,3003 服务今天一直报 5xx\"}"
echo; echo "=== 第二轮:考上下文(我叫什么?哪个服务有问题?) ==="
curl -sN http://localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d "{\"session_id\": \"$SID\", \"message\": \"还记得我叫什么、哪个服务有问题吗?\"}"
echo
