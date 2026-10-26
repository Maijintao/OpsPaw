"""事故中心:把"多条告警、多次诊断、一次修复"并成一起事故(第一期,2026-10-24)。

核心是并单判据——确定性、零阈值,不搞文本相似度那种概率把戏:
1. 告警触发时:告警指向的服务落在某起未关闭事故的 scope(症状/根因/诊断子图
   服务集)里 → 并入那起事故(时间线记一行,告警计数+1),不另开新单;
2. 诊断完成时:本事故的根因服务**和故障形态**都与另一起未关闭事故相同(同平台)
   → 后者整体并入前者——同根因同形态才是同一起故障,不管哪条哨兵先响;
3. 吸收有窗(对齐 PagerDuty 公开的分组窗语义:每并一次顺延,24 小时封顶,超窗
   新告警开新单):超窗未关闭的 open 单自动收口成已解决——没有结论也没人管的
   陈年旧单不该一直吸收后来的新故障;diagnosed/mitigating 超窗只停止吸收、
   不强关,已定位待修复的事故不能被定时器藏掉;
4. 宁可漏并不可错并:错并会把一起独立故障(可能是最严重的那起)藏进别人的
   事故卡里,所以每条并入的告警都能在详情页一键拆出成独立事故。

挂点(谁调用这里;全部 try 兜底,事故联动失败绝不弄死触发它的链路):
- 哨兵触发诊断后 → on_alert(开单或并单);
- 诊断终态落库后(runner)→ on_diagnosed(推进状态+同根因并单);
  聊天里人工发起的诊断不自动开单,但撞上未关闭事故的 scope 也会并入;
- 修复回执落库后(repair agent)→ on_repair(mitigating/resolved 推进)。

并发说明:开单竞态(两条首告同时到、各开一单)靠判据②自愈——两单诊断完成后
同根因同形态会被并回;计数用 SQL 原子自增,并发不丢。

状态机:open(诊断中) → diagnosed(已定位) → mitigating(修复中) → resolved(已解决)
→ closed(已关闭);另有一个终态 merged(已并入他单,时间线保留可查)。
resolved/closed 可人工重开回 open。mitigating/diagnosed 只由系统推进,不给按钮。
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel

from app.db import base as db_base
from app.db.models import DiagnosisRun, Incident, IncidentEvent, RepairRun

logger = logging.getLogger("app")

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

# 吸收窗/静默收口的时长:取自业界公开缺省,不是拍值——PagerDuty 全局告警分组的
# 窗口上限就是 24 小时(文档明示"每并一次延长,至多 24h,超窗开新单"),
# Alertmanager 的 resolve_timeout 是同一语义(安静即解决)。做成接入可配置的
# 策略项留给第二期(升级链一起),先落全平台统一值。
ABSORB_WINDOW_H = 24

_FAULT_VERDICTS = ("root_cause", "critical_failure")       # 确诊故障
_ABNORMAL_VERDICTS = ("impact", "victims_only")            # 有异常、未定位到根因
_OPEN_STATUSES = ("open", "diagnosed", "mitigating")
# 等级是结论语义的映射,不是拍数值:P0 只留给人工标记,系统不自动定 P0
_SEVERITY_BY_VERDICT = {**{v: "P1" for v in _FAULT_VERDICTS},
                        **{v: "P2" for v in _ABNORMAL_VERDICTS}}
_SEVERITIES = ("P0", "P1", "P2", "P3")
_MANUAL_TRANSITIONS = ("open", "resolved", "closed")   # 按钮能去的终点(重开/解决/关闭)
# 人工转移的合法边(按来源状态限定):已并入的单是别人的一部分,动它没意义;
# 已关闭只能重开,不能再"解决"一遍——解决必须在还开着的时候(2026-10-24 审计修正)
_MANUAL_EDGES = {
    "open": {"resolved", "closed"},
    "diagnosed": {"resolved", "closed", "open"},
    "mitigating": {"resolved", "closed", "open"},
    "resolved": {"closed", "open"},
    "closed": {"open"},
    "merged": set(),
}


def _title_of(service: str, platform: str | None) -> str:
    return f"{service or platform or '平台'} 疑似故障"


async def _event(incident_id: int, kind: str, detail: dict,
                 ref_id: str | None = None) -> None:
    async with db_base.async_session() as sess:
        sess.add(IncidentEvent(incident_id=incident_id, kind=kind,
                               ref_id=ref_id, detail=detail))
        await sess.commit()


async def _sweep() -> None:
    """超窗收口,在每次并单判定和列表读取前跑:
    - 超过吸收窗(24h)没有新告警、没有结论的 open 单 → 自动 resolved(带说明)。
      没有结论也没人管的陈年旧单不该一直躺在未关闭列表里吸收后来的新故障;
    - diagnosed/mitigating 超窗**不**强关——已定位待修复的事故被定时器藏掉,
      等真出了事就是掩盖 P0,它们只是不再吸收新告警(见 _open_incidents 的窗)。"""
    from sqlalchemy import select
    cutoff = datetime.now() - timedelta(hours=ABSORB_WINDOW_H)
    stale: list[int] = []
    async with db_base.async_session() as sess:
        rows = (await sess.execute(
            select(Incident).where(Incident.status == "open",
                                   Incident.updated_at < cutoff))).scalars().all()
        for r in rows:
            r.status = "resolved"
            r.note = f"超过 {ABSORB_WINDOW_H} 小时无新告警、无诊断结论,自动收口"
            r.updated_at = datetime.now()
            stale.append(r.id)
        await sess.commit()
    for iid in stale:
        await _event(iid, "status", {"from": "open", "to": "resolved",
                                     "note": "超窗自动收口", "by": "system"})


async def _open_incidents() -> list[Incident]:
    """还能吸收新告警的未关闭事故:状态未关闭,且在吸收窗内(24h 内有过动静)。
    超窗的旧单哪怕还开着也不再吸收——后来的同服务告警应该开新单。"""
    from sqlalchemy import select
    cutoff = datetime.now() - timedelta(hours=ABSORB_WINDOW_H)
    async with db_base.async_session() as sess:
        rows = (await sess.execute(
            select(Incident).where(Incident.status.in_(_OPEN_STATUSES),
                                   Incident.updated_at >= cutoff)
            .order_by(Incident.id))).scalars().all()
        return list(rows)


def _scope_of(inc: Incident) -> set[str]:
    out = set(inc.scope or [])
    if inc.service:
        out.add(inc.service)
    if inc.root_cause_service:
        out.add(inc.root_cause_service)
    return out


# ---------- 挂点 1:告警触发 ----------

async def on_alert(intake_event_id: int, sentinel_id: int, sentinel_name: str,
                   trigger: str, service: str, platform: str | None,
                   diag_id: str | None) -> int | None:
    """哨兵触发了诊断:先判并单,并不上才开新事故。返回事故 id(失败返回 None)。"""
    try:
        await _sweep()
        for inc in await _open_incidents():
            # 并单判据①:告警指向的服务在这起事故的覆盖范围里(scope 只会被
            # 诊断结果往里加服务,不会被猜测扩围,所以命中就是实打实的重叠)
            if service and service in _scope_of(inc):
                from sqlalchemy import update
                async with db_base.async_session() as sess:
                    row = await sess.get(Incident, inc.id)
                    if service not in (row.scope or []):
                        row.scope = sorted(set(row.scope or []) | {service})
                    if not row.diagnosis_id and diag_id:
                        row.diagnosis_id = diag_id
                    row.updated_at = datetime.now()
                    # 告警计数用 SQL 原子自增:告警风暴下多条并入并发到达,
                    # "读出来加一再写回"会丢计数(2026-10-24 审计修正)
                    await sess.execute(
                        update(Incident).where(Incident.id == inc.id)
                        .values(alert_count=Incident.alert_count + 1))
                    await sess.commit()
                await _event(inc.id, "alert", {
                    "intake_event_id": intake_event_id, "sentinel": sentinel_name,
                    "trigger": trigger, "service": service, "diagnosis_id": diag_id,
                    "merged": True}, ref_id=str(intake_event_id))
                logger.info("告警并入事故 #%s:%s/%s", inc.id, sentinel_name,
                            service or "(全平台)")
                return inc.id
        async with db_base.async_session() as sess:
            inc = Incident(title=_title_of(service, platform), platform=platform,
                           service=service or "",
                           scope=[service] if service else [],
                           diagnosis_id=diag_id)
            sess.add(inc)
            await sess.flush()
            iid = inc.id
            await sess.commit()
        await _event(iid, "alert", {
            "intake_event_id": intake_event_id, "sentinel": sentinel_name,
            "trigger": trigger, "service": service, "diagnosis_id": diag_id},
            ref_id=str(intake_event_id))
        logger.info("新事故 #%s:%s/%s diag=%s", iid, sentinel_name, service or "(全平台)",
                    diag_id)
        return iid
    except Exception:  # noqa: BLE001
        logger.warning("事故开单/并单失败", exc_info=True)
        return None


# ---------- 挂点 2:诊断终态 ----------

async def _incident_of_diagnosis(diag_id: str) -> Incident | None:
    """这起诊断挂在哪起事故上:先查事故首诊断,再查时间线里告警事件的 detail
    (并单进来的诊断不在首诊字段里,在告警事件的 diagnosis_id 上)。"""
    from sqlalchemy import select
    async with db_base.async_session() as sess:
        inc = (await sess.execute(
            select(Incident).where(Incident.diagnosis_id == diag_id))).scalar_one_or_none()
        if inc is not None:
            return inc
        rows = (await sess.execute(
            select(IncidentEvent).where(IncidentEvent.kind == "alert")
            .order_by(IncidentEvent.id))).scalars().all()
        for e in rows:
            if (e.detail or {}).get("diagnosis_id") == diag_id:
                return await sess.get(Incident, e.incident_id)
        return None


async def on_diagnosed(diag_id: str, run: dict | None = None) -> None:
    """诊断终态:推进所属事故(状态/根因/scope/等级/标题),再做同根因并单。

    聊天发起的诊断(run 没挂事故)不自动开单——事故从告警来;但它若撞上
    未关闭事故的 scope,把结论挂进去(那起事故需要这份证据)。
    """
    try:
        result: dict = (run or {}).get("result") or {}
        if not result:
            from app.db import repository
            archived = await repository.get_diagnosis_run(diag_id)
            result = (archived or {}).get("result") or {}
        verdict = result.get("verdict") or ""
        root = result.get("root_cause_service") or None
        fault = (result.get("fault_type") or {}).get("text") or None
        service = (run or {}).get("service") or result.get("service") or ""
        subgraph = {n.get("service") for n in (result.get("subgraph") or {}).get("nodes", [])
                    if n.get("service")}
        inc = await _incident_of_diagnosis(diag_id)
        if inc is None:
            if not service:
                return
            for cand in await _open_incidents():
                if service in _scope_of(cand):
                    await _event(cand.id, "diagnosis", {
                        "diagnosis_id": diag_id, "verdict": verdict,
                        "root_cause": root, "fault_type": fault,
                        "source": "chat"}, ref_id=diag_id)
                    inc = cand
                    break
            if inc is None:
                return
        # 推进状态:确诊→diagnosed;未见故障→resolved(自动关,如实);
        # 有异常没定位/证据不足 → 保持 open(故障在,事故不该自己关门)
        async with db_base.async_session() as sess:
            row = await sess.get(Incident, inc.id)
            scope = set(row.scope or []) | subgraph | ({root} if root else set()) \
                | ({service} if service else set())
            row.scope = sorted(scope)
            row.updated_at = datetime.now()
            if verdict in _FAULT_VERDICTS and root:
                if row.root_cause_service is None:
                    row.root_cause_service = root
                    row.fault_type = fault
                    row.title = f"[{root}] {fault or '故障'}"
                if row.status == "open":
                    row.status = "diagnosed"
                if row.severity in ("P2", "P3"):
                    row.severity = _SEVERITY_BY_VERDICT[verdict]
            elif verdict in _ABNORMAL_VERDICTS:
                if row.severity == "P3":
                    row.severity = _SEVERITY_BY_VERDICT[verdict]
            elif verdict in ("clear", "no_data"):
                row.status = "resolved"
                row.note = f"诊断结论 {verdict},未见故障,自动关闭"
            await sess.commit()
        await _event(inc.id, "diagnosis", {
            "diagnosis_id": diag_id, "verdict": verdict, "root_cause": root,
            "fault_type": fault,
            "confidence": (result.get("confidence") or {}).get("tier")},
            ref_id=diag_id)
        # 并单判据②:同根因**且同故障形态**(同平台)的未关闭事故并入本事故。
        # 只比根因服务不够——同一个服务可以同时挂两种不同的病(2026-10-24 审计
        # 修正:实现补上了设计里本来就有、代码里漏掉的形态比对)。形态有一侧
        # 缺失就不并,按"宁可漏并"处理。先到者为主——已经带着这个根因的事故
        # 是"先立案"的那起,后来者并进去,时间线两边都留痕。
        if root and verdict in _FAULT_VERDICTS:
            for other in await _open_incidents():
                if other.id != inc.id and other.root_cause_service == root \
                        and (other.fault_type or None) == (fault or None) \
                        and (other.platform or None) == (inc.platform or None):
                    await _merge(src=inc.id, dst=other.id)
                    break
    except Exception:  # noqa: BLE001
        logger.warning("事故联动(诊断)失败 diag=%s", diag_id, exc_info=True)


async def _merge(src: int, dst: int) -> None:
    """事故 src 整体并入 dst:src 关成 merged(时间线保留),告警计数和 scope 归并过去。"""
    async with db_base.async_session() as sess:
        s, d = await sess.get(Incident, src), await sess.get(Incident, dst)
        if s is None or d is None or s.id == d.id:
            return
        d.alert_count = (d.alert_count or 1) + (s.alert_count or 1)
        d.scope = sorted(set(d.scope or []) | set(s.scope or []))
        d.updated_at = datetime.now()
        s.status = "merged"
        s.note = f"已并入事故 #{dst}(同根因 {s.root_cause_service})"
        s.updated_at = datetime.now()
        await sess.commit()
    await _event(src, "status", {"from": "open", "to": "merged", "merged_into": dst})
    await _event(dst, "status", {"from": None, "to": "merged_in",
                                 "absorbed": src, "alerts": True})
    logger.info("事故 #%s 并入 #%s(同根因)", src, dst)


# ---------- 挂点 3:修复回执 ----------

async def on_repair(repair_id: str, diag_id: str | None, verdict: str | None,
                    status: str | None, action: str | None = None) -> None:
    """修复回执:派单→mitigating;修复成功→resolved(失败不自动关,留给人工)。"""
    try:
        if not diag_id:
            return
        inc = await _incident_of_diagnosis(diag_id)
        if inc is None:
            return
        async with db_base.async_session() as sess:
            row = await sess.get(Incident, inc.id)
            if row.status in _OPEN_STATUSES:
                if status == "running":
                    row.status = "mitigating"
                elif verdict == "succeeded":
                    row.status = "resolved"
                row.updated_at = datetime.now()
                await sess.commit()
        await _event(inc.id, "repair", {"repair_id": repair_id, "diagnosis_id": diag_id,
                                        "verdict": verdict, "action": action},
                     ref_id=repair_id)
    except Exception:  # noqa: BLE001
        logger.warning("事故联动(修复)失败 repair=%s", repair_id, exc_info=True)


# ---------- API ----------

class _StatusBody(BaseModel):
    status: str
    note: str | None = None


class _SeverityBody(BaseModel):
    severity: str


class _SplitBody(BaseModel):
    event_id: int


@router.get("")
async def list_incidents(status: str | None = None) -> dict:
    """事故列表 + 降噪统计(告警条数 vs 事故数——这是给值班员看价值的最硬数字)。"""
    await _sweep()
    from sqlalchemy import func, select
    async with db_base.async_session() as sess:
        q = select(Incident).order_by(Incident.status.in_(("open", "diagnosed",
                                                           "mitigating")).desc(),
                                      Incident.updated_at.desc())
        if status:
            q = q.where(Incident.status == status)
        rows = (await sess.execute(q)).scalars().all()
        alerts_total = (await sess.execute(
            select(func.sum(Incident.alert_count)))).scalar() or 0
        return {"incidents": [_inc_dict(r) for r in rows],
                "stats": {"open": sum(1 for r in rows if r.status in _OPEN_STATUSES),
                          "alerts_total": int(alerts_total),
                          "incidents_total": len(rows)}}


def _inc_dict(r: Incident) -> dict:
    return {"id": r.id, "title": r.title, "severity": r.severity, "status": r.status,
            "platform": r.platform, "service": r.service,
            "root_cause_service": r.root_cause_service, "fault_type": r.fault_type,
            "diagnosis_id": r.diagnosis_id, "alert_count": r.alert_count,
            "scope": r.scope or [], "note": r.note,
            "created_at": r.created_at, "updated_at": r.updated_at}


@router.get("/{iid}")
async def incident_detail(iid: int) -> dict:
    """事故详情:时间线 + 关联诊断(可跳转诊断详情页) + 关联修复回执。"""
    from sqlalchemy import select
    async with db_base.async_session() as sess:
        inc = await sess.get(Incident, iid)
        if inc is None:
            return {"error": "not_found"}
        events = (await sess.execute(
            select(IncidentEvent).where(IncidentEvent.incident_id == iid)
            .order_by(IncidentEvent.id))).scalars().all()
        ev_list = [{"id": e.id, "kind": e.kind, "ref_id": e.ref_id,
                    "detail": e.detail or {}, "created_at": e.created_at}
                   for e in events]
        diag_ids = {inc.diagnosis_id} | {e["ref_id"] for e in ev_list
                                         if e["kind"] == "diagnosis" and e["ref_id"]}
        diag_ids.discard(None)
        diags = []
        for did in sorted(diag_ids):
            d = (await sess.execute(
                select(DiagnosisRun).where(DiagnosisRun.id == did))).scalar_one_or_none()
            if d is not None:
                diags.append({"id": d.id, "verdict": d.verdict,
                              "root_cause_service": d.root_cause_service,
                              "confirmation": d.confirmation, "created_at": d.created_at})
        repairs = (await sess.execute(
            select(RepairRun).where(RepairRun.diagnosis_id.in_(diag_ids or ["-"]))
            .order_by(RepairRun.id))).scalars().all() if diag_ids else []
        return {"incident": _inc_dict(inc), "events": ev_list, "diagnoses": diags,
                "repairs": [{"id": p.id, "diagnosis_id": p.diagnosis_id,
                             "verdict": p.verdict, "action": p.action,
                             "created_at": p.created_at} for p in repairs]}


@router.post("/{iid}/status")
async def set_status(iid: int, body: _StatusBody) -> dict:
    """人工状态推进:解决 / 关闭 / 重开。diagnosed/mitigating 只由系统推进,
    且转移按来源状态校验(合法边见 _MANUAL_EDGES)。"""
    if body.status not in _MANUAL_TRANSITIONS:
        return {"error": f"状态只能手工切到 {'/'.join(_MANUAL_TRANSITIONS)}"}
    async with db_base.async_session() as sess:
        inc = await sess.get(Incident, iid)
        if inc is None:
            return {"error": "not_found"}
        if body.status not in _MANUAL_EDGES.get(inc.status, set()):
            return {"error": f"当前状态 {inc.status} 不能直接改成 {body.status}"}
        old = inc.status
        inc.status = body.status
        inc.updated_at = datetime.now()
        if body.note:
            inc.note = body.note
        await sess.commit()
    await _event(iid, "status", {"from": old, "to": body.status, "note": body.note,
                                 "by": "manual"})
    return {"ok": True}


@router.post("/{iid}/severity")
async def set_severity(iid: int, body: _SeverityBody) -> dict:
    if body.severity not in _SEVERITIES:
        return {"error": "等级只能是 P0/P1/P2/P3"}
    async with db_base.async_session() as sess:
        inc = await sess.get(Incident, iid)
        if inc is None:
            return {"error": "not_found"}
        old = inc.severity
        inc.severity = body.severity
        inc.updated_at = datetime.now()
        await sess.commit()
    await _event(iid, "status", {"severity": {"from": old, "to": body.severity},
                                 "by": "manual"})
    return {"ok": True}


@router.post("/{iid}/split")
async def split(iid: int, body: _SplitBody) -> dict:
    """拆单:把一条并进来的告警(连同它的诊断)拆出去成独立事故。

    宁可漏并不可错并的另一面——错并了要能一键纠正。拆出后两边各记一笔,
    时间线完整可审计。
    """
    async with db_base.async_session() as sess:
        inc = await sess.get(Incident, iid)
        ev = await sess.get(IncidentEvent, body.event_id)
        if inc is None or ev is None or ev.incident_id != iid or ev.kind != "alert":
            return {"error": "这条时间线不是可拆的告警"}
        detail = ev.detail or {}
        service = detail.get("service") or ""
        new = Incident(title=_title_of(service, inc.platform), platform=inc.platform,
                       service=service, scope=[service] if service else [],
                       diagnosis_id=detail.get("diagnosis_id"), severity="P3")
        sess.add(new)
        await sess.flush()
        ev.incident_id = new.id
        inc.alert_count = max((inc.alert_count or 1) - 1, 1)
        inc.updated_at = datetime.now()
        await sess.commit()
        new_id = new.id
    await _event(iid, "split", {"event_id": body.event_id, "into": new_id})
    await _event(new_id, "split", {"from_incident": iid})
    return {"ok": True, "new_incident": new_id}
