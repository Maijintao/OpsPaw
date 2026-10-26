"""修复工具:查能力 + 受限只读查询 + 派出修复 agent。

修复的决策权在修复 agent(repair/agent.py):用户确认根因后由 start_repair 直接派单,
agent 自主复核现场、决策动作、执行、验证并写回执——聊天层不再生成"计划等确认"。
两条硬门槛写在工具里,不只写在提示词里:
1. 派单必须有用户已确认根因的诊断(会话内最近一单,或显式给 diagnosis_id);
2. agent 能用的动作仍然收口在服务端目录 + 连接器能力交集里。
"""
from typing import Annotated

from langchain_core.tools import InjectedToolArg, tool
from pydantic import Field

from app.db import repository
from app.repair import catalog, gateway, plans
from app.tools import registry


@tool
async def repair_capabilities(
    platform: Annotated[str, Field(description="平台名;留空则列出所有已接入平台")] = "",
) -> dict:
    """查询修复能力和受限诊断动作:哪些平台接了连接器、是否在线、支持哪些动作。
    需要日志、事件或资源信息时,用 repair_read 按 actions 清单里的动作查询,不要自造命令。"""
    targets = [r for r in await repository.list_targets() if r.enabled]
    if platform:
        targets = [r for r in targets if r.name == platform]
    out = []
    for t in targets:
        conns = [c for c in await repository.list_repair_connectors(t.id)
                 if c.status != "revoked"]
        if not conns:
            continue
        out.append({"platform": t.name, "target_id": t.id,
                    "connectors": [{
                        "id": c.id, "name": c.name,
                        "online": gateway.is_online(c.id),
                        "actions": [x.get("action") for x in (c.capabilities or [])
                                    if isinstance(x, dict)],
                    } for c in conns]})
    return {"platforms": out,
            "actions": [a for a in catalog.view() if a["read_only"]],
            "note": "只有 online 的连接器才能生成计划;动作以 actions 清单为准"}


@tool
async def start_repair(
    diagnosis_id: Annotated[str, Field(description="要修复的诊断任务号;不填则自动用当前会话里最近一次已确认根因的诊断")] = "",
    conversation_id: Annotated[int, InjectedToolArg] = 0,
) -> dict:
    """派出修复 agent 接管修复(根因已被用户在卡片上确认后)。修复 agent 自主完成:
    复核现场 → 决策修复动作 → 执行 → 再次确认结果 → 写修复回执,全程在前端修复卡上
    实时滚动。调用前提:诊断卡片上的根因已被用户确认;没确认就先让用户确认,不要代替。
    调用后只需一句话告知已派出,不要反问、不要再确认一遍、不要预告结果、
    不要生成修复计划或提交变更申请。"""
    from app.repair import agent as repair_agent
    diag_id = (diagnosis_id or "").strip()
    try:
        if diag_id:
            out = await repair_agent.start_for_diagnosis(diag_id)
        else:
            out = await repair_agent.start_for_conversation(conversation_id or None)
        return {**out, "status": "running",
                "note": "修复 agent 已派出,进度与回执由前端修复卡展示"}
    except repair_agent.RepairError as exc:
        return {"ok": False, "error": str(exc)}


registry.register(registry.spec_from_langchain_tool(repair_capabilities, source="builtin"))
# 15s:派单要现查平台/连接器(服务发现走 Jaeger),默认 5s 会把它切掉
registry.register(registry.spec_from_langchain_tool(
    start_repair, source="builtin", inject_conversation=True, timeout=15.0))


# 只读查询走连接器长轮询,gateway 侧超时 20s,工具侧放宽到 30s 防被提前掐断
@tool
async def repair_read(
    action: Annotated[str, Field(description="只读动作,只能从 repair_capabilities 返回的只读动作里选:"
                                                 "list_services / inspect_service / service_logs / "
                                                 "service_events / service_resources / "
                                                 "list_flags / get_flag / service_connections")],
    service: Annotated[str, Field(description="服务名(容器名);list_services 不需要")] = "",
    platform: Annotated[str, Field(description="平台名;留空且无法从服务名定位时,"
                                               "只有一个在线平台会自动选定")] = "",
    tail: Annotated[int, Field(description="service_logs 的行数,1-200")] = 100,
    limit: Annotated[int, Field(description="service_events 的条数,1-100")] = 20,
) -> dict:
    """查询修复连接器所在机器的受限只读信息:服务列表、运行状态、最近日志、Docker 事件或资源占用。
    这是查询,不产生任何变更;修复由 start_repair 派出的修复 agent 执行,不在这里做。"""
    definition = catalog.READ_ACTIONS.get(action)
    if definition is None:
        return {"ok": False,
                "error": f"「{action}」不是只读动作;只能用 repair_capabilities 返回的只读动作查询,"
                         "写动作必须走修复计划"}

    # 参数按服务端目录口径收口,与后台只读 API 同一套规则:目录没定义的不传,越界就拒
    params: dict = {}
    if "service" in definition["params"]:
        if not plans.SERVICE_RE.fullmatch(service or ""):
            return {"ok": False, "error": "服务名不合法(只允许字母数字和 _.-,最长 64)"}
        params["service"] = service
    if "tail" in definition["params"] and not 1 <= tail <= 200:
        return {"ok": False, "error": "tail 范围是 1-200"}
    if "tail" in definition["params"]:
        params["tail"] = tail
    if "limit" in definition["params"] and not 1 <= limit <= 100:
        return {"ok": False, "error": "limit 范围是 1-100"}
    if "limit" in definition["params"]:
        params["limit"] = limit

    # 定位平台:名字匹配 → 服务名经服务发现反查 → 只有唯一在线平台时自动选定
    targets = [r for r in await repository.list_targets() if r.enabled]
    target = next((r for r in targets if platform and r.name == platform), None)
    if target is None and service:
        try:
            from app.tools import service_catalog
            services, _ = await service_catalog.discover_services()
            hit = service_catalog.find_service(services, service)
        except Exception:  # noqa: BLE001 服务发现失败不致命,走后面的在线平台兜底
            hit = None
        if hit is not None:
            target = next((r for r in targets if r.name == hit["target"]), None)
    if target is None:
        candidates: list = []
        online_targets: list = []
        for r in targets:
            conns = [c for c in await repository.list_repair_connectors(r.id)
                     if c.status != "revoked"]
            if not conns:
                continue
            candidates.append(r)
            if any(gateway.is_online(c.id) for c in conns):
                online_targets.append(r)
        if len(online_targets) == 1:
            target = online_targets[0]
        elif not online_targets and len(candidates) == 1:
            # 只有一个接了连接器的平台:选中它,让后面的检查报出"连接器不在线"这种明确错误
            target = candidates[0]
    if target is None:
        return {"ok": False,
                "error": "定位不到平台:请传 platform 参数,或确认有平台接了在线的修复连接器"}

    conns = [c for c in await repository.list_repair_connectors(target.id)
             if c.status != "revoked" and gateway.is_online(c.id)]
    if not conns:
        return {"ok": False, "error": f"平台「{target.name}」的修复连接器不在线,查不了"}
    if len(conns) > 1:
        return {"ok": False,
                "error": "这个平台有多个连接器在线,需要用户到后台指定是哪一台"}
    declared = {c.get("action") for c in (conns[0].capabilities or [])
                if isinstance(c, dict)}
    if action not in declared:
        return {"ok": False, "error": f"连接器没有声明支持「{action}」,查不了"}
    try:
        return await gateway.call(conns[0].id, action, params, timeout=20)
    except gateway.ConnectorOffline as exc:
        return {"ok": False, "error": str(exc)}


registry.register(registry.spec_from_langchain_tool(repair_read, source="builtin", timeout=30.0))
