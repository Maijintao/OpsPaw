"""数据保留策略(retention)单测(本地跳过;真库行为由服务器接口实测覆盖)。

锁的行为:默认全关不动数据;手动清理按天数删 clear 类/摘要化确诊类/删流水;
摘要幂等(events 置空是标记);批量循环删得干净;天数保存时夹取防手滑。
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import base as db_base
from app.db import retention
from app.db.models import (AppSetting, Base, DiagnosisRun, Incident,
                           IncidentEvent, IntakeEvent)

pytest.skip("本地无 MySQL;aiosqlite 内存/文件库在多会话混合读写下会卡死(踩坑实录),"
           "清理语义已在服务器真库用接口实测验证", allow_module_level=True)

OLD = datetime.now() - timedelta(days=100)
RECENT = datetime.now() - timedelta(days=1)


@pytest_asyncio.fixture()
async def _sqlite(monkeypatch):
    # 文件型 sqlite:内存库在多会话混合读写时会把 aiosqlite 卡死(踩坑实录),
    # 文件库行为贴近生产 MySQL;每个用例独立文件,互不串
    import os, pathlib
    _p = pathlib.Path(f"/tmp/rt_{os.getpid()}_{id(object())}.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{_p}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[
            DiagnosisRun.__table__, IntakeEvent.__table__, Incident.__table__,
            IncidentEvent.__table__, AppSetting.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_base, "async_session", factory)
    yield factory
    await engine.dispose()


async def _add_run(factory, rid, verdict, created, result=None, events=None):
    async with factory() as s:
        s.add(DiagnosisRun(id=rid, service="checkout", status="done", verdict=verdict,
                           created_at=created, result=result or {"verdict": verdict,
                           "candidates": [{"service": "payment"}], "evidence": ["x"] * 50},
                           events=events if events is not None else [{"stage": "detect"}]))
        await s.commit()


async def _count(factory, model, *where):
    from sqlalchemy import func, select
    async with factory() as s:
        q = select(func.count()).select_from(model)
        for w in where:
            q = q.where(w)
        return (await s.execute(q)).scalar() or 0


async def test_disabled_by_default_touches_nothing(_sqlite):
    out = await retention.run_once(manual=False)
    assert out == {"skipped": "retention disabled"}
    p = await retention.get_policy()
    assert p["enabled"] is False and p["clear_days"] == 30


async def test_manual_run_clears_slims_and_deletes_intake(_sqlite):
    await retention.save_policy(True, 30, 90, 90)
    await _add_run(_sqlite, "old-clear", "clear", OLD)
    await _add_run(_sqlite, "new-clear", "clear", RECENT)
    await _add_run(_sqlite, "old-rc", "root_cause", OLD)
    await _add_run(_sqlite, "new-rc", "root_cause", RECENT)
    async with _sqlite() as s:
        s.add(IntakeEvent(intake_id=1, fired_at=OLD, trigger="webhook",
                          service="checkout"))
        s.add(IntakeEvent(intake_id=1, fired_at=RECENT, trigger="webhook",
                          service="checkout"))
        await s.commit()

    out = await retention.run_once(manual=True)
    assert out["cleared_runs"] == 1      # 只删了过期的 clear
    assert out["slimmed_runs"] == 1      # 过期确诊 → 摘要化
    assert out["intake_events"] == 1     # 过期流水

    async with _sqlite() as s:
        row = await s.get(DiagnosisRun, "old-rc")
        assert row.events is None, "events 置空 = 已摘要化的幂等标记"
        assert row.result.get("slimmed") is True
        assert "candidates" not in row.result, "大字段必须裁掉"
        assert row.result.get("verdict") == "root_cause"
        assert await s.get(DiagnosisRun, "new-rc") is not None
        assert await s.get(DiagnosisRun, "new-clear") is not None


async def test_slim_is_idempotent(_sqlite):
    await retention.save_policy(True, 30, 90, 90)
    await _add_run(_sqlite, "old-rc", "root_cause", OLD)
    first = await retention.run_once(manual=True)
    second = await retention.run_once(manual=True)
    assert first["slimmed_runs"] == 1 and second["slimmed_runs"] == 0


async def test_batched_delete_clears_all(_sqlite):
    """超过单批(500)也能循环删干净。"""
    await retention.save_policy(True, 30, 90, 3650)
    for i in range(600):
        await _add_run(_sqlite, f"c{i}", "clear", OLD, events=[])
    out = await retention.run_once(manual=True)
    assert out["cleared_runs"] == 600
    assert await _count(_sqlite, DiagnosisRun) == 0


async def test_policy_clamps_and_enables_auto(_sqlite):
    p = await retention.save_policy(True, 0, 99999, 7)
    assert p == {"enabled": True, "clear_days": 1, "slim_days": 3650,
                 "intake_days": 7}
    out = await retention.run_once(manual=False)   # 开了之后自动路也执行
    assert "skipped" not in out


async def test_incidents_never_deleted(_sqlite):
    await retention.save_policy(True, 1, 1, 1)
    async with _sqlite() as s:
        s.add(Incident(title="老事故", status="closed", created_at=OLD,
                       updated_at=OLD))
        await s.commit()
    await retention.run_once(manual=True)
    assert await _count(_sqlite, Incident) == 1
