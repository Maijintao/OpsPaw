"""告警接入:把"什么时候该看一眼"这件事接进来。

## 为什么是"接入"而不是"一个 webhook"

只做 webhook 等于要求客户已经有一套成熟告警——真实情况往往没有,或者不想把阈值
维护两份。所以一条接入支持三种触发方式,可以同时开:

  schedule   定时巡检:每 N 分钟自动跑一遍诊断(夜莺那种告警引擎的做法)
  webhook    外部告警:告警平台 POST 进来就触发(Alertmanager 那种)
  threshold  指标阈值:自己查 Prometheus,超了才触发(不依赖外部告警平台)

## 收敛是刚需,不是锦上添花

同一个故障在 5 分钟里被触发 20 次,就会跑 20 遍诊断——既烧钱又刷屏。
所以每条接入带一个静默期:窗口内只触发一次,其余如实记一条"被静默"的流水。

## 触发记录

每次触发(含被跳过)都落一条 intake_events。值班员最常问的是"它刚才为什么报警、
结论是什么",没有这条流水,接入就只是个配置页。
"""
import asyncio
import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, select, text

from app.db import base as db_base
from app.db.models import IntakeEvent, Sentinel

logger = logging.getLogger("app")
router = APIRouter()

CHECK_LABELS = {"error": "错误率", "latency": "p95 时延", "calls": "调用量",
                "cpu": "CPU", "memory": "内存"}
OPS = {">": "高于", "<": "低于"}

DEFAULT_TRIGGERS = {
    "schedule": {"enabled": False, "mode": "interval", "interval_min": 10,
                 "cron": "0 0 * * *"},
    "webhook": {"enabled": True},
    "threshold": {"enabled": False, "metric": "error", "op": ">", "value": None},
}
DEFAULT_POLICY = {"silence_min": 15, "hours": [0, 24]}

_READY = False
_scheduler: asyncio.Task | None = None


# ---------- 建表 / 补列 ----------

async def ensure_tables() -> None:
    """建表 + 给老表补列(幂等)。

    这套项目没有迁移工具,表是手建的。老库里 sentinels 已经有数据,直接
    create_all 不会补列,所以按 pragma 查一遍缺什么就 ALTER 加什么。
    """
    global _READY
    if _READY:
        return
    async with db_base.engine.begin() as conn:
        await conn.run_sync(Sentinel.__table__.create, checkfirst=True)
        await conn.run_sync(IntakeEvent.__table__.create, checkfirst=True)
        rows = (await conn.execute(text("PRAGMA table_info(sentinels)"))).fetchall()
        have = {r[1] for r in rows}
        for col, ddl in (("triggers", "JSON"), ("policy", "JSON"),
                         ("action", "VARCHAR(32) DEFAULT 'diagnose'"),
                         ("forward_url", "VARCHAR(512) DEFAULT ''"),
                         ("last_fired_at", "DATETIME")):
            if col not in have:
                await conn.execute(text(f"ALTER TABLE sentinels ADD COLUMN {col} {ddl}"))
                logger.info("sentinels 补列:%s", col)
    _READY = True


# ---------- 配置归一 ----------

def _cron_field(spec: str, val: int, lo: int, hi: int) -> bool:
    """单个 cron 字段匹配。支持 *、N、N-M、*/N、逗号列表。"""
    for part in spec.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                continue
            if step > 0 and (val - lo) % step == 0:
                return True
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                if int(a) <= val <= int(b):
                    return True
            except ValueError:
                continue
            continue
        if part.isdigit() and int(part) == val:
            return True
    return False


def cron_match(dt: datetime, expr: str) -> bool:
    """5 段 cron(分 时 日 月 周)匹配。周:0=周日,和标准 cron 一致。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    mi, ho, dom, mo, dow = parts
    try:
        return (_cron_field(mi, dt.minute, 0, 59)
                and _cron_field(ho, dt.hour, 0, 23)
                and _cron_field(dom, dt.day, 1, 31)
                and _cron_field(mo, dt.month, 1, 12)
                and _cron_field(dow, (dt.weekday() + 1) % 7, 0, 6))
    except Exception:  # noqa: BLE001
        return False


def _triggers(s: Sentinel) -> dict:
    t = {k: dict(v) for k, v in DEFAULT_TRIGGERS.items()}
    for k, v in (s.triggers or {}).items():
        if isinstance(v, dict):
            t[k] = {**t.get(k, {}), **v}
    return t


def _policy(s: Sentinel) -> dict:
    p = dict(DEFAULT_POLICY)
    for k, v in (s.policy or {}).items():
        p[k] = v
    return p


def _in_hours(policy: dict, now: datetime) -> bool:
    hours = policy.get("hours") or [0, 24]
    try:
        start, end = int(hours[0]), int(hours[1])
    except (TypeError, ValueError, IndexError):
        return True
    if start == end:
        return True
    h = now.hour
    return start <= h < end if start < end else (h >= start or h < end)


def _silenced(s: Sentinel, now: datetime) -> bool:
    """静默期内只触发一次:同一条接入 N 分钟内不重复跑。"""
    if not s.last_fired_at:
        return False
    mins = int(_policy(s).get("silence_min") or 0)
    return mins > 0 and now - s.last_fired_at < timedelta(minutes=mins)


# ---------- 触发 ----------

async def _record(sid: int, trigger: str, service: str,
                  diag_id: str | None = None, skipped: str | None = None) -> int | None:
    """记一条触发流水,返回流水 id(事故中心要拿它把告警挂到时间线上)。"""
    async with db_base.async_session() as sess:
        ev = IntakeEvent(intake_id=sid, fired_at=datetime.now(), trigger=trigger,
                         service=service or "", diagnosis_id=diag_id,
                         skipped=skipped)
        sess.add(ev)
        await sess.flush()
        eid = ev.id
        await sess.commit()
        return eid


async def _fire(s: Sentinel, trigger: str, payload: dict,
                force: bool = False) -> dict:
    """触发一次诊断。被停用/不在时段/静默中都会如实记一条流水再返回。"""
    from app.diagnosis import runner

    now = datetime.now()
    if not s.enabled:
        await _record(s.id, trigger, "", skipped="已停用")
        return {"ok": False, "code": "disabled", "message": "这条接入已停用"}
    if not _in_hours(_policy(s), now):
        await _record(s.id, trigger, "", skipped="不在生效时段")
        return {"ok": False, "code": "off_hours", "message": "当前不在生效时段"}
    if not force and _silenced(s, now):
        await _record(s.id, trigger, "", skipped="静默中")
        return {"ok": False, "code": "silenced",
                "message": f"静默中({_policy(s).get('silence_min')} 分钟内不重复触发)"}

    service = str(payload.get("service") or (s.services[0] if s.services else ""))
    platform = None
    if s.target_id:
        from app.db import repository
        tgt = await repository.get_target(s.target_id)
        platform = tgt.name if tgt else None

    diag_id = runner.start(service, _symptom_of(s, payload, trigger), platform=platform)

    async with db_base.async_session() as sess:
        row = await sess.get(Sentinel, s.id)
        if row is not None:
            row.trigger_count = (row.trigger_count or 0) + 1
            row.last_fired_at = now
            row.last_triggered_at = now
            row.last_diagnosis_id = diag_id
            await sess.commit()
    _eid = await _record(s.id, trigger, service, diag_id=diag_id)
    logger.info("告警接入触发 id=%s name=%s trigger=%s service=%s diag=%s",
                s.id, s.name, trigger, service or "(全平台)", diag_id)

    # 事故中心联动:这声告警要么并入未关闭的事故,要么开一起新事故。
    # 增强信号,失败只记日志,绝不反过来弄死触发链。
    try:
        from app.api import incidents as incidents_mod
        await incidents_mod.on_alert(_eid or 0, s.id, s.name, trigger, service,
                                     platform, diag_id)
    except Exception:  # noqa: BLE001
        logger.warning("事故联动(告警)失败 intake=%s", s.id, exc_info=True)

    # 处理办法:只诊断 / 自动转发 / 自动修复。后两个在后台跟着诊断结果走,
    # 不阻塞触发方(告警平台等不起)。
    # action 可以是逗号组合:"forward,repair" = 既转发又派修复
    acts = {x.strip() for x in (s.action or "diagnose").split(",") if x.strip()}
    if acts & {"forward", "repair"}:
        asyncio.create_task(_follow_up(s, diag_id, service, acts,
                                       (s.forward_url or "").strip()))
    return {"ok": True, "diagnosis_id": diag_id, "service": service or "(全平台)"}


async def _post_json(url: str, body: dict) -> str | None:
    """把结果推给外部接口。失败不抛,返回错误说明给流水记一笔。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=body)
            return None if r.status_code < 400 else f"HTTP {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}"


async def _follow_up(s: Sentinel, diag_id: str, service: str,
                     acts: set[str], forward_url: str) -> None:
    """诊断跑完后按处理办法办事:转发结果,或自动派修复单。

    为什么在后台等:诊断要一两分钟,触发方(告警平台)等不起。
    """
    from app.diagnosis import runner

    # 先立刻推一条"已触发",让外部系统知道有人在处理了
    if "forward" in acts and forward_url:
        err = await _post_json(forward_url, {
            "event": "triggered", "intake": s.name, "service": service,
            "diagnosis_id": diag_id, "at": datetime.now().isoformat()})
        if err:
            await _record(s.id, "forward", service, skipped=f"转发失败:{err}")

    # 等诊断出结论(最多 8 分钟)
    deadline = datetime.now() + timedelta(minutes=8)
    result: dict = {}
    while datetime.now() < deadline:
        await asyncio.sleep(10)
        run = runner.get(diag_id)
        if not run:
            continue
        if run.get("status") == "running":
            continue
        result = run.get("result") or {}
        break
    else:
        return

    verdict = result.get("verdict")
    root = result.get("root_cause_service")
    if "forward" in acts and forward_url:
        err = await _post_json(forward_url, {
            "event": "diagnosed", "intake": s.name, "service": service,
            "diagnosis_id": diag_id, "verdict": verdict, "root_cause": root,
            "fault_type": (result.get("fault_type") or {}).get("text"),
            "confidence": (result.get("confidence") or {}).get("tier"),
            "narrative": result.get("narrative"), "at": datetime.now().isoformat()})
        if err:
            await _record(s.id, "forward", service, skipped=f"转发失败:{err}")

    if "repair" in acts and verdict in ("root_cause", "critical_failure") and root:
        try:
            from app.repair import agent as repair_agent
            out = await repair_agent.start_for_diagnosis(diag_id)
            await _record(s.id, "repair", service, skipped=f"已派修复 {out.get('repair_id')}")
        except Exception as exc:  # noqa: BLE001
            await _record(s.id, "repair", service, skipped=f"派修复失败:{type(exc).__name__}")


def _symptom_of(s: Sentinel, payload: dict, trigger: str) -> str:
    if payload.get("symptom"):
        return str(payload["symptom"])[:200]
    labels = [CHECK_LABELS.get(c, c) for c in (s.checks or [])]
    who = "、".join(s.services) if s.services else "平台"
    sched = _triggers(s)["schedule"]
    sched_txt = "定时巡检"
    if sched.get("enabled") and (sched.get("mode") or "interval") == "cron":
        sched_txt = f"定时巡检({sched.get('cron')})"
    why = {"schedule": sched_txt, "webhook": "外部告警", "threshold": "指标超阈值",
           "manual": "手动触发"}.get(trigger, "触发")
    if labels:
        return f"告警接入「{s.name}」{why}:{who} 的 {'、'.join(labels)} 异常,请定位原因"
    return f"告警接入「{s.name}」{why}:{who} 出现异常,请定位原因"


# ---------- 指标阈值 ----------

async def _threshold_hit(s: Sentinel) -> tuple[bool, str]:
    """自己查 Prometheus 判超限。返回 (是否命中, 说明)。"""
    th = _triggers(s)["threshold"]
    if not th.get("enabled"):
        return False, ""
    service = s.services[0] if s.services else ""
    if not service:
        return False, "阈值判定需要指定服务"
    metric = th.get("metric") or "error"
    try:
        value = float(th.get("value"))
    except (TypeError, ValueError):
        return False, "阈值配置非法"

    from app.tools import grafana
    tid = s.target_id
    if tid is None:
        from app.db import repository
        rows = [r for r in await repository.list_targets() if r.enabled]
        if not rows:
            return False, "没有启用的平台"
        tid = rows[0].id
    try:
        data = await grafana.series(tid, service, minutes=10)
    except Exception as exc:  # noqa: BLE001  查不到就不触发,别把调度器搞挂
        return False, f"取数失败:{type(exc).__name__}"
    card = next((c for c in data.get("cards") or [] if c["key"] == metric), None)
    if not card or card.get("latest") is None:
        return False, "取不到该维度数据"
    cur = card["latest"]
    # 错误率面板的原始值是 0~1,用户填的是百分数
    shown = cur * 100 if card.get("unit") == "%" else cur
    op = th.get("op") or ">"
    hit = shown > value if op == ">" else shown < value
    return hit, f"{card['title']} 当前 {shown:.2f}{op}{value}"


# ---------- 调度器 ----------

async def _tick() -> None:
    """每 30 秒过一遍:定时巡检到点没、阈值超了没。"""
    await ensure_tables()
    async with db_base.async_session() as sess:
        rows = (await sess.execute(
            select(Sentinel).where(Sentinel.enabled == 1))).scalars().all()

    for s in rows:
        trg = _triggers(s)
        sched = trg["schedule"]
        if sched.get("enabled"):
            now = datetime.now()
            if (sched.get("mode") or "interval") == "cron":
                # 定点:同一分钟内只触发一次,靠 last_fired_at 的分钟位判断
                expr = sched.get("cron") or ""
                if expr and cron_match(now, expr):
                    fired_min = (s.last_fired_at.replace(second=0, microsecond=0)
                                 if s.last_fired_at else None)
                    if fired_min != now.replace(second=0, microsecond=0):
                        await _fire(s, "schedule", {})
            else:
                mins = int(sched.get("interval_min") or 10)
                due = (not s.last_fired_at or
                       now - s.last_fired_at >= timedelta(minutes=mins))
                if due:
                    await _fire(s, "schedule", {})
        th = trg["threshold"]
        if th.get("enabled"):
            try:
                hit, why = await _threshold_hit(s)
            except Exception:  # noqa: BLE001
                logger.warning("阈值判定异常 id=%s", s.id, exc_info=True)
                continue
            if hit:
                await _fire(s, "threshold", {"symptom": f"指标超阈值:{why}"})


async def scheduler_loop() -> None:
    """常驻循环。单条接入出错不影响其它接入。"""
    await asyncio.sleep(20)          # 让应用先起完
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("告警接入调度器本轮异常", exc_info=True)
        await asyncio.sleep(30)


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is None or _scheduler.done():
        _scheduler = asyncio.create_task(scheduler_loop())


# ---------- 接口 ----------

def _public(s: Sentinel, events: list[dict] | None = None) -> dict:
    return {"id": s.id, "name": s.name, "target_id": s.target_id,
            "services": s.services or [], "checks": s.checks or [],
            "triggers": _triggers(s), "policy": _policy(s),
            "action": s.action or "diagnose",
            "forward_url": s.forward_url or "",
            "token": s.token, "enabled": bool(s.enabled),
            "trigger_count": s.trigger_count or 0,
            "last_fired_at": s.last_fired_at.isoformat() if s.last_fired_at else None,
            "last_diagnosis_id": s.last_diagnosis_id,
            "events": events or []}


@router.get("/api/sentinels")
async def list_sentinels():
    await ensure_tables()
    async with db_base.async_session() as sess:
        rows = (await sess.execute(select(Sentinel).order_by(Sentinel.id))).scalars().all()
        evs = (await sess.execute(
            select(IntakeEvent).order_by(IntakeEvent.id.desc()).limit(200))).scalars().all()
    by: dict[int, list[dict]] = {}
    for e in evs:
        by.setdefault(e.intake_id, []).append({
            "at": e.fired_at.isoformat() if e.fired_at else "",
            "trigger": e.trigger, "service": e.service,
            "diagnosis_id": e.diagnosis_id, "skipped": e.skipped})
    return {"items": [_public(r, by.get(r.id, [])[:5]) for r in rows],
            "check_labels": CHECK_LABELS, "ops": OPS}


def _validate_threshold(triggers: dict) -> None:
    threshold = (triggers or {}).get("threshold") or {}
    if threshold.get("enabled"):
        try:
            float(threshold.get("value"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="启用指标阈值前请填写有效数值")


@router.post("/api/sentinels")
async def create_sentinel(body: dict):
    await ensure_tables()
    triggers = body.get("triggers") or DEFAULT_TRIGGERS
    _validate_threshold(triggers)
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="名称不能为空")
    row = Sentinel(name=name[:128],
                   target_id=body.get("target_id"),
                   services=[str(x) for x in (body.get("services") or [])][:20],
                   checks=[c for c in (body.get("checks") or []) if c in CHECK_LABELS],
                   triggers=triggers,
                   policy=body.get("policy") or DEFAULT_POLICY,
                   action=body.get("action") or "diagnose",
                   forward_url=str(body.get("forward_url") or "")[:512],
                   token=secrets.token_urlsafe(18), enabled=1)
    async with db_base.async_session() as sess:
        sess.add(row)
        await sess.commit()
        await sess.refresh(row)
    return _public(row)


@router.patch("/api/sentinels/{sid}")
async def update_sentinel(sid: int, body: dict):
    await ensure_tables()
    async with db_base.async_session() as sess:
        row = await sess.get(Sentinel, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="接入不存在")
        if "name" in body and str(body["name"]).strip():
            row.name = str(body["name"]).strip()[:128]
        if "services" in body:
            row.services = [str(x) for x in (body["services"] or [])][:20]
        if "checks" in body:
            row.checks = [c for c in (body["checks"] or []) if c in CHECK_LABELS]
        if "triggers" in body and isinstance(body["triggers"], dict):
            _validate_threshold(body["triggers"])
            row.triggers = body["triggers"]
        if "policy" in body and isinstance(body["policy"], dict):
            row.policy = body["policy"]
        if "action" in body:
            row.action = str(body["action"])[:32]
        if "forward_url" in body:
            row.forward_url = str(body["forward_url"] or "")[:512]
        if "enabled" in body:
            row.enabled = 1 if body["enabled"] else 0
        if body.get("rotate_token"):
            row.token = secrets.token_urlsafe(18)
        await sess.commit()
        await sess.refresh(row)
        return _public(row)


@router.delete("/api/sentinels/{sid}")
async def delete_sentinel(sid: int):
    await ensure_tables()
    async with db_base.async_session() as sess:
        await sess.execute(delete(Sentinel).where(Sentinel.id == sid))
        await sess.execute(delete(IntakeEvent).where(IntakeEvent.intake_id == sid))
        await sess.commit()
    return {"ok": True}


@router.post("/api/sentinels/{sid}/test")
async def test_sentinel(sid: int):
    """页面上的「立即跑一次」:跳过静默期,其它判断照旧。"""
    await ensure_tables()
    async with db_base.async_session() as sess:
        row = await sess.get(Sentinel, sid)
        if row is None:
            raise HTTPException(status_code=404, detail="接入不存在")
        return await _fire(row, "manual", {}, force=True)


@router.post("/api/sentinels/hook/{token}")
async def sentinel_hook(token: str, request: Request):
    """外部告警入口。body 可选:{"service": "...", "symptom": "..."}。"""
    await ensure_tables()
    async with db_base.async_session() as sess:
        row = (await sess.execute(
            select(Sentinel).where(Sentinel.token == token))).scalars().first()
        if row is None:
            raise HTTPException(status_code=404, detail="接入不存在或 token 已失效")
        if not _triggers(row)["webhook"].get("enabled", True):
            return {"ok": False, "code": "webhook_off", "message": "这条接入没开 webhook"}
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001  告警平台可能只发空 body
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return await _fire(row, "webhook", payload)
