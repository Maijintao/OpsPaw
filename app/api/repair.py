"""修复连接器 API:注册、心跳、长轮询、动作调用与撤销。

连接方向是**连接器主动出站**:甲方机器不需要开任何入站端口,我们也不去访问它的内网。
服务端只做三件事:发一次性令牌、维护会话、把固定动作投递过去。

协议是 MCP 的语义(工具名 + 结构化参数 + request_id 对账),传输用长轮询实现——
这是方案文档 §5.1 里"当前 MCP SDK 不支持服务端反向调用"时的兜底形态,后面换真 MCP
传输时,工具合同和调用方都不用动。
"""
import logging
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.db import repository
from app.repair import catalog, enrollment, gateway, plans

logger = logging.getLogger("app.repair")
router = APIRouter()

_CONNECTOR_DIR = Path(__file__).resolve().parent.parent / "repair" / "connector"


# ---------- 请求体 ----------

class ConnectIn(BaseModel):
    token: str = Field(min_length=8, max_length=128)
    platform_id: int
    name: str = Field(default="", max_length=128)
    host_info: dict = Field(default_factory=dict)
    capabilities: list = Field(default_factory=list)


class HeartbeatIn(BaseModel):
    connector_id: str = Field(min_length=4, max_length=32)
    secret: str = Field(min_length=8, max_length=128)
    capabilities: list | None = None
    host_info: dict | None = None


class PollIn(BaseModel):
    connector_id: str = Field(min_length=4, max_length=32)
    secret: str = Field(min_length=8, max_length=128)
    timeout: int = Field(default=25, ge=1, le=30)


class ResultIn(BaseModel):
    connector_id: str = Field(min_length=4, max_length=32)
    secret: str = Field(min_length=8, max_length=128)
    request_id: str = Field(min_length=4, max_length=64)
    ok: bool = False
    status: str = Field(default="failed", max_length=16)
    message: str = Field(default="", max_length=500)
    data: dict = Field(default_factory=dict)


class InspectIn(BaseModel):
    service: str = Field(min_length=1, max_length=64)
    connector_id: str | None = None


class ReadActionIn(BaseModel):
    action: str = Field(min_length=3, max_length=32)
    service: str | None = Field(default=None, min_length=1, max_length=64)
    tail: int = Field(default=100, ge=1, le=200)
    limit: int = Field(default=20, ge=1, le=100)
    connector_id: str | None = None


class EnrollmentIn(BaseModel):
    """对外地址可由页面覆盖:OpsPaw 跑在本机、连接器跑在云主机时,127.0.0.1 是对方自己,
    得填对方能访问到的地址(公网 IP 或 SSH 反向隧道)。"""
    endpoint: str | None = Field(default=None, max_length=512)


class PlanIn(BaseModel):
    connector_id: str = Field(min_length=4, max_length=32)
    action: str = Field(default="auto", max_length=32)  # "auto" = 按容器现状决定动作
    service: str = Field(min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)
    diagnosis_id: str | None = Field(default=None, max_length=16)


class ExecuteIn(BaseModel):
    approval_id: str = Field(min_length=4, max_length=32)


# ---------- 身份校验 ----------

async def _connector_or_401(connector_id: str, secret: str):
    row = await repository.get_repair_connector(connector_id)
    if row is None or not enrollment.verify_connector_secret(row, secret):
        raise HTTPException(401, "连接器身份校验失败")
    if row.status == "revoked":
        raise HTTPException(403, "连接器已被撤销")
    return row


def _view(row, online: bool) -> dict:
    status = "revoked" if row.status == "revoked" else ("online" if online else "offline")
    return {"id": row.id, "target_id": row.target_id, "name": row.name,
            "status": status, "capabilities": row.capabilities or [],
            "host_info": row.host_info or {},
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None}


# ---------- Admin:平台侧 ----------

@router.get("/api/targets/{target_id}/repair")
async def list_target_connectors(target_id: int) -> dict:
    rows = await repository.list_repair_connectors(target_id)
    views = [_view(r, gateway.is_online(r.id)) for r in rows]
    return {"connectors": views,
            "online": sum(1 for v in views if v["status"] == "online"),
            "total": len(views)}


_ENDPOINT_RE = re.compile(r"^https?://[A-Za-z0-9._\-:\[\]/]+$")


@router.post("/api/targets/{target_id}/repair/enrollment")
async def create_enrollment(target_id: int, request: Request,
                            body: EnrollmentIn | None = None) -> dict:
    """生成一次性接入命令。令牌 30 分钟有效、用一次即废,只绑定这一个平台。"""
    target = await repository.get_target(target_id)
    if target is None:
        raise HTTPException(404, "接入平台不存在")
    base = (body.endpoint or "").strip().rstrip("/") if body else ""
    if base:
        # 地址会被拼进一行 shell 命令,只收最朴素的 URL 形态——分号、空格、引号一律拒绝
        if not _ENDPOINT_RE.match(base):
            raise HTTPException(400, "地址格式不对:只填 http(s)://主机[:端口],不要带路径或空格")
    else:
        base = (settings.repair_public_base_url or str(request.base_url)).rstrip("/")
    issued = await enrollment.issue(target_id)
    command = (f"curl -fsSL {base}/repair-connector/install.sh | sh -s -- "
               f"--endpoint {base} --token {issued['token']} --platform-id {target_id}")
    return {"command": command, "endpoint": base,
            "token_expires_at": issued["expires_at"], "ttl_minutes": issued["ttl_minutes"],
            "platform": target.name}


@router.post("/api/targets/{target_id}/repair/inspect")
async def inspect_service(target_id: int, body: InspectIn) -> dict:
    """走一遍完整链路:后台 → 连接器 → 本机 docker → 结果回填。只读,不产生副作用。"""
    rows = [r for r in await repository.list_repair_connectors(target_id)
            if r.status != "revoked"]
    if body.connector_id:
        rows = [r for r in rows if r.id == body.connector_id]
    row = next((r for r in rows if gateway.is_online(r.id)), None)
    if row is None:
        raise HTTPException(409, "修复连接器不在线:先在服务所在机器上执行接入命令")
    try:
        return await gateway.call(row.id, "inspect_service", {"service": body.service})
    except gateway.ConnectorOffline as exc:
        raise HTTPException(409, str(exc))


@router.post("/api/targets/{target_id}/repair/read")
async def read_repair_action(target_id: int, body: ReadActionIn) -> dict:
    """执行目录中受限只读动作;该入口不能调用任何写动作。"""
    definition = catalog.READ_ACTIONS.get(body.action)
    if definition is None:
        raise HTTPException(400, "这里只允许目录中的只读动作")
    params = {}
    if "service" in definition["params"]:
        if not body.service or not plans.SERVICE_RE.fullmatch(body.service):
            raise HTTPException(400, "服务名不合法")
        params["service"] = body.service
    if "tail" in definition["params"]:
        params["tail"] = body.tail
    if "limit" in definition["params"]:
        params["limit"] = body.limit
    rows = [r for r in await repository.list_repair_connectors(target_id)
            if r.status != "revoked"]
    if body.connector_id:
        rows = [r for r in rows if r.id == body.connector_id]
    row = next((r for r in rows if gateway.is_online(r.id)), None)
    if row is None:
        raise HTTPException(409, "修复连接器不在线")
    declared = {c.get("action") for c in (row.capabilities or []) if isinstance(c, dict)}
    if body.action not in declared:
        raise HTTPException(409, "连接器没有声明支持此只读动作")
    try:
        return await gateway.call(row.id, body.action, params, timeout=20)
    except gateway.ConnectorOffline as exc:
        raise HTTPException(409, str(exc))


@router.post("/api/targets/{target_id}/repair/probe")
async def probe_connector(target_id: int) -> dict:
    """连通性自测:让在线连接器列一遍本机容器,验证整条链路真的通(只读)。"""
    rows = [r for r in await repository.list_repair_connectors(target_id)
            if r.status != "revoked"]
    row = next((r for r in rows if gateway.is_online(r.id)), None)
    if row is None:
        raise HTTPException(409, "修复连接器不在线:先在服务所在机器上执行接入命令")
    try:
        return await gateway.call(row.id, "list_services", {}, timeout=15)
    except gateway.ConnectorOffline as exc:
        raise HTTPException(409, str(exc))


def _execution_view(row) -> dict | None:
    if row is None:
        return None
    return {"id": row.id, "plan_id": row.plan_id, "approval_id": row.approval_id,
            "connector_id": row.connector_id, "status": row.status,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "result": row.result, "verification": row.verification, "error": row.error}


# ---------- 修复动作 / 计划 / 审批 / 执行 ----------

@router.get("/api/repair/actions")
async def list_actions() -> dict:
    """能做什么由服务端目录说了算,连接器只能声明自己支持其中哪些。"""
    return {"actions": catalog.view()}


class ApprovalModeIn(BaseModel):
    mode: str = Field(pattern="^(manual|auto)$")


@router.get("/api/repair/approval-mode")
async def get_approval_mode() -> dict:
    """修复审批模式:manual(默认,要人点头)/ auto(白名单动作免审批直接执行)。"""
    stored = (await repository.get_settings_map()).get(plans.APPROVAL_MODE_KEY)
    return {"mode": "auto" if stored == "auto" else "manual"}


@router.post("/api/repair/approval-mode")
async def set_approval_mode(body: ApprovalModeIn) -> dict:
    """覆盖式保存。安全边界不在这里:动作永远限定在服务端目录+白名单内,
    这里只决定"执行时要不要人点头"。"""
    await repository.upsert_settings({plans.APPROVAL_MODE_KEY: body.mode})
    await repository.add_repair_audit("repair_approval_mode_updated",
                                      payload={"mode": body.mode})
    return {"mode": body.mode}


@router.post("/api/targets/{target_id}/repair/plans")
async def create_plan(target_id: int, body: PlanIn) -> dict:
    action = body.action
    reason = body.reason
    if action in ("", "auto"):
        # 动作由系统按容器现状决定,不让用户挑——手动路径与诊断卡路径同一套规则
        if not body.connector_id:
            raise HTTPException(400, "action=auto 需要指定 connector_id")
        try:
            action, why = await plans.resolve_auto_action(body.connector_id,
                                                          body.service.strip())
        except plans.PlanError as exc:
            raise HTTPException(exc.status_code, str(exc))
        reason = (reason + "；" if reason else "") + "动作决策:" + why
    try:
        return await plans.create(target_id, body.connector_id, action,
                                  body.service.strip(), reason,
                                  body.diagnosis_id, params=body.params)
    except plans.PlanError as exc:
        raise HTTPException(exc.status_code, str(exc))


@router.get("/api/targets/{target_id}/repair/plans")
async def list_plans(target_id: int) -> dict:
    rows = await repository.list_repair_plans(target_id)
    return {"plans": [plans.view(r) for r in rows]}


@router.get("/api/repair/plans/{plan_id}")
async def get_plan(plan_id: str) -> dict:
    plan = await repository.get_repair_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "计划不存在")
    execution = _execution_view(await repository.latest_execution_for_plan(plan_id))
    return plans.view(plan, execution)


@router.post("/api/repair/plans/{plan_id}/approve")
async def approve_plan(plan_id: str) -> dict:
    try:
        return await plans.approve(plan_id)
    except plans.PlanError as exc:
        raise HTTPException(exc.status_code, str(exc))


@router.post("/api/repair/plans/{plan_id}/reject")
async def reject_plan(plan_id: str) -> dict:
    try:
        return await plans.reject(plan_id)
    except plans.PlanError as exc:
        raise HTTPException(exc.status_code, str(exc))


@router.post("/api/repair/plans/{plan_id}/execute")
async def execute_plan(plan_id: str, body: ExecuteIn) -> dict:
    """执行只认 plan_id + approval_id;动作、目标、参数全部从计划里读,请求体塞不进来。"""
    try:
        return await plans.execute(plan_id, body.approval_id)
    except plans.PlanError as exc:
        raise HTTPException(exc.status_code, str(exc))


@router.post("/api/repair/plans/{plan_id}/handoff")
async def handoff_plan(plan_id: str) -> dict:
    """转人工:把计划、执行、验证拼成工单。验证失败后不允许继续自动试错。"""
    try:
        return await plans.handoff(plan_id)
    except plans.PlanError as exc:
        raise HTTPException(exc.status_code, str(exc))


@router.get("/api/targets/{target_id}/repair/events")
async def repair_events(target_id: int, limit: int = 20) -> dict:
    rows = await repository.list_repair_audit(target_id, max(1, min(limit, 100)))
    return {"events": [{"id": r.id, "event_type": r.event_type, "actor": r.actor,
                        "connector_id": r.connector_id, "plan_id": r.plan_id,
                        "execution_id": r.execution_id, "payload": r.payload or {},
                        "created_at": r.created_at.isoformat() if r.created_at else None}
                       for r in rows]}


@router.get("/api/repair/executions/{execution_id}")
async def get_execution(execution_id: str) -> dict:
    row = await repository.get_repair_execution(execution_id)
    if row is None:
        raise HTTPException(404, "执行记录不存在")
    return _execution_view(row)


class RollbackIn(BaseModel):
    approved_by: str = Field(default="admin", max_length=64)


@router.post("/api/repair/runs/{repair_id}/rollback")
async def rollback_repair_run(repair_id: str, body: RollbackIn) -> dict:
    """回滚一次修复:set_flag 类动作把开关切回修复前的旧值。

    回滚是**确定性逆操作**,不走 LLM——怎么回滚在修复执行时就确定了(审计里的旧值),
    人点回滚按钮 = 授权,执行仍走同一套计划/审批/验证状态机,留痕完整。
    容器类动作没有逆操作(重启无法撤销),这类修复不提供自动回滚,转人工处置。"""
    from app.repair import agent as repair_agent

    run = repair_agent.get(repair_id) or await repository.get_repair_run(repair_id)
    if run is None:
        raise HTTPException(404, "修复任务不存在")
    res = run.get("result") or {}
    rb = res.get("rollback")
    if not rb:
        raise HTTPException(409, "这次修复没有可自动回滚的动作"
                                 "(只有切开关支持;容器类动作的回滚是人工处置)")
    plan_id = res.get("plan_id")
    plan = await repository.get_repair_plan(plan_id) if plan_id else None
    if plan is None:
        raise HTTPException(409, "原修复计划已不可查,无法生成回滚计划")
    if not gateway.is_online(plan.connector_id):
        raise HTTPException(409, "连接器不在线,无法执行回滚")

    try:
        rb_plan = await plans.create(
            plan.target_id, plan.connector_id, rb["action"], run.get("service") or "",
            reason=f"[回滚 {repair_id}] 把 {rb['params'].get('flag')} 切回修复前旧值 "
                   f"{rb['params'].get('variant')}",
            diagnosis_id=run.get("diagnosis_id"),
            conversation_id=run.get("conversation_id"),
            created_by=f"rollback:{body.approved_by}", params=rb["params"])
        ap = await plans.approve(rb_plan["id"],
                                 approved_by=f"rollback:{body.approved_by}")
        execution = await plans.execute(rb_plan["id"], ap["approval_id"])
    except plans.PlanError as exc:
        raise HTTPException(exc.status_code, f"回滚未执行:{exc}")
    await repository.add_repair_audit(
        "repair_rolled_back", target_id=plan.target_id,
        connector_id=plan.connector_id, plan_id=rb_plan["id"],
        execution_id=execution.get("execution_id"),
        actor=f"rollback:{body.approved_by}",
        payload={"source_repair_id": repair_id, "rollback": rb})
    return {"ok": True, "rollback_plan_id": rb_plan["id"],
            "execution_id": execution.get("execution_id"),
            "status": execution.get("status"),
            "source_repair_id": repair_id}


# ---------- 修复 agent 任务(卡片轮询与回放) ----------

@router.get("/api/repair/runs/{repair_id}")
async def get_repair_agent_run(repair_id: str) -> dict:
    """修复任务轮询:内存优先(进行中的任务只有内存有),没有再查存档——
    同 GET /api/diagnosis/{id} 的兜底次序,进程重启后修复卡靠存档回放。"""
    from app.repair import agent as repair_agent
    run = repair_agent.get(repair_id)
    if run is not None:
        return run
    archived = await repository.get_repair_run(repair_id)
    if archived is not None:
        return archived
    return {"error": "修复任务不存在(未发起过,或发起于更早且未落库的旧任务)",
            "code": "not_found"}


@router.get("/api/repair/runs/by-diagnosis/{diag_id}")
async def latest_repair_for_diagnosis(diag_id: str) -> dict:
    """某诊断单最近一次修复任务(聊天卡片回放:刷新页面后修复卡靠它恢复)。"""
    latest = await repository.latest_repair_run_for_diagnosis(diag_id)
    return {"repair": latest}


@router.post("/api/repair/connectors/{connector_id}/revoke")
async def revoke_connector(connector_id: str) -> dict:
    row = await repository.get_repair_connector(connector_id)
    if not await repository.revoke_repair_connector(connector_id):
        raise HTTPException(404, "连接器不存在")
    gateway.unregister(connector_id)
    await repository.add_repair_audit(
        "connector_revoked", target_id=row.target_id if row else None,
        connector_id=connector_id)
    logger.info("修复连接器已撤销 id=%s", connector_id)
    return {"ok": True, "connector_id": connector_id, "status": "revoked"}


# ---------- 连接器侧(主动出站) ----------

@router.post("/api/repair/connect")
async def connect(body: ConnectIn) -> dict:
    """用一次性令牌换连接器身份。令牌用掉即废,重复使用直接拒绝。"""
    try:
        target_id, token_id = await enrollment.verify(body.token)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if target_id != body.platform_id:
        raise HTTPException(400, "令牌与平台不匹配")
    target = await repository.get_target(target_id)
    if target is None:
        raise HTTPException(404, "接入平台不存在")

    connector_id = "rc_" + secrets.token_hex(8)
    secret = secrets.token_hex(24)
    name = (body.name or "").strip() or (body.host_info or {}).get("hostname") or connector_id
    await repository.create_repair_connector(
        connector_id, target_id, name[:128],
        enrollment.connector_secret_hash(secret), body.capabilities, body.host_info)
    await repository.mark_enrollment_used(token_id)
    gateway.register(connector_id)
    await repository.add_repair_audit(
        "connector_connected", target_id=target_id, connector_id=connector_id,
        payload={"name": name, "capabilities": len(body.capabilities)})
    logger.info("修复连接器上线 id=%s target=%s name=%s", connector_id, target_id, name)
    return {"connector_id": connector_id, "secret": secret,
            "heartbeat_interval": 15, "poll_timeout": 25,
            "capabilities": body.capabilities}


@router.post("/api/repair/heartbeat")
async def heartbeat(body: HeartbeatIn) -> dict:
    row = await repository.get_repair_connector(body.connector_id)
    if row is None or not enrollment.verify_connector_secret(row, body.secret):
        raise HTTPException(401, "连接器身份校验失败")
    if row.status == "revoked":
        return {"ok": False, "revoked": True}
    await repository.touch_repair_connector(body.connector_id, body.capabilities,
                                            body.host_info)
    # 服务重启后内存会话会丢:心跳到了就把会话补回来,连接器不用重新注册
    if not gateway.has_session(body.connector_id):
        gateway.register(body.connector_id)
    return {"ok": True, "revoked": False}


@router.post("/api/repair/poll")
async def poll(body: PollIn) -> dict:
    """长轮询取任务。没有任务就挂住到超时,返回 command=null。

    用 POST 而不是 GET:密钥若放查询串会进 uvicorn 访问日志,等于把身份写进日志文件。
    请求体不进访问日志,这是唯一原因,不是 REST 洁癖。"""
    row = await repository.get_repair_connector(body.connector_id)
    if row is None or not enrollment.verify_connector_secret(row, body.secret):
        raise HTTPException(401, "连接器身份校验失败")
    if row.status == "revoked":
        raise HTTPException(403, "连接器已被撤销")
    if not gateway.has_session(body.connector_id):
        gateway.register(body.connector_id)
    command = await gateway.wait_command(body.connector_id, timeout=body.timeout)
    return {"command": command}


@router.post("/api/repair/result")
async def result(body: ResultIn) -> dict:
    await _connector_or_401(body.connector_id, body.secret)
    payload = {"ok": body.ok, "status": body.status,
               "message": body.message, "data": body.data}
    matched = gateway.resolve(body.request_id, payload)
    if not matched:
        logger.info("连接器回报了过期请求 connector=%s req=%s", body.connector_id,
                    body.request_id)
    return {"ok": True, "matched": matched}


# ---------- 安装脚本与连接器本体(给一行命令用) ----------

@router.get("/repair-connector/install.sh", include_in_schema=False)
async def install_script() -> FileResponse:
    return FileResponse(_CONNECTOR_DIR / "install.sh", media_type="text/x-shellscript",
                        filename="install.sh")


@router.get("/repair-connector/connector.py", include_in_schema=False)
async def connector_script() -> FileResponse:
    return FileResponse(_CONNECTOR_DIR / "connector.py", media_type="text/x-python",
                        filename="connector.py")


@router.get("/repair-connector/manifest.json", include_in_schema=False)
async def connector_manifest() -> dict:
    """下载校验用:安装脚本比对 sha256,挡住传输截断/被中间层改写。
    注意它挡不住"服务端本身被攻破"(两个文件都从同一个服务端拿),那需要发布包签名。"""
    import hashlib
    data = (_CONNECTOR_DIR / "connector.py").read_bytes()
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', data.decode("utf-8", "ignore"))
    return {"version": m.group(1) if m else "unknown",
            "sha256": hashlib.sha256(data).hexdigest()}
