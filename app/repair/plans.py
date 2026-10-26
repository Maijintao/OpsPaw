"""修复计划 → 审批 → 执行 → 验证 的状态机。

一条铁律:执行只认 `plan_id + approval_id`,不接受动作、目标或任何参数。计划内容改一个字,
hash 就变,旧审批立刻失效。这样"批的是哪一份"永远说得清。

失败语义:
- 预检查不过 → 不执行,计划标 preflight_failed;
- 执行超时/连接器没回话 → unknown,不自动重试(写操作超时未必没执行);
- 执行成功但验证不过 → verification_failed,停在这里,不继续试;
- 冷却期内重复动作 → 直接拒,防重启循环。
"""
import asyncio
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta

from app.db import repository
from app.repair import catalog, gateway, policy

logger = logging.getLogger("app.repair")

PLAN_TTL_MIN = 15
APPROVAL_TTL_MIN = 10
VERIFY_DELAY_S = 3.0
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# flagd 开关/变体与主机名的值规范(连接器同一套,双端白名单)
FLAG_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
VARIANT_RE = re.compile(r"^[A-Za-z0-9_%.-]{1,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
# 后台全局审批模式存 app_settings 这把 key:manual(默认,要人点头) / auto(免审批直接执行)
APPROVAL_MODE_KEY = "repair_approval_mode"

# 同一连接器同时只允许一个写动作在跑
_LOCKS: dict[str, asyncio.Lock] = {}
# 自动执行的背景任务强引用:没人 await 也必须跑完,不能被 GC 回收
_AUTO_TASKS: set[asyncio.Task] = set()


class PlanError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _normalize_params(action: str, service: str, params: dict | None = None) -> dict:
    """按动作目录的参数定义逐键收口,客户端多余字段不带进计划或连接器。
    service/flag/variant/host/port 各有值规范,双端同一套白名单。"""
    item = catalog.definition(action)
    if item is None or (not catalog.is_write(action)):
        raise PlanError("不支持的写动作", 400)
    params = params or {}
    if set(params) - set(item["params"]):
        raise PlanError("计划包含动作目录之外的参数", 400)
    out: dict = {}
    for key in item["params"]:
        val = params.get(key)
        if key == "service":
            if not SERVICE_RE.fullmatch(service or ""):
                raise PlanError("服务名不合法(只允许字母数字和 _.-,最长 64)", 400)
            out[key] = service
        elif key == "flag":
            if not FLAG_RE.fullmatch(str(val or "")):
                raise PlanError("开关名不合法(只允许字母数字下划线,最长 64)", 400)
            out[key] = str(val)
        elif key == "variant":
            if not VARIANT_RE.fullmatch(str(val or "")):
                raise PlanError("变体名不合法(只允许字母数字与 _%.-,最长 64)", 400)
            out[key] = str(val)
        elif key == "target_host":
            if not HOST_RE.fullmatch(str(val or "")):
                raise PlanError("目标主机名不合法", 400)
            out[key] = str(val)
        elif key == "target_port":
            try:
                port = int(val)
            except (TypeError, ValueError):
                raise PlanError("目标端口必须是整数", 400)
            if not 1 <= port <= 65535:
                raise PlanError("目标端口范围是 1-65535", 400)
            out[key] = port
        else:
            raise PlanError("当前动作参数定义不完整", 400)
    return out


def _hash(target_id: int, connector_id: str, action: str, service: str,
          params: dict) -> str:
    raw = json.dumps({"target_id": target_id, "connector_id": connector_id,
                      "action": action, "service": service, "params": params},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def view(plan, execution=None) -> dict:
    return {
        "id": plan.id, "target_id": plan.target_id, "connector_id": plan.connector_id,
        "conversation_id": plan.conversation_id,
        "diagnosis_id": plan.diagnosis_id, "action": plan.action,
        "label": catalog.label(plan.action), "service": plan.service,
        "params": plan.params or {}, "reason": plan.reason,
        "risk": catalog.risk(plan.action),
        "plan_hash": plan.plan_hash, "status": plan.status,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
        "execution": execution,
    }


async def resolve_auto_action(connector_id: str, service: str) -> tuple[str, str]:
    """自动修复用什么动作由系统按容器现状决定,不让用户在下拉框里挑:
    在跑的故障服务 → 重启;停了的 → 启动。inspect 拿不到就如实报错,不瞎猜。
    返回 (动作, 决策理由)——理由进计划的 reason,审批卡和结果卡都要展示。"""
    try:
        insp = await gateway.call(connector_id, "inspect_service",
                                  {"service": service}, timeout=15)
    except gateway.ConnectorOffline as exc:
        raise PlanError(f"决定修复动作时连接器失联:{exc}", 409)
    if not insp.get("ok"):
        raise PlanError(f"决定修复动作失败:{insp.get('message') or '连接器未返回结果'}", 409)
    data = insp.get("data") or {}
    if not data.get("found"):
        raise PlanError(f"服务 {service} 在这台机器上不存在,无法自动修复", 409)
    if not data.get("running"):
        return ("start_service",
                "连接器实查容器当前未运行,选择启动服务(start_service)")
    return ("restart_service",
            "连接器实查容器在运行但服务异常,选择重启服务(restart_service)")


async def create(target_id: int, connector_id: str, action: str, service: str,
                 reason: str = "", diagnosis_id: str | None = None,
                 created_by: str = "admin", conversation_id: int | None = None,
                 params: dict | None = None) -> dict:
    if not catalog.is_write(action):
        raise PlanError("只读动作不能生成修复计划,请使用诊断查询", 400)
    # 容器类动作才要求服务名;flag 类动作没有 service,值为空合法(参数校验在 _normalize_params)
    if "service" in catalog.ACTIONS[action]["params"] \
            and not SERVICE_RE.fullmatch(service or ""):
        raise PlanError("服务名不合法(只允许字母数字和 _.-,最长 64)", 400)
    conn = await repository.get_repair_connector(connector_id)
    if conn is None or conn.target_id != target_id or conn.status == "revoked":
        raise PlanError("连接器不属于这个平台或已撤销", 404)
    if not gateway.is_online(connector_id):
        raise PlanError("连接器不在线,不能生成计划", 409)
    # 有效能力 = 服务端目录 ∩ 连接器声明:连接器没声明的动作,服务端也不发
    declared = {c.get("action") for c in (conn.capabilities or []) if isinstance(c, dict)}
    if action not in declared:
        raise PlanError(f"连接器没有声明支持「{catalog.label(action)}」", 409)

    params = _normalize_params(action, service, params)
    now = datetime.now()
    plan_id = "pl_" + secrets.token_hex(8)
    await repository.create_repair_plan({
        "id": plan_id, "target_id": target_id, "connector_id": connector_id,
        "conversation_id": conversation_id,
        "diagnosis_id": diagnosis_id, "action": action, "service": service,
        "params": params, "reason": (reason or "")[:500],
        "risk": catalog.risk(action),
        "plan_hash": _hash(target_id, connector_id, action, service, params),
        "status": "awaiting_confirmation", "created_by": created_by,
        "created_at": now,
        "expires_at": now + timedelta(minutes=PLAN_TTL_MIN),
    })
    await repository.add_repair_audit(
        "repair_plan_created", target_id=target_id, connector_id=connector_id,
        plan_id=plan_id, actor=created_by,
        payload={"action": action, "service": service, "risk": catalog.risk(action),
                 "diagnosis_id": diagnosis_id})
    logger.info("修复计划已创建 plan=%s action=%s service=%s connector=%s",
                plan_id, action, service, connector_id)
    plan = await repository.get_repair_plan(plan_id)
    return view(plan)


async def auto_approve_enabled() -> bool:
    """后台开关:修复动作免审批自动执行。安全边界是动作白名单(AUTO_ACTIONS),
    这个开关只决定"要不要人点头"。配置坏了当 manual,不拦流程。"""
    try:
        stored = (await repository.get_settings_map()).get(APPROVAL_MODE_KEY)
        return stored == "auto"
    except Exception:  # noqa: BLE001
        return False


async def create_auto(target_id: int, connector_id: str, action: str, service: str,
                      reason: str = "", diagnosis_id: str | None = None,
                      created_by: str = "agent", conversation_id: int | None = None,
                      *, verdict: str | None = None, fault_type: str | None = None,
                      confidence_tier: str | None = None) -> dict:
    """生成计划;满足自动执行条件就直接执行,不满足就留给用户确认。

    自动执行不是“保证成功”,而是:动作只限已批准的自动白名单、故障形态匹配、证据档位够、症状还在。
    新增重建动作和只读动作不会进入自动车道。任何一条不满足,原样返回待确认计划并说明原因。"""
    plan = await create(target_id, connector_id, action, service, reason,
                        diagnosis_id=diagnosis_id, created_by=created_by,
                        conversation_id=conversation_id)

    from app.repair import symptom
    sym = await symptom.check(target_id, service)
    if action in ("set_flag", "restore_flags"):
        # flag 类动作的故障在开关不在指标,指标画像对它没有发言权,复检记 skipped
        sym = {"status": "skipped", "reason": "flag 类动作不做指标复检"}
    ok, reasons = policy.evaluate(action, verdict, fault_type, confidence_tier,
                                  sym.get("status") or "unknown")
    if not ok and action in policy.AUTO_ACTIONS and await auto_approve_enabled():
        # 后台开了「自动通过」:动作仍限定在白名单内,免人工审批直接执行。
        # 安全边界是"动作只能重启/启动",不是"必须有人点确认"。
        ap = await approve(plan["id"], approved_by="policy:auto-approve")
        task = asyncio.create_task(_auto_run(plan["id"], ap["approval_id"]))
        _AUTO_TASKS.add(task)
        task.add_done_callback(_AUTO_TASKS.discard)
        logger.info("修复审批模式为自动通过,免审批执行 plan=%s action=%s service=%s",
                    plan["id"], action, service)
        return {**plan, "status": "approved", "auto_started": True,
                "auto_reason": "后台开启了修复自动通过(动作限定在重启/启动白名单内)",
                "approval_id": ap["approval_id"], "symptom": sym}
    if not ok:
        return {**plan, "auto_started": False, "auto_blocked": reasons, "symptom": sym}

    ap = await approve(plan["id"], approved_by="policy:auto")
    task = asyncio.create_task(_auto_run(plan["id"], ap["approval_id"]))
    _AUTO_TASKS.add(task)
    task.add_done_callback(_AUTO_TASKS.discard)
    logger.info("自动修复已启动 plan=%s action=%s service=%s", plan["id"], action, service)
    return {**plan, "status": "approved", "auto_started": True,
            "auto_reason": "满足自动执行条件(动作/形态/证据/症状四项)", "symptom": sym,
            "approval_id": ap["approval_id"]}


async def _auto_run(plan_id: str, approval_id: str) -> None:
    try:
        await execute(plan_id, approval_id)
    except Exception:  # noqa: BLE001  自动执行失败落计划状态,不弄死后台任务
        logger.exception("自动修复执行失败 plan=%s", plan_id)
        try:
            await repository.set_repair_plan_status(plan_id, "execution_failed")
        except Exception:  # noqa: BLE001
            pass


async def approve(plan_id: str, approved_by: str = "admin") -> dict:
    plan = await repository.get_repair_plan(plan_id)
    if plan is None:
        raise PlanError("计划不存在", 404)
    if plan.status != "awaiting_confirmation":
        raise PlanError(f"计划当前状态是 {plan.status},不能再确认", 409)
    if plan.expires_at < datetime.now():
        await repository.set_repair_plan_status(plan_id, "expired")
        raise PlanError("计划已过期,请重新生成", 409)
    if _hash(plan.target_id, plan.connector_id, plan.action, plan.service,
             plan.params or {}) != plan.plan_hash:
        raise PlanError("计划内容与创建时不一致,审批作废,请重新生成", 409)

    approval_id = "ap_" + secrets.token_hex(8)
    expires = datetime.now() + timedelta(minutes=APPROVAL_TTL_MIN)
    await repository.create_repair_approval({
        "id": approval_id, "plan_id": plan.id, "plan_hash": plan.plan_hash,
        "approved_by": approved_by, "approved_at": datetime.now(),
        "expires_at": expires, "status": "active"})
    await repository.set_repair_plan_status(plan_id, "approved")
    await repository.add_repair_audit(
        "repair_approval_created", target_id=plan.target_id,
        connector_id=plan.connector_id, plan_id=plan.id, actor=approved_by,
        payload={"action": plan.action, "service": plan.service,
                 "approval_id": approval_id})
    logger.info("修复计划已批准 plan=%s approval=%s by=%s", plan_id, approval_id, approved_by)
    return {"plan_id": plan.id, "approval_id": approval_id,
            "expires_at": expires.isoformat(timespec="seconds")}


async def reject(plan_id: str) -> dict:
    plan = await repository.get_repair_plan(plan_id)
    if plan is None:
        raise PlanError("计划不存在", 404)
    if plan.status not in ("awaiting_confirmation", "approved"):
        raise PlanError(f"计划当前状态是 {plan.status},不能取消", 409)
    await repository.set_repair_plan_status(plan_id, "rejected")
    return {"plan_id": plan.id, "status": "rejected"}


def _preflight_check(action: str, data: dict) -> None:
    running = bool(data.get("running"))
    if action in ("restart_service", "stop_service", "recreate_service") and not running:
        raise PlanError("目标当前没在运行,不能执行这个动作", 409)
    if action == "start_service" and running:
        raise PlanError("目标已经在运行,不需要启动", 409)
    # flag 类 / 清日志动作不围绕容器运行状态,预检查在 execute 里各自做


async def _verify(plan, connector_id: str, before: dict) -> dict:
    """执行后的独立验证:不信连接器自报成功,隔几秒自己再看一眼状态。
    验证方式由动作目录的 verify 字段决定:
    - running/stopped/running_after:容器状态(原有语义);
    - flag_value:set_flag 后重读开关,确认值真的变了(flagd 热加载,读文件即真实值);
    - flags_off:restore 后确认全部开关已回 off;
    - ok:动作自报成功即过(截断日志没有比这更便宜的独立判据)。"""
    await asyncio.sleep(VERIFY_DELAY_S)
    mode = catalog.ACTIONS[plan.action]["verify"]
    before_data = before.get("data") or {}

    if mode == "ok":
        return {"status": "verified", "mode": mode,
                "checked_at": datetime.now().isoformat(timespec="seconds")}

    if mode == "flag_value":
        after = await gateway.call(connector_id, "get_flag",
                                   {"flag": (plan.params or {}).get("flag")}, timeout=15)
        if not after.get("ok"):
            return {"status": "unknown", "mode": mode,
                    "reason": after.get("message") or "复检开关失败"}
        ad = after.get("data") or {}
        ok = ad.get("found") and ad.get("current") == (plan.params or {}).get("variant")
        return {"status": "verified" if ok else "failed", "mode": mode,
                "reason": None if ok else
                f"开关当前是 {ad.get('current')},不是目标值 {(plan.params or {}).get('variant')}",
                "after": {"flag": ad.get("flag"), "current": ad.get("current")},
                "checked_at": datetime.now().isoformat(timespec="seconds")}

    if mode == "flags_off":
        after = await gateway.call(connector_id, "list_flags", {}, timeout=15)
        if not after.get("ok"):
            return {"status": "unknown", "mode": mode,
                    "reason": after.get("message") or "复检开关失败"}
        ad = after.get("data") or {}
        dirty = ad.get("non_off") or []
        ok = not dirty
        return {"status": "verified" if ok else "failed", "mode": mode,
                "reason": None if ok else "仍有非 off 开关:" + "、".join(map(str, dirty)),
                "after": {"non_off": dirty},
                "checked_at": datetime.now().isoformat(timespec="seconds")}

    try:
        after = await gateway.call(connector_id, "inspect_service",
                                   {"service": plan.service}, timeout=15)
    except gateway.ConnectorOffline as exc:
        return {"status": "unknown", "reason": str(exc)}
    if not after.get("ok"):
        return {"status": "unknown", "reason": after.get("message") or "复检失败"}
    data = after.get("data") or {}
    mode = catalog.ACTIONS[plan.action]["verify"]
    before_data = before.get("data") or {}

    if mode == "running":
        ok: bool | None = data.get("running") is True
    elif mode == "stopped":
        ok = data.get("running") is False
    else:  # running_after:必须还在跑,而且启动时间/重启次数确实变了
        if data.get("running") is not True:
            ok = False
        elif before_data.get("started_at") and data.get("started_at"):
            ok = data["started_at"] != before_data["started_at"]
        elif before_data.get("restart_count") is not None \
                and data.get("restart_count") is not None:
            ok = data["restart_count"] > before_data["restart_count"]
        else:
            ok = None

    # 容器自带健康检查时以它为准:unhealthy 说明起来了但业务没起来
    health = data.get("health")
    if ok and health == "unhealthy":
        return {"status": "failed", "mode": mode,
                "reason": "容器在跑,但健康检查是 unhealthy(服务没有真正恢复)",
                "before": before_data, "after": data}

    if ok is None:
        return {"status": "unknown", "mode": mode,
                "reason": "连接器没有返回可用于判断的启动时间或重启次数",
                "before": before_data, "after": data}
    return {"status": "verified" if ok else "failed", "mode": mode,
            "before": {k: before_data.get(k) for k in
                       ("running", "started_at", "restart_count", "state", "health")},
            "after": {k: data.get(k) for k in
                      ("running", "started_at", "restart_count", "state", "health")},
            "checked_at": datetime.now().isoformat(timespec="seconds")}


async def execute(plan_id: str, approval_id: str) -> dict:
    plan = await repository.get_repair_plan(plan_id)
    if plan is None:
        raise PlanError("计划不存在", 404)
    if plan.status != "approved":
        raise PlanError(f"计划当前状态是 {plan.status},不能执行", 409)
    approval = await repository.get_repair_approval(approval_id)
    if approval is None or approval.plan_id != plan_id:
        raise PlanError("审批不存在或不属于这个计划", 403)
    if approval.status != "active":
        raise PlanError("审批已使用或已失效,请重新确认", 403)
    if approval.plan_hash != plan.plan_hash:
        raise PlanError("审批与计划内容不一致,请重新确认", 403)
    if approval.expires_at < datetime.now():
        await repository.set_repair_approval_status(approval_id, "expired")
        raise PlanError("审批已过期,请重新确认", 403)
    if plan.expires_at < datetime.now():
        await repository.set_repair_plan_status(plan_id, "expired")
        raise PlanError("计划已过期,请重新生成", 409)
    if not gateway.is_online(plan.connector_id):
        raise PlanError("连接器不在线,不能执行", 409)

    # 修复前症状复检:问题已经自己好了就别动。用同一套指标判据,只把回看缩到 2 小时。
    # flag 类动作跳过:故障在开关不在指标,指标画像对它没有发言权(反而可能误判 resolved)。
    from app.repair import symptom
    if plan.action in ("set_flag", "restore_flags"):
        symptom_info = {"status": "skipped", "reason": "flag 类动作不做指标复检"}
    else:
        symptom_info = await symptom.check(plan.target_id, plan.service)
    if symptom_info.get("status") == "resolved":
        await repository.set_repair_plan_status(plan_id, "symptom_resolved")
        await repository.add_repair_audit(
            "repair_symptom_resolved", target_id=plan.target_id,
            connector_id=plan.connector_id, plan_id=plan.id,
            payload={"service": plan.service, "reason": symptom_info.get("reason")})
        logger.info("修复前症状已消失,不执行 plan=%s service=%s", plan.id, plan.service)
        return {"plan_id": plan.id, "execution_id": None,
                "status": "symptom_resolved", "symptom": symptom_info,
                "result": {"message": symptom_info.get("reason")},
                "verification": None}

    # 冷却:同一平台同一服务同一动作,短时间不许重复(防重启循环)
    last = await repository.latest_repair_execution(plan.target_id, plan.service, plan.action)
    if last is not None and last.started_at is not None:
        gap = (datetime.now() - last.started_at).total_seconds()
        cd = catalog.cooldown(plan.action)
        if gap < cd:
            raise PlanError(f"冷却中:距上次{catalog.label(plan.action)}只有 {int(gap)} 秒,"
                            f"至少要等 {cd} 秒", 429)

    lock = _LOCKS.setdefault(plan.connector_id, asyncio.Lock())
    if lock.locked():
        raise PlanError("这台连接器上还有修复动作在执行,请稍后再试", 409)

    async with lock:
        # 锁内重读:两个人同时点执行,只有一个能进
        plan = await repository.get_repair_plan(plan_id)
        if plan is None or plan.status != "approved":
            raise PlanError("计划状态已变化,不能执行", 409)
        approval = await repository.get_repair_approval(approval_id)
        if approval is None or approval.status != "active" or approval.plan_id != plan_id:
            raise PlanError("审批状态已变化,不能执行", 403)
        if approval.expires_at < datetime.now():
            await repository.set_repair_approval_status(approval_id, "expired")
            raise PlanError("审批已过期,请重新确认", 403)
        conn = await repository.get_repair_connector(plan.connector_id)
        declared = {c.get("action") for c in ((conn.capabilities if conn else None) or [])
                    if isinstance(c, dict)}
        if plan.action not in declared:
            await repository.set_repair_plan_status(plan_id, "preflight_failed")
            raise PlanError("连接器当前没有声明支持这个动作,已停止执行", 409)

        await repository.set_repair_plan_status(plan_id, "preflight")
        try:
            if plan.action == "set_flag":
                # flag 类预检查:开关存在、目标变体合法——不对容器状态说三道四
                before = await gateway.call(plan.connector_id, "get_flag",
                                            {"flag": (plan.params or {}).get("flag")},
                                            timeout=15)
                if not before.get("ok"):
                    raise PlanError(before.get("message") or "开关查询失败", 409)
                bd = before.get("data") or {}
                if not bd.get("found"):
                    raise PlanError(f"开关 {(plan.params or {}).get('flag')} 不存在", 409)
                if (plan.params or {}).get("variant") not in (bd.get("variants") or []):
                    raise PlanError("目标变体不存在", 409)
            elif plan.action == "restore_flags":
                before = await gateway.call(plan.connector_id, "list_flags", {}, timeout=15)
                if not before.get("ok"):
                    raise PlanError(before.get("message") or "开关清单读取失败", 409)
            else:
                before = await gateway.call(plan.connector_id, "inspect_service",
                                            {"service": plan.service}, timeout=15)
                if not before.get("ok"):
                    raise PlanError(f"预检查失败:{before.get('message') or '连接器未返回结果'}", 409)
                if not (before.get("data") or {}).get("found"):
                    raise PlanError("目标服务在这台机器上不存在", 409)
                _preflight_check(plan.action, before.get("data") or {})
        except (PlanError, gateway.ConnectorOffline) as exc:
            await repository.set_repair_plan_status(plan_id, "preflight_failed")
            raise PlanError(f"预检查未通过:{exc}", 409)

        execution_id = "ex_" + secrets.token_hex(8)
        await repository.create_repair_execution({
            "id": execution_id, "plan_id": plan.id, "approval_id": approval_id,
            "connector_id": plan.connector_id, "status": "running",
            "started_at": datetime.now()})
        await repository.set_repair_approval_status(approval_id, "used")
        await repository.set_repair_plan_status(plan_id, "executing")
        await repository.add_repair_audit(
            "repair_execution_started", target_id=plan.target_id,
            connector_id=plan.connector_id, plan_id=plan.id,
            execution_id=execution_id,
            payload={"action": plan.action, "service": plan.service,
                     "symptom": symptom_info.get("status")})

        try:
            result = await gateway.call(
                plan.connector_id, plan.action, plan.params or {},
                timeout=120 if plan.action == "recreate_service" else 60,
                extra={"plan_id": plan.id, "approval_id": approval_id})
        except gateway.ConnectorOffline as exc:
            await repository.finish_repair_execution(
                execution_id, "unknown", None, None, str(exc))
            await repository.set_repair_plan_status(plan_id, "execution_unknown")
            await repository.add_repair_audit(
                "repair_execution_finished", target_id=plan.target_id,
                connector_id=plan.connector_id, plan_id=plan.id,
                execution_id=execution_id, payload={"status": "execution_unknown",
                                                    "error": str(exc)})
            raise PlanError(f"执行中断:{exc}", 409)

        if result.get("status") != "success":
            status = "unknown" if result.get("status") == "unknown" else "failed"
            await repository.finish_repair_execution(
                execution_id, status, result, None, result.get("message"))
            await repository.set_repair_plan_status(
                plan_id, "execution_unknown" if status == "unknown" else "execution_failed")
            await repository.add_repair_audit(
                "repair_execution_finished", target_id=plan.target_id,
                connector_id=plan.connector_id, plan_id=plan.id,
                execution_id=execution_id,
                payload={"status": status, "message": result.get("message")})
            return {"plan_id": plan.id, "execution_id": execution_id,
                    "status": "execution_unknown" if status == "unknown" else "execution_failed",
                    "result": result, "verification": None, "symptom": symptom_info}

        await repository.set_repair_plan_status(plan_id, "verifying")
        verification = await _verify(plan, plan.connector_id, before)
        # 修复后观测:验证只回答"容器/开关起来了没有",这里再看一眼指标,回答"业务真的活回来没有"。
        # 刚重启指标可能还没回流(unknown 如实说),如果还有异常维也要如实摆出来——
        # 重启成功 ≠ 故障治好,这条证据链用户看得见。
        #
        # flag 类动作**不再跳过**:开关切完指标是会回落的,跳过等于闭着眼睛收工
        # (2026-10-22 实测:productCatalogFailure 切回 off 后没做任何复检,回执里只能
        # 写"切了,但不知道好没好",还把一个两小时前就存在的老报错当成修复引入的新问题)。
        # 口径要说清楚:这是"指标侧有没有残留",不是"开关切对了没有"——后者由
        # _verify 的 flag_value 负责,两者互补。
        try:
            post = await symptom.check(plan.target_id, plan.service)
            if plan.action in ("set_flag", "restore_flags"):
                post["note"] = ("flag 类动作的指标侧复检:看业务有没有真的活回来,"
                                "不代表开关切得对(开关值由 flag_value 验证)")
            verification["post_symptom"] = post
        except Exception as exc:  # noqa: BLE001  修复后观测失败不改变验证结论,如实记录
            verification["post_symptom"] = {"status": "unknown",
                                            "reason": f"修复后观测失败({type(exc).__name__})"}
        final = "succeeded" if verification.get("status") == "verified" \
            else "verification_failed" if verification.get("status") == "failed" \
            else "verification_unknown"
        await repository.finish_repair_execution(
            execution_id, final, result, verification, None)
        await repository.set_repair_plan_status(plan_id, final)
        await repository.add_repair_audit(
            "repair_execution_finished", target_id=plan.target_id,
            connector_id=plan.connector_id, plan_id=plan.id,
            execution_id=execution_id,
            payload={"status": final, "message": result.get("message")})
        await repository.add_repair_audit(
            "repair_verification_finished", target_id=plan.target_id,
            connector_id=plan.connector_id, plan_id=plan.id,
            execution_id=execution_id,
            payload={"verification": verification.get("status"),
                     "mode": verification.get("mode")})
        logger.info("修复执行完成 plan=%s execution=%s 最终=%s 验证=%s",
                    plan.id, execution_id, final, verification.get("status"))
        return {"plan_id": plan.id, "execution_id": execution_id, "status": final,
                "result": result, "verification": verification, "symptom": symptom_info}


async def handoff(plan_id: str, by: str = "admin") -> dict:
    """转人工:把诊断结论和修复执行证据拼成一份可独立阅读的问题报告建单。
    字段对齐验收口径:原始反馈/根因结论/证据/置信与缺口/修复执行/验证证据/后续建议。
    只有关联了会话的计划能建单。"""
    plan = await repository.get_repair_plan(plan_id)
    if plan is None:
        raise PlanError("计划不存在", 404)
    if plan.status not in ("preflight_failed", "verification_failed", "verification_unknown",
                           "execution_failed", "execution_unknown", "human_review"):
        raise PlanError(f"计划当前状态是 {plan.status},不需要转人工", 409)
    if plan.conversation_id is None:
        raise PlanError("这个计划没有关联会话,不能自动建单;请到工单系统手工建单", 409)

    execution = await repository.latest_execution_for_plan(plan_id)
    verification = (execution.verification if execution else None) or {}

    # 诊断侧全字段:转人工的读者没有卡片上下文,报告必须自含(验收 3.6 口径)
    diag = await repository.get_diagnosis_run(plan.diagnosis_id) \
        if plan.diagnosis_id else None
    res = (diag or {}).get("result") or {}
    nar = res.get("narrative") or {}
    conf = res.get("confidence") or {}
    ft = res.get("fault_type") or {}
    symptom = (diag or {}).get("symptom") or "未记录"
    diag_time = (diag or {}).get("created_at") or ""

    lines = [
        f"[修复转人工] 计划 {plan.id} · {catalog.label(plan.action)} · 目标 {plan.service}",
        f"计划状态:{plan.status};原因:{plan.reason or '未填写'}",
        "",
        "—— 原始反馈 ————",
        f"症状:{symptom}" + (f"(诊断发起于 {diag_time})" if diag_time else ""),
        "",
        "—— 诊断结论 ————",
        f"判定:{res.get('verdict') or '未知'},根因服务:{res.get('root_cause_service') or '未定位'}",
        f"故障形态:{ft.get('text') or '未定性'}",
    ]
    if nar:
        lines += [f"发生了什么:{nar.get('description', '')}",
                  f"病根:{nar.get('fault', '')}",
                  f"依据:{nar.get('reason', '')}",
                  f"给修复的交代:{nar.get('other', '')}"]
    if res.get("chain"):
        lines.append("证据链:" + " | ".join(res["chain"]))
    if res.get("missing"):
        lines.append("证据缺口:" + ";".join(res["missing"]))
    lines.append(f"证据充分度:{conf.get('tier', 'unknown')}"
                 + (f"({conf.get('summary')})" if conf.get("summary") else ""))
    lines.append("")
    lines.append("—— 修复执行 ————")
    if execution is not None:
        lines.append(f"执行:{execution.id} / 状态 {execution.status}"
                     + (f" / {(execution.result or {}).get('message')}"
                        if execution.result else ""))
    else:
        lines.append("修复动作未执行(预检查未通过或连接器问题),详情见计划审计")
    if verification:
        lines.append(f"状态机验证:{verification.get('status')}"
                     + (f"({verification.get('reason')})" if verification.get("reason") else ""))
        ps = verification.get("post_symptom") or {}
        if ps:
            lines.append(f"修复后观测:{ps.get('status')}"
                         + (f"——{ps.get('reason')}" if ps.get("reason") else ""))
    lines.append("")
    lines.append("—— 后续建议 ————")
    lines.append("1. 按证据链人工核实根因后处置;2. 处置结果回填本工单;"
                 "3. 若涉及配置/开关变更,恢复后请记录变更来源,避免复发。")

    desc = "\n".join(lines)[:4000]

    ticket_no = await repository.create_ticket(plan.conversation_id, desc, "故障")
    await repository.set_repair_plan_status(plan_id, "human_review")
    await repository.add_repair_audit(
        "repair_human_review_required", target_id=plan.target_id,
        connector_id=plan.connector_id, plan_id=plan.id, actor=by,
        payload={"ticket_no": ticket_no, "previous_status": plan.status})
    return {"plan_id": plan.id, "status": "human_review", "ticket_no": ticket_no}
