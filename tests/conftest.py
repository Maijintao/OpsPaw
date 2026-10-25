"""本阶段测试使用独立 SQLite，不访问业务库或真实模型服务。"""
import os

os.environ.setdefault('CHAT_MODEL', 'test-model')
os.environ.setdefault('CHAT_BASE_URL', 'https://example.invalid/v1')
os.environ.setdefault('CHAT_API_KEY', 'test-key')

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.db import base, models


@pytest_asyncio.fixture
async def db_session_factory(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'test.sqlite'))
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(base, 'engine', engine)
    monkeypatch.setattr(base, 'async_session', factory)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_clean(db_session_factory):
    yield
