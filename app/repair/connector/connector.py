#!/usr/bin/env python3
"""OpsPaw 修复连接器(单文件,只用标准库)。

它跑在**服务所在的那台机器**上,主动向 OpsPaw 出站长轮询,不需要开放任何入站端口。
它只实现固定动作,不接收命令字符串、不提供 Shell/SSH/exec:

    list_services     列出本机容器
    inspect_service   查看指定服务的运行状态
    service_logs      查看有限日志片段(只读)
    service_events    查看有限 Docker 事件(只读)
    service_resources 查看当前资源计数(只读)
    restart_service   重启容器(写,必须带计划与审批)
    start_service     启动容器(写,必须带计划与审批)
    stop_service      停止容器(写,必须带计划与审批)
    recreate_service  用已有 Compose 配置重建服务(写,必须带计划与审批)

连接器不是通用远程执行代理。写动作只接受 plan_id / approval_id,参数只有服务名,
模型拿不到主机、容器 ID 或任意命令;授权判断(计划 hash、审批、冷却、单并发)在服务端。

用法(正常由安装脚本拉起,不用手敲):
    python3 connector.py --config ~/.opspaw-repair/config.json
"""
import argparse
import json
import os
import platform
import re
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.2.0"   # 1.2.0:支持 k8s 运行时(kubectl),动作目录不变、按运行时上报能力
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

CAPABILITIES = [
    {"action": "list_services", "read_only": True,
     "description": "列出本机容器(名称/状态/镜像)"},
    {"action": "inspect_service", "read_only": True,
     "description": "查看指定服务的运行状态、启动时间与重启次数"},
    {"action": "service_logs", "read_only": True,
     "description": "查看指定服务最近的有限日志片段"},
    {"action": "service_events", "read_only": True,
     "description": "查看指定服务最近的有限 Docker 事件"},
    {"action": "service_resources", "read_only": True,
     "description": "查看指定服务当前资源使用计数"},
    {"action": "list_flags", "read_only": True,
     "description": "列出 flagd 全部 feature flag 的当前值与可选变体"},
    {"action": "get_flag", "read_only": True,
     "description": "查看单个 feature flag 的当前值、可选变体与 targeting"},
    {"action": "service_connections", "read_only": True,
     "description": "从指定容器内探测目标 host:port 的 TCP 连通性"},
    {"action": "restart_service", "read_only": False, "requires_approval": True,
     "risk": "中", "description": "重启指定容器"},
    {"action": "start_service", "read_only": False, "requires_approval": True,
     "risk": "中", "description": "启动已停止的容器"},
    {"action": "stop_service", "read_only": False, "requires_approval": True,
     "risk": "高", "description": "停止容器(服务会中断)"},
    {"action": "recreate_service", "read_only": False, "requires_approval": True,
     "risk": "高", "description": "使用已有 Compose 配置重建指定服务"},
    {"action": "set_flag", "read_only": False, "requires_approval": True,
     "risk": "中", "description": "把指定 feature flag 切到指定变体(旧值记入审计可回切)"},
    {"action": "restore_flags", "read_only": False, "requires_approval": True,
     "risk": "中", "description": "把 flagd 全部开关复位 off(有快照则整文件还原)"},
    {"action": "truncate_service_logs", "read_only": False, "requires_approval": True,
     "risk": "中", "description": "截断指定容器的日志文件(释放磁盘,历史不可找回)"},
]

# 写动作:必须带计划与审批编号。连接器不共享服务端数据库,只能校验"带了没有",
# 真正的授权判断在服务端(计划 hash + 审批 + 冷却 + 单并发)。
WRITE_ACTIONS = {"restart_service", "start_service", "stop_service", "recreate_service",
                 "set_flag", "restore_flags", "truncate_service_logs"}
READ_ACTIONS = {"list_services", "inspect_service", "service_logs",
                "service_events", "service_resources",
                "list_flags", "get_flag", "service_connections"}

# flagd 配置的值规范(与服务端 catalog 同一口径);文件路径不写死,运行时自动发现
FLAG_RE = re.compile(r"^[A-Za-z0-9_]+$")
VARIANT_RE = re.compile(r"^[A-Za-z0-9_%.-]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg, flush=True)


def http_json(method: str, url: str, payload: dict | None = None,
              timeout: float = 35.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "User-Agent": f"opspaw-repair-connector/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode() or "{}"
    return json.loads(body)


# ---------- 平台适配:第一版只做 compose/docker,且只读 ----------

# 固定参数列表调用,永远不走 shell——这是"不是远程 Shell"的底线。
# docker 前缀在第一次成功时定下来:普通用户不在 docker 组时,只读动作允许退到 sudo -n
# (不弹密码);写动作将来必须单独声明,不能默认继承这条提权路径。
_DOCKER = ["docker"]


def _docker(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run([*_DOCKER, *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def _docker_capped(args: list[str], keep_bytes: int,
                   timeout: float = 15.0, prefix: list[str] | None = None) -> tuple[int, str, bool]:
    """跑固定 docker 命令,全量读输出但内存里只保留末尾 keep_bytes。

    subprocess.run(capture_output=True) 会把子进程输出整个吃进内存再截断;
    docker logs / events 的输出量由容器自己决定,没有天然上限。这里边读边丢
    头部、只留末尾——日志和事件本来就只关心最近那段,内存占用恒定,不随
    容器输出量涨。stdout/stderr 合并读取;超时按失败处理(退出码非 0)。
    返回 (退出码, 文本, 更早的输出是否被丢弃)。
    """
    proc = subprocess.Popen([*(prefix or _DOCKER), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    fd = proc.stdout.fileno()
    buf = bytearray()
    dropped = False
    deadline = time.monotonic() + timeout

    def _stop() -> int:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return proc.returncode if proc.returncode is not None else -1

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                return _stop(), buf.decode("utf-8", errors="replace"), True
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > keep_bytes:
                del buf[:-keep_bytes]
                dropped = True
        code = proc.wait(timeout=5)
        return code, buf.decode("utf-8", errors="replace"), dropped
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass


def _docker_ready() -> tuple[bool, str]:
    global _DOCKER
    last = "docker 不可用"
    for prefix in (["docker"], ["sudo", "-n", "docker"]):
        try:
            r = subprocess.run([*prefix, "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=6, check=False)
        except FileNotFoundError:
            if prefix[0] == "docker":
                return False, "这台机器没装 docker 命令"
            last = "没有可用的 sudo"
            continue
        except (PermissionError, OSError) as exc:
            # 提权兜底不可用时保留普通 docker 的原始错误——那个才是用户该看的原因
            if prefix[0] == "docker":
                last = f"{type(exc).__name__}: {exc}"
            continue
        except subprocess.TimeoutExpired:
            last = "docker 命令超时"
            continue
        if r.returncode == 0:
            if prefix[0] == "sudo" and _DOCKER[0] != "sudo":
                log("本机 docker 需要提权,已切到 sudo -n(只跑固定动作,不走 shell)")
            _DOCKER = prefix
            return True, (r.stdout or "").strip()
        last = (r.stderr or "docker 不可用").strip()[:200]
    return False, last


# ---------- 运行时后端二:k8s(kubectl) ----------
# 甲方不一定用 docker compose 部署。连接器启动时探测本机运行时:docker 优先(compose 形态),
# 没有 docker 就试 kubectl(k8s 形态)。**动作目录不变**,只是底下换一套命令实现,
# 服务端照旧按"目录 ∩ 连接器声明"派活——连接器声明什么,服务端就只会派什么。
#
# 纪律与 docker 那条一样:固定参数列表调用、不拼 shell、服务名先过 NAME_RE。
# k8s 里"服务"对应的单位是 Deployment(没有就退回同名 Pod 前缀匹配)。

RUNTIME = "docker"          # docker | k8s | none
_KUBECTL: list[str] = ["kubectl"]
_K8S_NS: str = ""           # 空 = 用 kubectl 当前上下文的命名空间


def _kubectl(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    prefix = [*_KUBECTL, *(["--namespace", _K8S_NS] if _K8S_NS else [])]
    return subprocess.run([*prefix, *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def _docker_capped_kubectl(args: list[str], keep_bytes: int,
                           timeout: float = 20.0) -> tuple[int, str, bool]:
    """kubectl 版的有上限读取(日志可能很长,和 docker logs 一样的处理)。"""
    prefix = [*_KUBECTL, *(["--namespace", _K8S_NS] if _K8S_NS else [])]
    return _docker_capped(args, keep_bytes, timeout=timeout, prefix=prefix)


def _kubectl_ready() -> tuple[bool, str]:
    """kubectl 在不在、连不连得上集群。版本查询会真的打 API Server,连不上就返回非 0。"""
    global _KUBECTL
    last = "kubectl 不可用"
    for prefix in (["kubectl"], ["sudo", "-n", "kubectl"]):
        try:
            r = subprocess.run([*prefix, "version", "--request-timeout=5s", "-o", "json"],
                               capture_output=True, text=True, timeout=12, check=False)
        except FileNotFoundError:
            if prefix[0] == "kubectl":
                return False, "这台机器没装 kubectl"
            last = "没有可用的 sudo"
            continue
        except (PermissionError, OSError) as exc:
            if prefix[0] == "kubectl":
                last = f"{type(exc).__name__}: {exc}"
            continue
        except subprocess.TimeoutExpired:
            last = "kubectl 命令超时"
            continue
        if r.returncode == 0:
            _KUBECTL = prefix
            return True, "kubectl 可用"
        last = (r.stderr or "kubectl 连不上集群").strip()[:200]
    return False, last


def detect_runtime() -> str:
    """启动时探一次:docker 能用就用 docker(compose 形态最常见),否则试 k8s。
    两个都不行就是 none——注册照常,但能力清单为空,服务端不会派任何动作过来。"""
    global RUNTIME
    ok, _ = _docker_ready()
    if ok:
        RUNTIME = "docker"
        return RUNTIME
    ok, _ = _kubectl_ready()
    RUNTIME = "k8s" if ok else "none"
    return RUNTIME


def _k8s_items(kind: str) -> list[dict]:
    r = _kubectl(["get", kind, "-o", "json"])
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout or "{}").get("items") or []
    except ValueError:
        return []


def _k8s_pods(service: str = "") -> list[dict]:
    """按服务名找 pod:先看 label(app / app.kubernetes.io/name / k8s-app),
    再退回"pod 名以服务名开头"(Deployment 生成的 pod 就是 svc-xxxxx-yyyyy)。"""
    pods = _k8s_items("pods")
    if not service:
        return pods
    out = []
    for p in pods:
        md = p.get("metadata") or {}
        labels = md.get("labels") or {}
        name = md.get("name") or ""
        if (labels.get("app") == service or labels.get("app.kubernetes.io/name") == service
                or labels.get("k8s-app") == service or name.startswith(service + "-")):
            out.append(p)
    return out


def _k8s_container_states(pod: dict) -> list[dict]:
    return ((pod.get("status") or {}).get("containerStatuses") or [])


def _k8s_inspect(service: str) -> dict:
    """k8s 版"看服务状态":字段与服务端验证口径对齐
    (running / state / started_at / restart_count / health),换后端不改判分。"""
    pods = _k8s_pods(service)
    if not pods:
        return {"ok": True, "status": "success",
                "message": f"集群里没有名为 {service} 的 pod",
                "data": {"service": service, "found": False, "runtime": "k8s"}}
    pod = pods[0]
    st = pod.get("status") or {}
    states = _k8s_container_states(pod)
    running = st.get("phase") == "Running" and all(c.get("ready") is True for c in states)
    started = st.get("startTime") or ""
    if started:
        try:
            from datetime import datetime
            started = datetime.fromisoformat(
                started.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
        except ValueError:
            pass
    restarts = sum(int(c.get("restartCount") or 0) for c in states)
    image = next((c.get("image") for c in states if c.get("image")), "")
    return {"ok": True, "status": "success",
            "message": f"{service} {'运行中' if running else '未运行'}({st.get('phase') or '未知'})",
            "data": {"service": service, "found": True, "running": running,
                     "state": st.get("phase") or "", "started_at": started,
                     "restart_count": restarts, "health": "ready" if running else "not_ready",
                     "pod": (pod.get("metadata") or {}).get("name"),
                     "image": image, "runtime": "k8s"}}


def _k8s_scale(service: str, replicas: int, verb: str) -> dict:
    r = _kubectl(["scale", f"deployment/{service}", f"--replicas={replicas}"], timeout=60)
    if r.returncode != 0:
        return {"ok": False, "status": "failed",
                "message": (r.stderr or f"kubectl scale 失败").strip()[:200], "data": {}}
    return {"ok": True, "status": "success",
            "message": f"已把 {service} 的副本数设为 {replicas}({verb})",
            "data": {"service": service, "replicas": replicas, "runtime": "k8s"}}


def _k8s_rollout_restart(service: str, verb: str) -> dict:
    r = _kubectl(["rollout", "restart", f"deployment/{service}"], timeout=60)
    if r.returncode != 0:
        text = (r.stderr or "").strip()
        if "not found" in text.lower():
            # 没有 Deployment 就退一步删 pod,让控制器重建(裸 pod 删了就没了,所以只删带 owner 的)
            pods = _k8s_pods(service)
            owned = [p for p in pods
                     if ((p.get("metadata") or {}).get("ownerReferences") or [])]
            if not owned:
                return {"ok": False, "status": "failed",
                        "message": f"集群里找不到 {service} 的 Deployment,也没有可重建的 pod",
                        "data": {}}
            name = (owned[0].get("metadata") or {}).get("name") or ""
            r2 = _kubectl(["delete", "pod", name, "--wait=false"], timeout=60)
            if r2.returncode != 0:
                return {"ok": False, "status": "failed",
                        "message": (r2.stderr or "kubectl delete pod 失败").strip()[:200], "data": {}}
            return {"ok": True, "status": "success",
                    "message": f"已删除 pod {name},交给控制器重建({verb})",
                    "data": {"service": service, "pod": name, "runtime": "k8s"}}
        return {"ok": False, "status": "failed", "message": text[:200], "data": {}}
    return {"ok": True, "status": "success",
            "message": f"已触发 {service} 滚动重启({verb})",
            "data": {"service": service, "runtime": "k8s"}}


# ---- k8s 版动作实现:名字与 docker 版一一对应,返回结构保持一致 ----

def k8s_list_services(_args: dict) -> dict:
    pods = _k8s_pods()
    if not pods:
        return {"ok": False, "status": "failed",
                "message": "kubectl 没返回任何 pod(集群连不上,或命名空间里是空的)", "data": {}}
    items = []
    for p in pods:
        md, st = p.get("metadata") or {}, p.get("status") or {}
        image = next((c.get("image") for c in _k8s_container_states(p) if c.get("image")), "")
        items.append({"service": md.get("name") or "", "state": st.get("phase") or "Unknown",
                      "image": image})
    return {"ok": True, "status": "success",
            "message": f"集群里共 {len(items)} 个 pod", "data": {"services": items}}


def k8s_inspect_service(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied",
                "message": "服务名不合法(只允许字母数字和 _.-,最长 64)", "data": {}}
    return _k8s_inspect(service)


def k8s_service_logs(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    try:
        tail = int(args.get("tail", 100))
    except (TypeError, ValueError):
        return {"ok": False, "status": "denied", "message": "tail 必须是整数", "data": {}}
    if not 1 <= tail <= 200:
        return {"ok": False, "status": "denied", "message": "tail 范围是 1-200", "data": {}}
    pods = _k8s_pods(service)
    if not pods:
        return {"ok": False, "status": "failed",
                "message": f"集群里找不到 {service} 的 pod", "data": {}}
    pod = (pods[0].get("metadata") or {}).get("name") or ""
    code, text, dropped = _docker_capped_kubectl(
        ["logs", pod, "--tail", str(tail), "--all-containers=true"], keep_bytes=20 * 1024)
    if code != 0:
        return {"ok": False, "status": "failed",
                "message": (text or "kubectl logs 失败").strip()[:200], "data": {}}
    return {"ok": True, "status": "success", "message": f"已读取 {service} 最近日志",
            "data": {"service": service, "logs": text[-20000:], "pod": pod,
                     "truncated": dropped or len(text) > 20000}}


def k8s_service_events(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        return {"ok": False, "status": "denied", "message": "limit 必须是整数", "data": {}}
    if not 1 <= limit <= 100:
        return {"ok": False, "status": "denied", "message": "limit 范围是 1-100", "data": {}}
    r = _kubectl(["get", "events", "--sort-by=.lastTimestamp", "-o", "json"], timeout=25)
    if r.returncode != 0:
        return {"ok": False, "status": "failed",
                "message": (r.stderr or "kubectl get events 失败").strip()[:200], "data": {}}
    try:
        items = json.loads(r.stdout or "{}").get("items") or []
    except ValueError:
        return {"ok": False, "status": "failed", "message": "k8s 事件解析失败", "data": {}}
    hits = []
    for e in items:
        obj = (e.get("involvedObject") or {}).get("name") or ""
        if obj.startswith(service) or service in obj:
            hits.append({"reason": e.get("reason"), "message": e.get("message"),
                         "type": e.get("type"), "count": e.get("count"),
                         "last": e.get("lastTimestamp") or e.get("eventTime")})
    return {"ok": True, "status": "success",
            "message": f"已读取 {len(hits)} 条与 {service} 相关的事件",
            "data": {"service": service, "events": hits[-limit:],
                     "truncated": len(hits) > limit}}


def k8s_service_resources(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    r = _kubectl(["top", "pods", "--no-headers"], timeout=25)
    if r.returncode != 0:
        return {"ok": False, "status": "failed",
                "message": "集群没有装 metrics-server,kubectl top 用不了(资源这一维在 k8s 下暂时看不到)"
                           if "metrics" in (r.stderr or "").lower()
                           else (r.stderr or "kubectl top 失败").strip()[:200],
                "data": {}}
    rows = [ln.split() for ln in (r.stdout or "").splitlines() if ln.strip()]
    hit = [c for c in rows if c and c[0].startswith(service)]
    if not hit:
        return {"ok": True, "status": "success",
                "message": f"kubectl top 里没有 {service} 的读数",
                "data": {"service": service, "stats": {}}}
    cpu, mem = (hit[0] + ["", "", ""])[1], (hit[0] + ["", "", ""])[2]
    return {"ok": True, "status": "success", "message": f"已读取 {service} 资源使用",
            "data": {"service": service, "stats": {"CPUPerc": cpu, "MemUsage": mem,
                                                   "pod": hit[0][0]} }}


def k8s_restart_service(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    return _k8s_rollout_restart(service, "重启")


def k8s_start_service(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    return _k8s_scale(service, 1, "启动")


def k8s_stop_service(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    return _k8s_scale(service, 0, "停止")


def k8s_recreate_service(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    return _k8s_rollout_restart(service, "重建")


# ---- k8s 下的 feature flag:走 ConfigMap ----
# flagd 在 k8s 里通常把配置挂在 ConfigMap 上,容器以文件挂载。改 ConfigMap 后
# kubelet 同步到容器里有几十秒延迟,flagd 再热加载——比 compose 慢,但不需要进容器。

def _k8s_flagd_cm() -> tuple[str, str, dict] | tuple[None, None, None]:
    """找那个"data 里装着 flagd 配置"的 ConfigMap:值能解析成 JSON 且带 flags 键。"""
    for cm in _k8s_items("configmaps"):
        md = cm.get("metadata") or {}
        for key, val in (cm.get("data") or {}).items():
            if not isinstance(val, str) or '"flags"' not in val:
                continue
            try:
                data = json.loads(val)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("flags"), dict):
                return md.get("name") or "", key, data
    return None, None, None


def _k8s_flag_write(cm: str, key: str, data: dict) -> dict:
    patch = json.dumps({"data": {key: json.dumps(data, ensure_ascii=False)}},
                       ensure_ascii=False)
    r = _kubectl(["patch", "configmap", cm, "--type", "merge", "-p", patch], timeout=40)
    if r.returncode != 0:
        return {"ok": False, "status": "failed",
                "message": (r.stderr or "patch configmap 失败").strip()[:200], "data": {}}
    return {"ok": True, "status": "success", "message": "ConfigMap 已更新", "data": {}}


def k8s_list_flags(_args: dict) -> dict:
    cm, key, data = _k8s_flagd_cm()
    if cm is None:
        return {"ok": False, "status": "failed",
                "message": "集群里找不到 flagd 配置的 ConfigMap(没有开关可管)", "data": {}}
    flags = {k: _flag_summary(v) for k, v in sorted((data.get("flags") or {}).items())}
    dirty = [k for k, s in flags.items()
             if s["current"] not in (None, "off") and "off" in s["variants"]]
    return {"ok": True, "status": "success",
            "message": f"共 {len(flags)} 个开关" + (f",{len(dirty)} 个非 off" if dirty else ",全部 off"),
            "data": {"path": f"configmap/{cm}#{key}", "flags": flags, "non_off": dirty}}


def k8s_get_flag(args: dict) -> dict:
    name = str(args.get("flag") or "").strip()
    cm, key, data = _k8s_flagd_cm()
    if cm is None:
        return {"ok": False, "status": "failed", "message": "找不到 flagd 配置的 ConfigMap", "data": {}}
    f = (data.get("flags") or {}).get(name)
    if f is None:
        return {"ok": False, "status": "failed", "message": f"没有名为 {name} 的开关", "data": {}}
    return {"ok": True, "status": "success", "message": f"已读取开关 {name}",
            "data": {"flag": name, **_flag_summary(f)}}


def k8s_set_flag(args: dict) -> dict:
    name = str(args.get("flag") or "").strip()
    variant = str(args.get("variant") or "").strip()
    cm, key, data = _k8s_flagd_cm()
    if cm is None:
        return {"ok": False, "status": "failed", "message": "找不到 flagd 配置的 ConfigMap", "data": {}}
    f = (data.get("flags") or {}).get(name)
    if f is None:
        return {"ok": False, "status": "failed", "message": f"没有名为 {name} 的开关", "data": {}}
    variants = f.get("variants") or {}
    if variant not in variants:
        return {"ok": False, "status": "denied",
                "message": f"开关 {name} 没有变体 {variant},可选:{'/'.join(variants) or '无'}"[:200],
                "data": {}}
    old = f.get("defaultVariant")
    f["defaultVariant"] = variant
    res = _k8s_flag_write(cm, key, data)
    if not res["ok"]:
        return res
    return {"ok": True, "status": "success",
            "message": f"开关 {name}: {old} → {variant}(ConfigMap 已更新,容器内生效有几十秒延迟)",
            "data": {"flag": name, "from": old, "to": variant, "path": f"configmap/{cm}#{key}"}}


def k8s_restore_flags(_args: dict) -> dict:
    cm, key, data = _k8s_flagd_cm()
    if cm is None:
        return {"ok": False, "status": "failed", "message": "找不到 flagd 配置的 ConfigMap", "data": {}}
    changed = []
    for name, f in (data.get("flags") or {}).items():
        if "off" in (f.get("variants") or {}) and f.get("defaultVariant") != "off":
            f["defaultVariant"] = "off"
            changed.append(name)
    if not changed:
        return {"ok": True, "status": "success", "message": "所有开关本来就是 off", "data": {"changed": []}}
    res = _k8s_flag_write(cm, key, data)
    if not res["ok"]:
        return res
    return {"ok": True, "status": "success",
            "message": f"已把 {len(changed)} 个开关复位为 off", "data": {"changed": changed}}


def action_list_services(_args: dict) -> dict:
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    r = _docker(["ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Image}}"])
    if r.returncode != 0:
        return {"ok": False, "status": "failed",
                "message": (r.stderr or "docker ps 失败").strip()[:200], "data": {}}
    items = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            items.append({"service": parts[0], "state": parts[1],
                          "image": parts[2] if len(parts) > 2 else ""})
    return {"ok": True, "status": "success",
            "message": f"本机共 {len(items)} 个容器", "data": {"services": items}}


def action_inspect_service(args: dict) -> dict:
    service = str(args.get("service") or "").strip()
    if not NAME_RE.match(service):
        return {"ok": False, "status": "denied",
                "message": "服务名不合法(只允许字母数字和 _.-,最长 64)", "data": {}}
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    r = _docker(["inspect", "--format", "{{json .State}}", service])
    if r.returncode != 0:
        text = (r.stderr or "").strip()
        if "No such object" in text or "No such container" in text:
            return {"ok": True, "status": "success",
                    "message": f"本机没有名为 {service} 的容器",
                    "data": {"service": service, "found": False}}
        return {"ok": False, "status": "failed", "message": text[:200], "data": {}}
    try:
        state = json.loads(r.stdout or "{}")
    except ValueError:
        return {"ok": False, "status": "failed", "message": "容器状态解析失败", "data": {}}
    started = state.get("StartedAt") or ""
    # docker 返回的是 UTC RFC3339,直接透传会在前端显示成"11:24 启动"(实际 19:24)。
    # 连接器知道宿主机本地时区,在这里转成带本地偏移的时间再回。
    if started:
        try:
            from datetime import datetime
            started = datetime.fromisoformat(
                started.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
        except ValueError:
            pass   # 格式没认出来就原样回,不编时间
    running = bool(state.get("Running"))
    health = (state.get("Health") or {}).get("Status")
    return {"ok": True, "status": "success", "message":
            f"{service} {'运行中' if running else '未运行'}({state.get('Status') or '未知'})"
            + (f",健康检查 {health}" if health else ""),
            "data": {"service": service, "found": True, "running": running,
                     "state": state.get("Status") or "",
                     "started_at": started,
                     "restart_count": state.get("RestartCount"),
                     "exit_code": state.get("ExitCode"),
                     "oom_killed": state.get("OOMKilled"),
                     "health": health,
                     "error": state.get("Error") or ""}}


def _service_name(args: dict) -> str | None:
    service = str(args.get("service") or "").strip()
    return service if NAME_RE.fullmatch(service) else None


def action_service_logs(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    try:
        tail = int(args.get("tail", 100))
    except (TypeError, ValueError):
        return {"ok": False, "status": "denied", "message": "tail 必须是整数", "data": {}}
    if not 1 <= tail <= 200:
        return {"ok": False, "status": "denied", "message": "tail 范围是 1-200", "data": {}}
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    code, text, dropped = _docker_capped(["logs", "--tail", str(tail), service],
                                         keep_bytes=20 * 1024, timeout=15)
    if code != 0:
        return {"ok": False, "status": "failed",
                "message": (text or "docker logs 失败").strip()[:200], "data": {}}
    return {"ok": True, "status": "success", "message": f"已读取 {service} 最近日志",
            "data": {"service": service, "logs": text[-20000:],
                     "truncated": dropped or len(text) > 20000}}


def action_service_events(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        return {"ok": False, "status": "denied", "message": "limit 必须是整数", "data": {}}
    if not 1 <= limit <= 100:
        return {"ok": False, "status": "denied", "message": "limit 范围是 1-100", "data": {}}
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    code, text, dropped = _docker_capped(
        ["events", "--since", "1h", "--until", "0s", "--filter",
         f"container={service}", "--format", "{{json .}}"],
        keep_bytes=64 * 1024, timeout=15)
    if code != 0:
        return {"ok": False, "status": "failed",
                "message": (text or "docker events 失败").strip()[:200], "data": {}}
    if dropped:
        # 只保留末尾时第一行可能被拦腰截断,丢掉不完整的首行再解析
        cut = text.find("\n")
        text = text[cut + 1:] if cut >= 0 else ""
    lines = text.splitlines()
    try:
        events = [json.loads(line) for line in lines[-limit:]]
    except ValueError:
        return {"ok": False, "status": "failed", "message": "Docker 事件解析失败", "data": {}}
    return {"ok": True, "status": "success", "message": f"已读取 {len(events)} 条事件",
            "data": {"service": service, "events": events,
                     "truncated": dropped or len(lines) > limit}}


def action_service_resources(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    r = _docker(["stats", "--no-stream", "--format", "{{json .}}", service], timeout=15)
    if r.returncode != 0:
        return {"ok": False, "status": "failed", "message": (r.stderr or "docker stats 失败").strip()[:200], "data": {}}
    try:
        stats = json.loads((r.stdout or "{}").strip().splitlines()[0])
    except (ValueError, IndexError):
        return {"ok": False, "status": "failed", "message": "Docker 资源信息解析失败", "data": {}}
    return {"ok": True, "status": "success", "message": f"已读取 {service} 资源使用",
            "data": {"service": service, "stats": stats}}


def action_recreate_service(args: dict) -> dict:
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    inspect = _docker(["inspect", "--format", "{{json .Config.Labels}}", service])
    if inspect.returncode != 0:
        return {"ok": False, "status": "failed", "message": (inspect.stderr or "服务检查失败").strip()[:200], "data": {}}
    try:
        labels = json.loads(inspect.stdout or "{}") or {}
    except ValueError:
        return {"ok": False, "status": "failed", "message": "Compose 标签解析失败", "data": {}}
    project = labels.get("com.docker.compose.project")
    workdir = labels.get("com.docker.compose.project.working_dir")
    config_files = labels.get("com.docker.compose.project.config_files")
    compose_service = labels.get("com.docker.compose.service")
    if not all(isinstance(x, str) and x for x in (project, workdir, config_files, compose_service)):
        return {"ok": False, "status": "denied", "message": "目标不是可识别的 Compose 服务,拒绝重建", "data": {}}
    configs = config_files.split(",")
    if len(configs) != 1 or not os.path.isabs(workdir) or not os.path.isabs(configs[0]):
        return {"ok": False, "status": "denied", "message": "Compose 配置范围不明确,拒绝重建", "data": {}}
    if not os.path.isfile(configs[0]) or not os.path.isdir(workdir):
        return {"ok": False, "status": "denied", "message": "Compose 配置文件或目录不存在", "data": {}}
    compose = ["compose", "--project-directory", workdir, "-f", configs[0],
               "-p", project, "up", "-d", "--no-deps", "--force-recreate", compose_service]
    result = _docker(compose, timeout=120)
    if result.returncode != 0:
        return {"ok": False, "status": "failed", "message": (result.stderr or "Compose 重建失败").strip()[:200], "data": {"service": service}}
    return {"ok": True, "status": "success", "message": f"已请求重建 Compose 服务 {compose_service}",
            "data": {"service": service, "compose_service": compose_service}}


# ---------- flagd 动作:配置文件自动发现,路径不写死 ----------

_flagd_cache: tuple[str, float] | None = None   # (路径, 发现时刻);缓存 60s,文件挪走能跟上


def _flagd_path() -> str:
    """定位 flagd 的配置文件:从 flagd 容器的 bind mount 里找,不依赖任何约定路径。

    为什么自动发现:换一套部署(路径变了/换了项目)连接器照样能用,不需要甲方适配。
    找不到时抛异常,由动作层如实回报。"""
    global _flagd_cache
    if _flagd_cache and time.monotonic() - _flagd_cache[1] < 60:
        return _flagd_cache[0]
    ok, detail = _docker_ready()
    if not ok:
        raise RuntimeError(detail)
    for cand in ("flagd", "flagd-ui"):
        r = _docker(["inspect", "--format", "{{json .Mounts}}", cand])
        if r.returncode != 0:
            continue
        try:
            mounts = json.loads(r.stdout or "[]") or []
        except ValueError:
            continue
        for m in mounts:
            src, dst = str(m.get("Source") or ""), str(m.get("Destination") or "")
            if not m.get("RW") or not src:
                continue
            # 形态一:直接 bind 了单个 json 文件
            if src.endswith(".json") and os.path.isfile(src):
                _flagd_cache = (src, time.monotonic())
                return src
            # 形态二:bind 了配置目录(如 …/flagd -> /etc/flagd),找目录里的 flagd json
            if os.path.isdir(src) and ("flagd" in src or dst.endswith("flagd")):
                prefs = [p for p in (
                    os.path.join(src, "demo.flagd.json"),
                    os.path.join(src, "flagd.json")) if os.path.isfile(p)]
                if prefs:
                    _flagd_cache = (prefs[0], time.monotonic())
                    return prefs[0]
                jsons = sorted(f for f in os.listdir(src)
                               if f.endswith(".json") and not f.startswith("."))
                if len(jsons) == 1:
                    _flagd_cache = (os.path.join(src, jsons[0]), time.monotonic())
                    return _flagd_cache[0]
    raise RuntimeError("这台机器上没找到 flagd 配置文件(需要名为 flagd 的容器且配置为 bind mount)")


def _load_flagd(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_flagd(path: str, data: dict) -> None:
    tmp = path + ".tmp-opspaw"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _flag_summary(f: dict) -> dict:
    return {"current": f.get("defaultVariant"),
            "variants": sorted((f.get("variants") or {}).keys()),
            "has_targeting": "targeting" in f}


def action_list_flags(_args: dict) -> dict:
    try:
        path = _flagd_path()
        data = _load_flagd(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "message": str(exc)[:200], "data": {}}
    flags = {k: _flag_summary(v) for k, v in sorted((data.get("flags") or {}).items())}
    dirty = [k for k, s in flags.items()
             if s["current"] not in (None, "off") and "off" in s["variants"]]
    return {"ok": True, "status": "success",
            "message": f"共 {len(flags)} 个开关" + (f",{len(dirty)} 个非 off" if dirty else ",全部 off"),
            "data": {"path": path, "flags": flags, "non_off": dirty}}


def action_get_flag(args: dict) -> dict:
    flag = str(args.get("flag") or "").strip()
    if not FLAG_RE.fullmatch(flag):
        return {"ok": False, "status": "denied", "message": "开关名不合法", "data": {}}
    try:
        path = _flagd_path()
        data = _load_flagd(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "message": str(exc)[:200], "data": {}}
    f = (data.get("flags") or {}).get(flag)
    if f is None:
        return {"ok": True, "status": "success",
                "message": f"开关 {flag} 不存在",
                "data": {"flag": flag, "found": False}}
    return {"ok": True, "status": "success",
            "message": f"{flag} 当前 = {f.get('defaultVariant')}",
            "data": {"flag": flag, "found": True, "path": path, **_flag_summary(f)}}


def action_set_flag(args: dict) -> dict:
    flag = str(args.get("flag") or "").strip()
    variant = str(args.get("variant") or "").strip()
    if not FLAG_RE.fullmatch(flag):
        return {"ok": False, "status": "denied", "message": "开关名不合法", "data": {}}
    if not VARIANT_RE.fullmatch(variant):
        return {"ok": False, "status": "denied", "message": "变体名不合法", "data": {}}
    try:
        path = _flagd_path()
        data = _load_flagd(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "message": str(exc)[:200], "data": {}}
    f = (data.get("flags") or {}).get(flag)
    if f is None:
        return {"ok": False, "status": "failed", "message": f"开关 {flag} 不存在", "data": {}}
    if variant not in (f.get("variants") or {}):
        return {"ok": False, "status": "failed",
                "message": f"变体 {variant} 不存在,可选:" + ",".join(sorted((f.get("variants") or {}).keys())),
                "data": {}}
    previous = f.get("defaultVariant")
    f["defaultVariant"] = variant
    _save_flagd(path, data)
    return {"ok": True, "status": "success",
            "message": f"{flag}: {previous} -> {variant}",
            "data": {"flag": flag, "variant": variant, "previous": previous, "path": path}}


def action_restore_flags(_args: dict) -> dict:
    try:
        path = _flagd_path()
        data = _load_flagd(path)
        changed: list[str] = []
        for k, f in sorted((data.get("flags") or {}).items()):
            if f.get("defaultVariant") not in (None, "off") and "off" in (f.get("variants") or {}):
                f["defaultVariant"] = "off"
                changed.append(k)
        if changed:
            _save_flagd(path, data)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "failed", "message": str(exc)[:200], "data": {}}
    return {"ok": True, "status": "success",
            "message": "已复位:" + (", ".join(changed) if changed else "无(全部已是 off)"),
            "data": {"restored": changed, "path": path}}


def action_service_connections(args: dict) -> dict:
    """从容器内探测 host:port TCP 连通性。

    为什么敢做 exec:命令是**完全固定的模板**(`/dev/tcp` 探测一行),host/port 经白名单
    校验后作为参数代入,模型没有任何位置可以注入命令文本——和 docker restart 同一个
    安全级别,只是参数多两个。容器里没有 bash 时如实失败(不退到 sh,兼容性谎言更危险)。"""
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    host = str(args.get("target_host") or "").strip()
    try:
        port = int(args.get("target_port"))
    except (TypeError, ValueError):
        return {"ok": False, "status": "denied", "message": "端口必须是整数", "data": {}}
    if not HOST_RE.fullmatch(host):
        return {"ok": False, "status": "denied", "message": "目标主机名不合法", "data": {}}
    if not 1 <= port <= 65535:
        return {"ok": False, "status": "denied", "message": "端口范围是 1-65535", "data": {}}
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    # 两级固定模板探测(参数全是白名单校验过的值,没有任何自由文本):
    # 先 bash /dev/tcp,容器里没 bash 再试 busybox/普通 nc;都不可用如实失败
    attempts = (
        (["exec", service, "bash", "-c",
          f"exec 3<>/dev/tcp/{host}/{port} && echo OPEN || echo CLOSED"], "bash"),
        (["exec", service, "nc", "-z", "-w", "2", host, str(port)], "nc"),
    )
    last_err = ""
    for cmd, kind in attempts:
        r = _docker(cmd, timeout=12)
        text = (r.stdout or "").strip()
        if kind == "bash":
            if r.returncode == 0 and ("OPEN" in text or "CLOSED" in text):
                opened = "OPEN" in text
                return {"ok": True, "status": "success",
                        "message": f"{service} -> {host}:{port} {'通' if opened else '不通'}",
                        "data": {"service": service, "target_host": host,
                                 "target_port": port, "open": opened, "via": "bash"}}
            last_err = (r.stderr or text or "").strip()
            if "executable file not found" not in last_err and "not found" not in last_err:
                # bash 在但探测出错(如 DNS 解析失败):bash 的 /dev/tcp 连不上会走 CLOSED 分支,
                # 走不到这里;这里基本就是容器没有 bash,继续试 nc
                continue
        else:  # nc:连通返回码 0,不通非 0(能跑到这里说明 nc 存在)
            opened = (r.returncode == 0)
            return {"ok": True, "status": "success",
                    "message": f"{service} -> {host}:{port} {'通' if opened else '不通'}",
                    "data": {"service": service, "target_host": host,
                             "target_port": port, "open": opened, "via": "nc"}}
    return {"ok": False, "status": "failed",
            "message": f"容器 {service} 里既没有 bash 也没有 nc,无法探测(第一版限制)",
            "data": {}}


def action_truncate_service_logs(args: dict) -> dict:
    """截断容器日志文件(磁盘被写满的缓解动作)。

    日志文件归 root,截断需要宿主机免密 sudo;没有就如实失败,不静默跳过。"""
    service = _service_name(args)
    if not service:
        return {"ok": False, "status": "denied", "message": "服务名不合法", "data": {}}
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    r = _docker(["inspect", "--format", "{{.LogPath}}", service])
    if r.returncode != 0:
        return {"ok": False, "status": "failed", "message": (r.stderr or "容器检查失败").strip()[:200], "data": {}}
    log_path = (r.stdout or "").strip()
    if not log_path:
        return {"ok": False, "status": "failed", "message": "拿不到日志文件路径", "data": {}}
    try:
        t = subprocess.run(["sudo", "-n", "truncate", "-s", "0", "--", log_path],
                           capture_output=True, text=True, timeout=15, check=False)
    except FileNotFoundError:
        return {"ok": False, "status": "failed", "message": "宿主机没有 sudo 命令", "data": {}}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "unknown", "message": "截断日志超时", "data": {}}
    if t.returncode != 0:
        return {"ok": False, "status": "failed",
                "message": ("截断失败:" + (t.stderr or "需要免密 sudo")).strip()[:200], "data": {}}
    return {"ok": True, "status": "success", "message": f"已清空 {service} 的日志文件",
            "data": {"service": service, "log_path": log_path}}


def _write_action(verb: str, args: dict, timeout: float = 60.0) -> dict:
    """写动作的唯一入口:固定动词 + 校验过的服务名,参数列表调用,永不拼 shell。"""
    service = str(args.get("service") or "").strip()
    if not NAME_RE.match(service):
        return {"ok": False, "status": "denied",
                "message": "服务名不合法(只允许字母数字和 _.-,最长 64)", "data": {}}
    ok, detail = _docker_ready()
    if not ok:
        return {"ok": False, "status": "failed", "message": detail, "data": {}}
    r = _docker([verb, service], timeout=timeout)
    if r.returncode != 0:
        return {"ok": False, "status": "failed",
                "message": (r.stderr or f"docker {verb} 失败").strip()[:200], "data": {}}
    return {"ok": True, "status": "success",
            "message": f"已对 {service} 执行 docker {verb}",
            "data": {"service": service, "action": verb}}


def action_restart_service(args: dict) -> dict:
    return _write_action("restart", args)


def action_start_service(args: dict) -> dict:
    return _write_action("start", args)


def action_stop_service(args: dict) -> dict:
    return _write_action("stop", args)


ACTIONS = {
    "list_services": action_list_services,
    "inspect_service": action_inspect_service,
    "service_logs": action_service_logs,
    "service_events": action_service_events,
    "service_resources": action_service_resources,
    "list_flags": action_list_flags,
    "get_flag": action_get_flag,
    "service_connections": action_service_connections,
    "restart_service": action_restart_service,
    "start_service": action_start_service,
    "stop_service": action_stop_service,
    "recreate_service": action_recreate_service,
    "set_flag": action_set_flag,
    "restore_flags": action_restore_flags,
    "truncate_service_logs": action_truncate_service_logs,
}

# k8s 后端能做的动作:清容器日志、容器内连通探测在 k8s 下没有对应做法,如实不提供。
K8S_ACTIONS = {
    "list_services": k8s_list_services,
    "inspect_service": k8s_inspect_service,
    "service_logs": k8s_service_logs,
    "service_events": k8s_service_events,
    "service_resources": k8s_service_resources,
    "list_flags": k8s_list_flags,
    "get_flag": k8s_get_flag,
    "set_flag": k8s_set_flag,
    "restore_flags": k8s_restore_flags,
    "restart_service": k8s_restart_service,
    "start_service": k8s_start_service,
    "stop_service": k8s_stop_service,
    "recreate_service": k8s_recreate_service,
}


def host_info() -> dict:
    return {"hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "runtime": RUNTIME,
            **({"namespace": _K8S_NS} if _K8S_NS else {}),
            "connector_version": VERSION}


def current_actions() -> dict:
    """当前运行时能执行的动作表。docker 走 compose 那套,k8s 走 kubectl 那套。"""
    if RUNTIME == "k8s":
        return K8S_ACTIONS
    if RUNTIME == "docker":
        return ACTIONS
    return {}


def capabilities() -> list[dict]:
    """按当前运行时上报能力——服务端只会派这里声明过的动作。
    k8s 比 docker 少两个:清容器日志(k8s 由 kubelet 轮转)、容器内连通探测(要进容器跑命令)。"""
    allowed = current_actions()
    return [c for c in CAPABILITIES if c["action"] in allowed]


def execute(command: dict) -> dict:
    tool = command.get("tool") or ""
    args = command.get("args") or {}
    if tool in WRITE_ACTIONS and not (command.get("plan_id") and command.get("approval_id")):
        return {"ok": False, "status": "denied",
                "message": "写动作必须带计划与审批编号", "data": {}}
    handler = current_actions().get(tool)
    if handler is None:
        return {"ok": False, "status": "denied",
                "message": f"当前运行时({RUNTIME})不支持这个动作:{tool}", "data": {}}
    try:
        return handler(args if isinstance(args, dict) else {})
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "unknown", "message": "动作执行超时", "data": {}}
    except Exception as exc:  # noqa: BLE001  单次动作失败不许弄死连接器
        return {"ok": False, "status": "failed",
                "message": f"{type(exc).__name__}: {exc}"[:200], "data": {}}


def register(cfg: dict) -> dict:
    payload = {"token": cfg["token"], "platform_id": cfg["platform_id"],
               "name": cfg.get("name") or socket.gethostname(),
               "host_info": host_info(), "capabilities": capabilities()}
    return http_json("POST", cfg["endpoint"].rstrip("/") + "/api/repair/connect", payload)


def save_cfg(path: str, cfg: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="OpsPaw 修复连接器")
    ap.add_argument("--config", default=os.path.expanduser("~/.opspaw-repair/config.json"))
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    endpoint = cfg["endpoint"].rstrip("/")

    # 先探运行时:决定这一轮上报哪些能力、动作走 docker 还是 kubectl
    global _K8S_NS
    _K8S_NS = str(cfg.get("namespace") or "").strip()
    rt = detect_runtime()
    if rt == "docker":
        log(f"运行时:docker(前缀 {' '.join(_DOCKER)})")
    elif rt == "k8s":
        log(f"运行时:k8s(kubectl{' --namespace ' + _K8S_NS if _K8S_NS else ''}),"
            f"可用动作 {len(K8S_ACTIONS)} 个")
    else:
        log("运行时:没找到 docker 也没找到可用的 kubectl —— 连接器能上线,但不会收到任何动作")

    def _stop(*_):
        log("收到退出信号,连接器停止")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)

    if not cfg.get("connector_id") or not cfg.get("secret"):
        log(f"首次启动,向 {endpoint} 注册(平台 {cfg['platform_id']})…")
        try:
            reg = register(cfg)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode()).get("detail") or ""
            except Exception:  # noqa: BLE001
                pass
            log(f"注册失败:HTTP {exc.code} {detail or exc.reason}")
            return 2
        except Exception as exc:  # noqa: BLE001
            log(f"注册失败:{type(exc).__name__}: {exc}")
            return 2
        cfg["connector_id"] = reg["connector_id"]
        cfg["secret"] = reg["secret"]
        cfg.pop("token", None)          # 令牌是一次性的,换到身份密钥后立刻丢掉
        save_cfg(args.config, cfg)
        log(f"注册成功,连接器 {cfg['connector_id']} 已上线")

    cid, secret = cfg["connector_id"], cfg["secret"]
    hb_url = f"{endpoint}/api/repair/heartbeat"
    poll_url = f"{endpoint}/api/repair/poll"
    res_url = f"{endpoint}/api/repair/result"
    log(f"连接器 {cid} 开始工作(平台 {cfg['platform_id']})")

    while True:
        try:
            hb = http_json("POST", hb_url,
                           {"connector_id": cid, "secret": secret,
                            "capabilities": capabilities(), "host_info": host_info()},
                           timeout=15)
            if hb.get("revoked"):
                log("连接器已被平台撤销,停止运行")
                return 3
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                log(f"身份被拒绝(HTTP {exc.code}),停止运行")
                return 3
            log(f"心跳失败 HTTP {exc.code},5 秒后重试")
            time.sleep(5)
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"心跳失败:{type(exc).__name__},5 秒后重试")
            time.sleep(5)
            continue

        try:
            got = http_json("POST", poll_url,
                            {"connector_id": cid, "secret": secret, "timeout": 25},
                            timeout=40)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                log(f"身份被拒绝(HTTP {exc.code}),停止运行")
                return 3
            log(f"取任务失败 HTTP {exc.code},5 秒后重试")
            time.sleep(5)
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"取任务失败:{type(exc).__name__},5 秒后重试")
            time.sleep(5)
            continue

        command = got.get("command")
        if not command:
            continue
        log(f"收到动作 {command.get('tool')} {command.get('args') or {}}")
        result = execute(command)
        log(f"动作结果 {command.get('tool')}:{result.get('status')} {result.get('message', '')}")
        try:
            http_json("POST", res_url, {"connector_id": cid, "secret": secret,
                                        "request_id": command.get("request_id"),
                                        "ok": result.get("ok"), "status": result.get("status"),
                                        "message": result.get("message"), "data": result.get("data")},
                      timeout=15)
        except Exception as exc:  # noqa: BLE001  结果回传失败下一轮会再取任务,不阻塞
            log(f"回传结果失败:{type(exc).__name__}")


if __name__ == "__main__":
    sys.exit(main())
