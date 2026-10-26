#!/bin/sh
# OpsPaw 修复连接器安装脚本。
#
# 由后台「配置修复 Agent」生成,在服务所在机器上执行:
#   curl -fsSL <endpoint>/repair-connector/install.sh | sh -s -- \
#     --endpoint <endpoint> --token <一次性令牌> --platform-id <平台ID> --name <连接器名>
#
# 它做四件事:下载连接器 → 写配置(0600) → 停掉旧进程 → 后台拉起。
# 不写系统目录、不装依赖、不用 root:全部落在 ~/.opspaw-repair/ 下。
set -eu

ENDPOINT=""
TOKEN=""
PLATFORM_ID=""
NAME=""

while [ $# -gt 0 ]; do
  case "$1" in
    --endpoint)    ENDPOINT="${2:-}"; shift 2 ;;
    --token)       TOKEN="${2:-}"; shift 2 ;;
    --platform-id) PLATFORM_ID="${2:-}"; shift 2 ;;
    --name)        NAME="${2:-}"; shift 2 ;;
    *) echo "未知参数:$1"; exit 1 ;;
  esac
done

[ -n "$ENDPOINT" ] || { echo "缺少 --endpoint"; exit 1; }
[ -n "$TOKEN" ] || { echo "缺少 --token"; exit 1; }
[ -n "$PLATFORM_ID" ] || { echo "缺少 --platform-id"; exit 1; }

command -v python3 >/dev/null 2>&1 || { echo "这台机器没有 python3,先装一个再跑"; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "这台机器没有 curl"; exit 1; }

# 非 root 运行:root 会把连接器权限放大到整机,不是必须就别这么干
if [ "$(id -u)" -eq 0 ] && [ "${OPSPAW_ALLOW_ROOT:-}" != "1" ]; then
  echo "检测到 root 用户。建议用普通用户运行(非 root);确实要用 root 请加 OPSPAW_ALLOW_ROOT=1"
  exit 1
fi

# 运行时预检:这台机器靠什么干活?docker(compose)还是 k8s(kubectl)。
# 两个都没有的话,连接器能上线、也能只读探活,但任何修复动作都执行不了——
# 与其装完静默失败让人猜,不如现在把原因说清楚。
RUNTIME=""
if command -v docker >/dev/null 2>&1; then
  if docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    RUNTIME="docker"
  elif sudo -n docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    RUNTIME="docker(需要 sudo -n)"
  fi
fi
if [ -z "$RUNTIME" ] && command -v kubectl >/dev/null 2>&1; then
  if kubectl version --request-timeout=5s -o json >/dev/null 2>&1; then
    RUNTIME="k8s(kubectl)"
  fi
fi
if [ -z "$RUNTIME" ]; then
  echo "⚠ 这台机器既没有可用的 docker,也没有能连上集群的 kubectl。"
  echo "  连接器能装上、能上线、能只读探活,但修复动作一条都执行不了。"
  echo "  · 装了 docker 但没权限 → 把当前用户加进 docker 组,或配好 sudo 免密"
  echo "  · 用 k8s → 确认 kubectl 在当前上下文能连上集群(kubectl get pods 能出结果)"
  if [ "${OPSPAW_ALLOW_NO_RUNTIME:-}" != "1" ]; then
    echo "  确认仍然要装,请加 OPSPAW_ALLOW_NO_RUNTIME=1 再执行一次。"
    exit 1
  fi
  echo "  已按 OPSPAW_ALLOW_NO_RUNTIME=1 继续安装(只读可用,写动作不可用)。"
else
  echo "检测到运行时:$RUNTIME"
fi

DIR="$HOME/.opspaw-repair"
mkdir -p "$DIR"

echo "下载连接器 …"
curl -fsSL "$ENDPOINT/repair-connector/connector.py" -o "$DIR/connector.py.new"
curl -fsSL "$ENDPOINT/repair-connector/manifest.json" -o "$DIR/manifest.json"
EXPECT=$(python3 -c "import json;print(json.load(open('$DIR/manifest.json'))['sha256'])")
ACTUAL=$(python3 -c "import hashlib;print(hashlib.sha256(open('$DIR/connector.py.new','rb').read()).hexdigest())")
if [ "$EXPECT" != "$ACTUAL" ]; then
  rm -f "$DIR/connector.py.new" "$DIR/manifest.json"
  echo "校验失败:下载到的连接器与清单不一致(传输被截断或被改写),已中止"
  exit 1
fi
mv "$DIR/connector.py.new" "$DIR/connector.py"

# 配置写盘后再启动:令牌只进这个 0600 文件,不出现在常驻进程的命令行里
ENDPOINT="$ENDPOINT" TOKEN="$TOKEN" PLATFORM_ID="$PLATFORM_ID" NAME="$NAME" DIR="$DIR" \
python3 - <<'PY'
import json, os
name = os.environ.get("NAME") or ""
cfg = {"endpoint": os.environ["ENDPOINT"], "token": os.environ["TOKEN"],
       "platform_id": int(os.environ["PLATFORM_ID"])}
if name:
    cfg["name"] = name
p = os.path.join(os.environ["DIR"], "config.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=1)
os.chmod(p, 0o600)
PY

if [ -f "$DIR/connector.pid" ]; then
  OLD=$(cat "$DIR/connector.pid" 2>/dev/null || true)
  if [ -n "${OLD:-}" ] && kill -0 "$OLD" 2>/dev/null; then
    kill "$OLD" 2>/dev/null || true
    sleep 1
  fi
fi

# 扫一遍残留进程:同一个连接器身份跑两条轮询会互相顶掉,表现是服务端一直回
# "取任务失败 HTTP 500"(2026-10-21 实测:旧进程 pid 没记进 pid 文件,新装完两条并存)
if command -v pgrep >/dev/null 2>&1; then
  for pid in $(pgrep -f "[c]onnector\.py" 2>/dev/null || true); do
    [ "$pid" = "$$" ] && continue
    kill "$pid" 2>/dev/null || true
  done
  sleep 1
fi

echo "启动连接器 …"
cd "$DIR"
nohup python3 "$DIR/connector.py" --config "$DIR/config.json" >> "$DIR/connector.log" 2>&1 &
echo $! > "$DIR/connector.pid"
sleep 2

echo ""
echo "连接器目录:$DIR"
echo "日志:$DIR/connector.log"
echo "--- 最近日志 ---"
tail -n 8 "$DIR/connector.log" 2>/dev/null || true
echo "---------------"
echo "回到 OpsPaw 后台,状态会自动变成「在线」。"
