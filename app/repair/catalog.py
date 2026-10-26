"""修复动作目录:服务端唯一一份"能做什么"的定义。

连接器可以声明自己支持哪些动作,但最终能力必须同时满足目录和连接器声明。
动作参数由这里统一规范,不开放 shell、exec 或任意命令。

两类动作形态:
- 容器类:参数是 service,预检查/验证围绕容器运行状态;
- flag 类:参数是 flag/variant(或无参),针对 flagd 配置文件,预检查/验证围绕开关值。
  AIOpsLab 的全部故障注入都是 flag 形态,这类动作是 mitigation 的主战场;
  set_flag 写前的旧值记进审计,天然可回滚。
"""

# 所有动作都使用结构化参数。写动作必须走计划 + 审批,
# 连接器收到时还必须带 plan_id / approval_id。
ACTIONS: dict[str, dict] = {
    "restart_service": {
        "label": "重启服务", "risk": "中", "read_only": False,
        "params": ("service",), "cooldown_s": 300, "verify": "running_after",
        "impact": "服务短暂中断", "preflight": "容器存在且正在运行",
        "desc": "重启指定容器;期间该服务短暂不可用",
    },
    "start_service": {
        "label": "启动服务", "risk": "中", "read_only": False,
        "params": ("service",), "cooldown_s": 120, "verify": "running",
        "desc": "启动已停止的容器",
    },
    "stop_service": {
        "label": "停止服务", "risk": "高", "read_only": False,
        "params": ("service",), "cooldown_s": 120, "verify": "stopped",
        "desc": "停止容器;该服务会中断,只有确认要下线时才用",
    },
    "recreate_service": {
        "label": "重建服务", "risk": "高", "read_only": False,
        "params": ("service",), "cooldown_s": 600, "verify": "running_after",
        "desc": "使用容器已有 Compose 项目配置重建指定服务;可能造成短暂中断",
    },
    "set_flag": {
        "label": "切换功能开关", "risk": "中", "read_only": False,
        "params": ("flag", "variant"), "cooldown_s": 30, "verify": "flag_value",
        "impact": "对应功能行为立即改变(flagd 热加载,不重启容器)",
        "preflight": "开关存在且目标变体合法",
        "desc": "把指定 feature flag 切到指定变体(如把故障开关设回 off);"
                "写前的旧值记进审计,可再切回去",
    },
    "restore_flags": {
        "label": "复位全部开关", "risk": "中", "read_only": False,
        "params": (), "cooldown_s": 300, "verify": "flags_off",
        "impact": "所有 feature flag 回到 off(有干净快照则整文件还原)",
        "preflight": "flagd 配置文件可读",
        "desc": "把 flagd 全部开关复位:有干净快照就整文件还原,"
                "没有就把非 off 的开关逐个设回 off",
    },
    "truncate_service_logs": {
        "label": "清理服务日志", "risk": "中", "read_only": False,
        "params": ("service",), "cooldown_s": 600, "verify": "ok",
        "impact": "该容器的日志历史被清空,清掉前无法找回",
        "preflight": "容器存在",
        "desc": "截断指定容器的日志文件,释放磁盘(disk stress 类故障的缓解动作);"
                "需要宿主机免密 sudo,没有就如实失败",
    },
}

# 只读动作也必须经过这个目录和连接器能力交集,但不进入审批状态机。
READ_ACTIONS: dict[str, dict] = {
    "list_services": {
        "label": "列出服务", "params": (),
        "desc": "列出连接器所在机器的容器",
    },
    "inspect_service": {
        "label": "查看服务状态", "params": ("service",),
        "desc": "查看运行、健康、退出和重启信息",
    },
    "service_logs": {
        "label": "查看服务日志", "params": ("service", "tail"),
        "desc": "查看指定服务最近的有限日志片段",
    },
    "service_events": {
        "label": "查看服务事件", "params": ("service", "limit"),
        "desc": "查看指定服务最近的有限 Docker 事件",
    },
    "service_resources": {
        "label": "查看资源使用", "params": ("service",),
        "desc": "查看指定服务当前 CPU、内存和网络计数",
    },
    "list_flags": {
        "label": "列出功能开关", "params": (),
        "desc": "列出 flagd 全部 feature flag 的当前值与可选变体",
    },
    "get_flag": {
        "label": "查看功能开关", "params": ("flag",),
        "desc": "查看单个 feature flag 的当前值、可选变体与 targeting 规则",
    },
    "service_connections": {
        "label": "探测服务连通", "params": ("service", "target_host", "target_port"),
        "desc": "从指定容器内探测目标 host:port 的 TCP 连通性(定位依赖连不上类故障)",
    },
}


def is_write(action: str) -> bool:
    return action in ACTIONS


def is_read(action: str) -> bool:
    return action in READ_ACTIONS


def definition(action: str) -> dict | None:
    return ACTIONS.get(action) or READ_ACTIONS.get(action)


def label(action: str) -> str:
    return (definition(action) or {}).get("label", action)


def risk(action: str) -> str:
    return (ACTIONS.get(action) or {}).get("risk", "低")


def cooldown(action: str) -> int:
    return int((ACTIONS.get(action) or {}).get("cooldown_s", 0))


def view() -> list[dict]:
    """给前端/文档看的完整目录,不暴露可执行命令。"""
    out = []
    for name, item in [(k, v) for k, v in READ_ACTIONS.items()] + list(ACTIONS.items()):
        out.append({"action": name, "label": item["label"],
                    "risk": item.get("risk", "低"),
                    "read_only": not is_write(name),
                    "params": list(item["params"]), "desc": item["desc"],
                    "requires_approval": is_write(name)})
    return out
