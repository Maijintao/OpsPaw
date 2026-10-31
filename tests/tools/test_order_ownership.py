"""服务校验:工具只认「接入平台里真实存在的服务」,不认模型说了什么。

为什么要有这组测试:query_service 早先只收 service_id,报个号就把数据念给用户听,
这是最经典的越权访问(IDOR)。身份不能让模型填——用户自称是谁,模型就按谁处理——
所以 user_id 走执行引擎注入,工具内部先过 owns_service 校验再查数据。

真实语义说明:mock 时代的值班清单(list_user_services/DEMO_SERVICE_IDS)已下线,
现在没有 CMDB 数据源,owns_service 语义退化为「服务在接入平台中存在」的存在性校验
(user_id 参数保留兼容调用方,接真实 CMDB 后升级回归属校验)。「别人的服务」和
「不存在的服务」回同一句话(反枚举:两种情况不可区分)。
"""
import pytest

from app.tools import service_catalog
from app.tools.builtin.services import query_service
from app.tools.builtin.changes import submit_change

SERVICES = [
    {"service_id": "cartservice", "name": "cartservice", "target": "otel-demo", "status": None},
    {"service_id": "frontend", "name": "frontend", "target": "otel-demo", "status": None},
]


async def _stub_discover(monkeypatch, services=SERVICES, error=None):
    async def fake():
        return services, error
    monkeypatch.setattr("app.tools.service_catalog.discover_services", fake)


async def test_owns_service_存在即放行(monkeypatch):
    await _stub_discover(monkeypatch)
    assert await service_catalog.owns_service("u-alice", "cartservice") is True
    assert await service_catalog.owns_service("u-alice", "ghostservice") is False


async def test_owns_service_空身份一律不放行(monkeypatch):
    # user_id 没注入到(空串)时不能当成「谁都行」,否则漏一个调用点就等于没做校验
    await _stub_discover(monkeypatch)
    assert await service_catalog.owns_service("", "cartservice") is False


async def test_查自己的服务照常返回(monkeypatch):
    await _stub_discover(monkeypatch)

    async def fake_snapshot(name, discovered=None):
        return {"service_id": name, "status": "运行中", "reason": "指标与链路抽样均未见异常",
                "cpu_usage_cores": 0.4, "mem_usage_bytes": 1024}
    monkeypatch.setattr("app.tools.builtin.services.service_snapshot", fake_snapshot)
    out = await query_service.ainvoke({"service_name": "cartservice", "user_id": "u-alice"})
    assert out["service_id"] == "cartservice"
    assert "cpu_usage_cores" in out       # 自己平台里的服务该给的字段一样不少


async def test_查不存在的服务被拒且不泄露任何字段(monkeypatch):
    await _stub_discover(monkeypatch)
    out = await query_service.ainvoke({"service_name": "ghostservice", "user_id": "u-alice"})
    assert out.get("code") == "service_not_found"
    # 版本、实例数、资源水位、负责团队一个都不能漏出去(能顺着快照摸到部署信息)
    for leaked in ("version", "instances", "cpu_usage_cores", "mem_usage_bytes",
                   "last_deploy", "owner_team", "status", "name", "reason"):
        assert leaked not in out


async def test_不可达和不存在回同一句话(monkeypatch):
    # 两种情况回不同的话就成了枚举 oracle:攻击者靠回答差异就能挨个试出哪些服务真实存在
    # (平台不可达时 owns_service=False → NOT_FOUND;平台正常但查无此服务 → 同一个 NOT_FOUND)
    await _stub_discover(monkeypatch, error="接入平台均不可达:otel-demo(ConnectError)")
    down = await query_service.ainvoke({"service_name": "ghostservice", "user_id": "u-alice"})
    await _stub_discover(monkeypatch)
    ghost = await query_service.ainvoke({"service_name": "ghostservice", "user_id": "u-alice"})
    assert down["error"] == ghost["error"]
    assert down["code"] == ghost["code"] == "service_not_found"


async def test_变更也过同一道校验(monkeypatch):
    # 写操作有二次确认门,但确认的是「要不要变更」,不是「服务存不存在」
    await _stub_discover(monkeypatch)
    out = await submit_change.ainvoke(
        {"service_id": "ghostservice", "change_type": "重启服务", "user_id": "u-alice"})
    assert out.get("code") == "service_not_found"


@pytest.mark.parametrize("bad", ["", None])
async def test_身份缺失时读写都拒(monkeypatch, bad):
    await _stub_discover(monkeypatch)
    out = await query_service.ainvoke({"service_name": "cartservice", "user_id": bad or ""})
    assert out.get("code") == "service_not_found"
