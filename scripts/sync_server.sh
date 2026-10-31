#!/usr/bin/env bash
# 同步本地代码到跑批服务器(54.252.67.182)。
# 用法: ./scripts/sync_server.sh            只同步代码
#       ./scripts/sync_server.sh --restart  同步完顺带重启服务器上的服务端
# 说明: .env / 数据库 / .venv 不在同步范围,服务器上有自己的一份,别指望脚本带过去。
set -euo pipefail

SERVER="ubuntu@54.252.67.182"
KEY="/Users/Mai/Desktop/opspaw.pem"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

rsync -az --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  --exclude '/data/' \
  --exclude 'log/' \
  --exclude '.env' \
  --exclude '*.sqlite3' \
  --exclude '*.db' \
  --exclude '.workbuddy/' \
  --exclude '.pytest_cache/' \
  -e "ssh -i $KEY -o StrictHostKeyChecking=no" \
  "$ROOT/" "$SERVER:~/OpsPaw/"

echo "✅ 代码已同步 → $SERVER:~/OpsPaw"

if [[ "${1:-}" == "--restart" ]]; then
  ssh -i "$KEY" -o StrictHostKeyChecking=no "$SERVER" '
    sudo systemctl restart opspaw-server
    sleep 5
    code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ || echo fail)
    echo "服务端健康检查: $code (200=正常)"
  '
fi
