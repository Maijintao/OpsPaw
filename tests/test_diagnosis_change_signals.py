"""窗内变更信号(recent_change_signals)的单测:sqlite 内存库,不依赖 MySQL 测试环境。

变更信号是归因的第 5 条"免费数据"通道——产品自己的 tickets(变更工单)与
tool_audit_logs(submit_change 审计)里"有人动过这个服务"的痕迹,之前从未进过诊断管线。
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import base as db_base
from app.db import repository
from app.db.models import Base, Ticket, ToolAuditLog
from datetime import datetime


@pytest_asyncio.fixture()
async def _sqlite_repo(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Ticket.__table__, ToolAuditLog.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_base, "async_session", factory)
    yield factory
    await engine.dispose()


async def test_change_signals_match_ticket_and_audit(_sqlite_repo):
    async with _sqlite_repo() as s:
        s.add(Ticket(ticket_no="T1", conversation_id=1,
                     description="对 cart 服务执行滚动重启,变更窗口 10 分钟",
                     ticket_type="变更", status="已处理"))
        s.add(Ticket(ticket_no="T2", conversation_id=1,
                     description="对 frontend 服务扩容", ticket_type="变更", status="待处理"))
        s.add(Ticket(ticket_no="T3", conversation_id=1,
                     description="账单咨询", ticket_type="咨询", status="待处理"))
        s.add(ToolAuditLog(tool_name="submit_change", tool_source="builtin",
                           arguments={"service_id": "cart"}, status="成功"))
        s.add(ToolAuditLog(tool_name="submit_change", tool_source="builtin",
                           arguments={"service_id": "other"}, status="校验拦下"))
        await s.commit()
    since = datetime(2000, 1, 1)
    out = await repository.recent_change_signals("cart", since, datetime(2100, 1, 1))
    kinds = {x["kind"] for x in out}
    assert kinds == {"change_ticket", "change_request"}
    assert all("cart" in x["detail"].lower() or x["kind"] == "change_request" for x in out)


async def test_change_signals_unrelated_service_empty(_sqlite_repo):
    async with _sqlite_repo() as s:
        s.add(Ticket(ticket_no="T1", conversation_id=1, description="frontend 扩容",
                     ticket_type="变更", status="待处理"))
        await s.commit()
    out = await repository.recent_change_signals("cart", datetime(2000, 1, 1),
                                                 datetime(2100, 1, 1))
    assert out == []
