"""事故中心单测:并单/同根因合并/拆单/状态推进(sqlite 内存库,不依赖 MySQL)。

判据都是确定性的(服务在不在事故 scope 里、根因服务是否相同),本测试锁的就是
这几条判据的行为——特别是"宁可漏并不可错并"的边界:scope 外的服务绝不并入。
"""
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import incidents as inc
from app.db import base as db_base
from app.db.models import Base, Incident, IncidentEvent, IntakeEvent


@pytest_asyncio.fixture()
async def _sqlite(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Incident.__table__, IncidentEvent.__table__, IntakeEvent.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_base, "async_session", factory)
    yield factory
    await engine.dispose()


async def _one(factory, model, **kw):
    async with factory() as s:
        s.add(model(**kw))
        await s.commit()


async def _get(factory, model, iid):
    async with factory() as s:
        return await s.get(model, iid)


async def test_first_alert_opens_incident(_sqlite):
    iid = await inc.on_alert(1, 7, "checkout 告警", "webhook", "checkout", "prod", "d1")
    row = await _get(_sqlite, Incident, iid)
    assert row.status == "open" and row.alert_count == 1
    assert row.service == "checkout" and row.scope == ["checkout"]


async def test_same_service_alerts_merge_into_one(_sqlite):
    """同一故障的五条告警 → 一张事故卡(降噪的兑现)。"""
    first = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    for n, name in enumerate(["哨兵B", "哨兵C", "哨兵D", "哨兵E"], 2):
        merged = await inc.on_alert(n, 8 + n, name, "threshold", "checkout", "prod", f"d{n}")
        assert merged == first, "同服务的后续告警必须并进同一起事故"
    row = await _get(_sqlite, Incident, first)
    assert row.alert_count == 5


async def test_out_of_scope_alert_does_not_merge(_sqlite):
    """scope 外的服务绝不并入——宁可漏并不可错并。"""
    first = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    other = await inc.on_alert(2, 8, "哨兵B", "webhook", "currency", "prod", "d2")
    assert other != first
    rows = [await _get(_sqlite, Incident, i) for i in (first, other)]
    assert all(r.alert_count == 1 for r in rows)


async def test_diagnosis_grows_scope_then_later_alerts_merge(_sqlite):
    """诊断把子图服务并进 scope 后,指向子图服务的告警也能并进来。"""
    first = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    await inc.on_diagnosed("d1", {
        "id": "d1", "service": "checkout", "platform": "prod",
        "result": {"verdict": "root_cause", "root_cause_service": "payment",
                   "fault_type": {"text": "服务不可达"},
                   "subgraph": {"nodes": [{"service": "payment"},
                                          {"service": "checkout"}]}}})
    row = await _get(_sqlite, Incident, first)
    assert row.status == "diagnosed" and row.root_cause_service == "payment"
    assert row.severity == "P1" and "[payment]" in row.title
    assert "payment" in row.scope
    # 指向根因服务的告警现在能并进来了
    merged = await inc.on_alert(2, 8, "哨兵B", "threshold", "payment", "prod", "d2")
    assert merged == first


async def test_same_root_cause_incidents_merge(_sqlite):
    """并单判据②:两条哨兵各开一起事故,诊断收敛到同一根因 → 后者并入前者。"""
    a = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    b = await inc.on_alert(2, 8, "哨兵B", "webhook", "frontend", "prod", "d2")
    assert a != b
    await inc.on_diagnosed("d1", {
        "id": "d1", "service": "checkout", "platform": "prod",
        "result": {"verdict": "root_cause", "root_cause_service": "payment",
                   "fault_type": {"text": "服务不可达"}, "subgraph": {"nodes": []}}})
    await inc.on_diagnosed("d2", {
        "id": "d2", "service": "frontend", "platform": "prod",
        "result": {"verdict": "root_cause", "root_cause_service": "payment",
                   "fault_type": {"text": "服务不可达"}, "subgraph": {"nodes": []}}})
    row_b = await _get(_sqlite, Incident, b)
    row_a = await _get(_sqlite, Incident, a)
    assert row_b.status == "merged" and f"#{a}" in (row_b.note or "")
    assert row_a.alert_count == 2, "并入方的告警计数要累加过来"


async def test_clear_verdict_auto_resolves(_sqlite):
    iid = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    await inc.on_diagnosed("d1", {
        "id": "d1", "service": "checkout", "platform": "prod",
        "result": {"verdict": "clear", "root_cause_service": None,
                   "fault_type": None, "subgraph": {"nodes": []}}})
    row = await _get(_sqlite, Incident, iid)
    assert row.status == "resolved"


async def test_split_detaches_alert_into_new_incident(_sqlite):
    """拆单:错并的告警一键拆出成独立事故,两边计数各归各。"""
    iid = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    await inc.on_alert(2, 8, "哨兵B", "webhook", "checkout", "prod", "d2")
    async with _sqlite() as s:
        ev = (await s.execute(
            __import__("sqlalchemy").select(IncidentEvent)
            .where(IncidentEvent.kind == "alert"))).scalars().first()
        eid = ev.id
    out = await inc.split(iid, inc._SplitBody(event_id=eid))
    assert out["ok"] and out["new_incident"] != iid
    new = await _get(_sqlite, Incident, out["new_incident"])
    old = await _get(_sqlite, Incident, iid)
    assert new.alert_count == 1 and old.alert_count == 1


async def test_manual_status_and_severity(_sqlite):
    iid = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    assert (await inc.set_status(iid, inc._StatusBody(status="resolved", note="手工")))
    assert (await inc.set_status(iid, inc._StatusBody(status="open", note="重开")))
    assert (await inc.set_severity(iid, inc._SeverityBody(severity="P0")))
    row = await _get(_sqlite, Incident, iid)
    assert row.status == "open" and row.severity == "P0"
    bad = await inc.set_status(iid, inc._StatusBody(status="mitigating"))
    assert bad["error"], "mitigating 只能由系统推进,不给按钮"


async def test_same_root_different_fault_type_do_not_merge(_sqlite):
    """同一根因服务但故障形态不同 = 两种病,不许互相吞并(审计修正③)。"""
    a = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    b = await inc.on_alert(2, 8, "哨兵B", "webhook", "frontend", "prod", "d2")
    await inc.on_diagnosed("d1", {
        "id": "d1", "service": "checkout", "platform": "prod",
        "result": {"verdict": "root_cause", "root_cause_service": "payment",
                   "fault_type": {"text": "服务不可达"}, "subgraph": {"nodes": []}}})
    await inc.on_diagnosed("d2", {
        "id": "d2", "service": "frontend", "platform": "prod",
        "result": {"verdict": "root_cause", "root_cause_service": "payment",
                   "fault_type": {"text": "CPU 耗尽"}, "subgraph": {"nodes": []}}})
    row_b = await _get(_sqlite, Incident, b)
    assert row_b.status != "merged", "同根因不同形态是两起故障,不能被并单吃掉"


async def test_stale_open_incident_auto_resolves_and_stops_absorbing(_sqlite):
    """超过吸收窗(24h)的 open 单自动收口,且不再吸收——后来的同服务告警开新单。"""
    old = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    async with _sqlite() as s:
        row = await s.get(Incident, old)
        row.updated_at = datetime.now() - timedelta(hours=25)
        await s.commit()
    fresh = await inc.on_alert(2, 8, "哨兵B", "webhook", "checkout", "prod", "d3")
    assert fresh != old, "超窗旧单不该再吸收,新告警应开新单"
    row = await _get(_sqlite, Incident, old)
    assert row.status == "resolved" and "收口" in (row.note or "")


async def test_manual_transition_edges_are_validated(_sqlite):
    """人工转移按来源状态校验:已关闭只能重开;已并入的单不许人工动(审计修正④)。"""
    iid = await inc.on_alert(1, 7, "哨兵A", "webhook", "checkout", "prod", "d1")
    await inc.set_status(iid, inc._StatusBody(status="closed", note="处理完关掉"))
    bad = await inc.set_status(iid, inc._StatusBody(status="resolved"))
    assert bad.get("error"), "已关闭的事故不能直接改成已解决"
    ok = await inc.set_status(iid, inc._StatusBody(status="open", note="重开"))
    assert ok["ok"]
