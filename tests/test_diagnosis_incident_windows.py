"""基线洗刷的窗口筛选:只剔"与历史区间重叠"的故障窗,且不被时区错位带偏。

## 为什么单测这个(2026-10-17 实测挖出来的真 bug)

`recent_incident_windows` 是基线洗刷的入口:把已确诊/已登记的故障时段从 24 小时历史
分布里剔除,否则同款故障第二次发生时,当前值落回被污染的"正常范围",粗筛失明。

旧实现用 `created_at`(行创建时间)当边界筛窗口。但 `created_at` 走
`server_default=func.now()`——sqlite 上是 UTC,MySQL 上是会话时区,而窗口本身是本地
epoch。两者差 8 小时(本机实测),于是:

  - 回放把时钟拨到 14:42,却剔掉了 21:59 才发生的故障窗(**未来**的窗);
  - 同时漏掉了 24 小时历史最早那 8 小时里的故障窗。

两个方向都直接改历史分布。实测影响:ad 的 CPU 打到自身历史中位的 58 倍,粗筛却判
"正常";修正后 12 份真实故障录制里锚点第一名从 2/12 提到 4/12,ad 三组从"完全看不见"
变成能定位。

判据本身没动——只把"用创建时间筛窗口"换成"用窗口自身的 epoch 判重叠",与时区无关。
"""
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import base as db_base
from app.db import repository
from app.db.models import Base, DiagnosisRun


@pytest_asyncio.fixture()
async def _sqlite_repo(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[DiagnosisRun.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_base, "async_session", factory)
    yield factory
    await engine.dispose()


def _row(rid: str, created_at: datetime, win: tuple[datetime, datetime] | None,
         verdict: str = "root_cause") -> DiagnosisRun:
    result = {}
    if win:
        result = {"data_quality": {"trace": {"window_start": win[0].timestamp(),
                                             "window_end": win[1].timestamp()}}}
    return DiagnosisRun(id=rid, service="s", symptom="t", status="done",
                        verdict=verdict, result=result, created_at=created_at)


async def test_windows_filtered_by_overlap_not_creation_time(_sqlite_repo):
    """窗口落在查询区间内就剔,落在区间外(尤其是"未来")就不剔。"""
    until = datetime(2026, 10, 16, 14, 42)
    since = until - timedelta(hours=24)

    async with _sqlite_repo() as s:
        # A) 真故障:窗在区间内。created_at 故意比窗口早 8 小时(模拟 UTC 落库)
        s.add(_row("A", datetime(2026, 10, 16, 5, 0),
                   (datetime(2026, 10, 16, 13, 0), datetime(2026, 10, 16, 13, 10))))
        # B) 未来窗:窗口在诊断时刻之后 6 小时(created_at 也早 8 小时 → 旧实现会误剔)
        s.add(_row("B", datetime(2026, 10, 16, 12, 0),
                   (datetime(2026, 10, 16, 20, 0), datetime(2026, 10, 16, 20, 10))))
        # C) 太老的窗:整个在 24 小时历史之外
        s.add(_row("C", datetime(2026, 10, 14, 2, 0),
                   (datetime(2026, 10, 14, 10, 0), datetime(2026, 10, 14, 10, 10))))
        # D) 区间内但没确诊:不是故障证据,不该剔
        s.add(_row("D", datetime(2026, 10, 16, 6, 0),
                   (datetime(2026, 10, 16, 14, 0), datetime(2026, 10, 16, 14, 5)),
                   verdict="inconclusive"))
        await s.commit()

    wins = await repository.recent_incident_windows(since, until)

    assert len(wins) == 1, f"只应剔 A 一个窗,实际剔了 {len(wins)} 个:{wins}"
    ws, we = wins[0]
    assert ws == datetime(2026, 10, 16, 13, 0).timestamp()
    assert we == datetime(2026, 10, 16, 13, 10).timestamp()


async def test_windows_never_outside_requested_range(_sqlite_repo):
    """回归断言:返回的窗必须与 [since, until] 真的重叠——时区再错也不许越界。"""
    until = datetime(2026, 10, 16, 14, 42)
    since = until - timedelta(hours=24)
    async with _sqlite_repo() as s:
        for i, hour in enumerate((1, 6, 13, 20, 23)):
            s.add(_row(f"r{i}", datetime(2026, 10, 16, max(hour - 8, 0), 0),
                       (datetime(2026, 10, 16, hour, 0), datetime(2026, 10, 16, hour, 5))))
        await s.commit()

    wins = await repository.recent_incident_windows(since, until)
    lo, hi = since.timestamp(), until.timestamp()
    assert all(we > lo and ws < hi for ws, we in wins), f"有窗越界:{wins}"
