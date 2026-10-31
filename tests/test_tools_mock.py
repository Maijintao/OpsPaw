"""services 内置工具(真实服务数据层):存在性校验、快照透传、目录关键词过滤。"""
import pytest

from app.tools.builtin.services import NOT_FOUND, query_catalog, query_service

SERVICES = [
    {"service_id": "cartservice", "name": "cartservice", "target": "otel-demo", "status": None},
    {"service_id": "frontend", "name": "frontend", "target": "otel-demo", "status": None},
]


async def _stub_discover(monkeypatch, services=SERVICES, error=None):
    # services.py 是 from-import,patch 必须打在它自己的命名空间上;
    # service_catalog.owns_service 内部走 service_catalog 模块全局,源处一并 patch 才同步生效
    async def fake():
        return services, error
    monkeypatch.setattr("app.tools.builtin.services.discover_services", fake)
    monkeypatch.setattr("app.tools.service_catalog.discover_services", fake)


@pytest.mark.asyncio
async def test_query_service_not_found(monkeypatch):
    # 存在性校验:平台里没有的服务,如实报 NOT_FOUND,不编快照
    await _stub_discover(monkeypatch)
    out = await query_service.ainvoke({"service_name": "ghostservice", "user_id": "u1"})
    assert out == NOT_FOUND


@pytest.mark.asyncio
async def test_query_service_passes_through_snapshot(monkeypatch):
    await _stub_discover(monkeypatch)
    snap = {"service_id": "cartservice", "status": "运行中", "reason": "指标与链路抽样均未见异常"}

    async def fake_snapshot(name):
        assert name == "cartservice"
        return snap
    # services.py 是 from-import,patch 要打在它的命名空间上
    monkeypatch.setattr("app.tools.builtin.services.service_snapshot", fake_snapshot)

    out = await query_service.ainvoke({"service_name": "cartservice", "user_id": "u1"})
    assert out["service_id"] == "cartservice" and out["status"] == "运行中"


@pytest.mark.asyncio
async def test_query_service_discovery_down(monkeypatch):
    await _stub_discover(monkeypatch, error="接入平台均不可达:otel-demo(ConnectError)")
    out = await query_service.ainvoke({"service_name": "cartservice", "user_id": "u1"})
    assert out == NOT_FOUND     # 不可达与查无此服务回同一句话,不留枚举 oracle


@pytest.mark.asyncio
async def test_query_catalog_filters_by_keyword(monkeypatch):
    await _stub_discover(monkeypatch)
    out = await query_catalog.ainvoke({"keyword": "cart"})
    assert out["matched"] == 1
    assert out["services"][0]["service_id"] == "cartservice"
    assert "status" not in out["services"][0]   # 目录不带过期状态


@pytest.mark.asyncio
async def test_query_catalog_no_match_returns_full(monkeypatch):
    # 没匹配到关键词 → 亮全量目录,让模型自己认(真实清单规模有限,全给不超载)
    await _stub_discover(monkeypatch)
    out = await query_catalog.ainvoke({"keyword": "不存在的关键词xyz"})
    assert out["matched"] == 2


@pytest.mark.asyncio
async def test_query_catalog_discovery_down(monkeypatch):
    await _stub_discover(monkeypatch, error="接入平台均不可达:otel-demo(ConnectError)")
    out = await query_catalog.ainvoke({"keyword": "cart"})
    assert out["code"] == "discovery_down"
