#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python mcp_servers/monitor_server.py &
monitor_pid=$!
uv run python mcp_servers/log_server.py &
logs_pid=$!
trap 'kill "$monitor_pid" "$logs_pid" 2>/dev/null || true' EXIT INT TERM
wait
