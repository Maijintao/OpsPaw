"""诊断任务 API:发起(POST)与轮询(GET)。v0 用轮询不做 SSE——卡片 2s 一拉够用。"""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import repository
from app.diagnosis import runner
from app.repair import catalog, plans

router = APIRouter(prefix="/api/diagnosis")


class DiagnosisIn(BaseModel):
    service: str = Field(min_length=1, max_length=128)
    symptom: str = Field(default="", max_length=512)
    conversation_id: int | None = None


class ConfirmIn(BaseModel):
    confirmed: bool


class RepairPlanIn(BaseModel):
    action: str = Field(default="auto", max_length=32)  # "auto" = 系统按容器现状决定动作
    connector_id: str | None = Field(default=None, max_length=32)
    auto: bool = True   # 满足策略就自动执行;不满足原样返回待确认计划


@router.post("")
async def create_diagnosis(body: DiagnosisIn) -> dict:
    """发起诊断任务:立即返回 id,管线后台跑,前端拿 id 轮询事件流。"""
    service = body.service.strip()
    diag_id = runner.start(service, body.symptom.strip(), conversation_id=body.conversation_id)
    await asyncio.sleep(0)   # 让 start 里 create_task 的任务先挂上事件循环
    return {"diagnosis_id": diag_id, "service": service, "status": "running"}


@router.get("/{diag_id}")
async def get_diagnosis(diag_id: str) -> dict:
    # 内存优先(进行中的任务只有内存有);没有再查存档——进程重启后卡片靠这条拿回结论
    run = runner.get(diag_id)
    if run is not None:
        return run
    archived = await repository.get_diagnosis_run(diag_id)
    if archived is not None:
        return archived
    return {"error": "诊断任务不存在(未发起过,或发起于更早且未落库的旧任务)", "code": "not_found"}


async def _load_run(diag_id: str) -> dict | None:
    return runner.get(diag_id) or await repository.get_diagnosis_run(diag_id)


@router.post("/{diag_id}/confirm")
async def confirm_diagnosis(diag_id: str, body: ConfirmIn) -> dict:
    """用户确认/否认根因。确认前结论只是系统分析,不能拿它启动修复。"""
    live = runner.get(diag_id)
    run = live or await repository.get_diagnosis_run(diag_id)
    if run is None:
        raise HTTPException(404, "诊断任务不存在或还没落库")
    if run.get("status") != "done":
        raise HTTPException(409, "诊断还没结束,不能确认")
    verdict = (run.get("result") or {}).get("verdict")
    if verdict not in ("root_cause", "critical_failure"):
        raise HTTPException(409, "诊断结论不是根因,不需要确认")
    saved = await repository.confirm_diagnosis(diag_id, body.confirmed)
    if saved is None:
        raise HTTPException(404, "诊断任务不存在或还没落库")
    if live is not None:
        live["confirmation"] = saved["confirmation"]
    return {"id": diag_id, "confirmation": saved["confirmation"]}


@router.post("/{diag_id}/repair/run")
async def start_repair_agent(diag_id: str) -> dict:
    """启动修复 agent:校验与定位收在 repair/agent.py 的统一入口里,
    这里只做 HTTP 异常转换。修复 agent(LLM 决策)后台自主跑,前端轮询修复卡。"""
    from app.repair import agent as repair_agent

    try:
        out = await repair_agent.start_for_diagnosis(diag_id)
    except repair_agent.RepairError as exc:
        raise HTTPException(409, str(exc))
    await asyncio.sleep(0)   # 让 start 里 create_task 的任务先挂上事件循环
    return {"repair_id": out["repair_id"], "diagnosis_id": diag_id,
            "service": out["service"], "status": "running"}


@router.post("/{diag_id}/repair/plan")
async def create_repair_plan(diag_id: str, body: RepairPlanIn) -> dict:
    """按诊断结论生成修复计划:根因已确认 + 结论是根因 + 有在线连接器,三条缺一不可。

    只生成计划,不执行——执行仍要用户在计划卡上确认。"""
    from app.repair import gateway
    from app.tools import service_catalog

    run = await _load_run(diag_id)
    if run is None:
        raise HTTPException(404, "诊断任务不存在")
    if run.get("status") != "done":
        raise HTTPException(409, "诊断还没结束,不能生成修复计划")
    res = run.get("result") or {}
    if res.get("verdict") not in ("root_cause", "critical_failure"):
        raise HTTPException(409, "诊断结论不是根因,不能自动修复;请走人工处理")
    service = res.get("root_cause_service")
    if not service:
        raise HTTPException(409, "诊断结论没有根因服务,不能生成计划")
    if (run.get("confirmation") or "pending") != "confirmed":
        raise HTTPException(409, "根因还没被确认,不能生成修复计划")

    targets = [r for r in await repository.list_targets() if r.enabled]
    platform = run.get("platform")
    target = next((r for r in targets if platform and r.name == platform), None)
    if target is None:
        services, _ = await service_catalog.discover_services()
        hit = service_catalog.find_service(services, service)
        if hit is not None:
            target = next((r for r in targets if r.name == hit["target"]), None)
    if target is None:
        raise HTTPException(409, "找不到这个服务所属的接入平台")

    conns = [c for c in await repository.list_repair_connectors(target.id)
             if c.status != "revoked"]
    if body.connector_id:
        conns = [c for c in conns if c.id == body.connector_id]
    online = [c for c in conns if gateway.is_online(c.id)]
    if not online:
        raise HTTPException(409, f"平台「{target.name}」还没有在线的修复连接器")
    if len(online) > 1:
        raise HTTPException(409, "这个平台有多个连接器在线,请指定 connector_id 再生成计划")
    conn = online[0]

    narrative = res.get("narrative") or {}
    reason = "；".join(x for x in (narrative.get("fault"), narrative.get("other")) if x)
    reason = reason or f"诊断 {diag_id} 判定根因 {service}({res.get('verdict')})"

    # 动作决定权在系统不在用户:传 "auto" 时按容器现状解析;显式传动作必须是写动作
    # (只读动作是诊断查询,不该出现在修复计划里)。
    action = body.action
    if action in ("", "auto"):
        action, why = await plans.resolve_auto_action(conn.id, service)
        reason = (reason + "；" if reason else "") + "动作决策:" + why
    else:
        if not catalog.is_write(action):
            raise HTTPException(400, "只读动作走诊断查询,不进修复计划;"
                                     "自动修复请传 action=auto 由系统决定")

    try:
        if body.auto:
            return await plans.create_auto(
                target.id, conn.id, action, service, reason,
                diagnosis_id=diag_id, conversation_id=run.get("conversation_id"),
                created_by="diagnosis", verdict=res.get("verdict"),
                fault_type=(res.get("fault_type") or {}).get("type"),
                confidence_tier=(res.get("confidence") or {}).get("tier"))
        return await plans.create(
            target.id, conn.id, action, service, reason,
            diagnosis_id=diag_id, conversation_id=run.get("conversation_id"),
            created_by="diagnosis")
    except plans.PlanError as exc:
        raise HTTPException(exc.status_code, str(exc))
