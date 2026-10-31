"""Agent 只读查询工具 repair_read 的边界测试:不碰真实连接器,只验证校验与分发。

交接文档遗留问题:只读动作此前只有后台页面入口,Agent 没有工具可用。
本工具在服务端按动作目录收口参数、按能力交集放行动作——这些规则改坏了,
等于给模型开了绕过目录的口子,所以逐条卡住。
"""
from types import SimpleNamespace

from app.tools.builtin.repair import repair_read


class _Row:
    def __init__(self, rid, name, capabilities):
        self.id = rid
        self.name = name
        self.capabilities = capabilities
        self.status = "active"


def _caps(*actions):
    return [{"action": a, "read_only": a not in ("restart_service",)} for a in actions]


def _patch_world(monkeypatch, targets, connectors_by_target, online_ids):
    from app.tools.builtin import repair as mod

    async def fake_list_targets():
        return targets

    async def fake_list_connectors(target_id):
        return connectors_by_target.get(target_id, [])

    monkeypatch.setattr(mod.repository, "list_targets", fake_list_targets)
    monkeypatch.setattr(mod.repository, "list_repair_connectors", fake_list_connectors)
    monkeypatch.setattr(mod.gateway, "is_online", lambda cid: cid in online_ids)


async def test_写动作和目录外动作一律拒绝(monkeypatch):
    for action in ("restart_service", "recreate_service", "free_shell"):
        out = await repair_read.ainvoke({"action": action})
        assert out["ok"] is False and "只读" in out["error"], action


async def test_非法服务名与越界参数被拒(monkeypatch):
    out = await repair_read.ainvoke({"action": "service_logs", "service": "bad/name"})
    assert out["ok"] is False and "服务名" in out["error"]
    out = await repair_read.ainvoke({"action": "service_logs", "service": "ad", "tail": 500})
    assert out["ok"] is False and "tail" in out["error"]
    out = await repair_read.ainvoke({"action": "service_events", "service": "ad", "limit": 0})
    assert out["ok"] is False and "limit" in out["error"]


async def test_正常查询按目录收口参数(monkeypatch):
    conn = _Row("rc_1", "机器A", _caps("service_logs", "list_services"))
    _patch_world(monkeypatch, [SimpleNamespace(id=1, name="otel-demo", enabled=True)],
                 {1: [conn]}, online_ids={"rc_1"})
    seen = {}

    async def fake_call(cid, action, params, timeout=20, **kw):
        seen.update(connector=cid, action=action, params=params)
        return {"ok": True, "status": "success", "message": "ok", "data": {}}

    monkeypatch.setattr("app.tools.builtin.repair.gateway.call", fake_call)
    out = await repair_read.ainvoke(
        {"action": "service_logs", "service": "ad", "platform": "otel-demo", "tail": 50})
    assert out["ok"] is True
    assert seen["connector"] == "rc_1" and seen["action"] == "service_logs"
    assert seen["params"] == {"service": "ad", "tail": 50}  # 目录外参数一个不漏出去


async def test_list_services_不需要服务名(monkeypatch):
    conn = _Row("rc_1", "机器A", _caps("list_services"))
    _patch_world(monkeypatch, [SimpleNamespace(id=1, name="otel-demo", enabled=True)],
                 {1: [conn]}, online_ids={"rc_1"})

    async def fake_call(cid, action, params, timeout=20, **kw):
        assert params == {}
        return {"ok": True, "status": "success", "message": "ok", "data": {"services": []}}

    monkeypatch.setattr("app.tools.builtin.repair.gateway.call", fake_call)
    out = await repair_read.ainvoke({"action": "list_services"})
    assert out["ok"] is True


async def test_连接器未声明的动作被拒(monkeypatch):
    conn = _Row("rc_1", "机器A", _caps("list_services"))  # 没声明 service_logs
    _patch_world(monkeypatch, [SimpleNamespace(id=1, name="otel-demo", enabled=True)],
                 {1: [conn]}, online_ids={"rc_1"})
    out = await repair_read.ainvoke(
        {"action": "service_logs", "service": "ad", "platform": "otel-demo"})
    assert out["ok"] is False and "声明" in out["error"]


async def test_多连接器在线时不擅自选(monkeypatch):
    conns = [_Row("rc_1", "机器A", _caps("list_services")),
             _Row("rc_2", "机器B", _caps("list_services"))]
    _patch_world(monkeypatch, [SimpleNamespace(id=1, name="otel-demo", enabled=True)],
                 {1: conns}, online_ids={"rc_1", "rc_2"})
    out = await repair_read.ainvoke({"action": "list_services", "platform": "otel-demo"})
    assert out["ok"] is False and "后台" in out["error"]


async def test_唯一在线平台自动选定(monkeypatch):
    conn = _Row("rc_1", "机器A", _caps("list_services"))
    _patch_world(monkeypatch, [SimpleNamespace(id=1, name="otel-demo", enabled=True)],
                 {1: [conn]}, online_ids={"rc_1"})
    seen = {}

    async def fake_call(cid, action, params, timeout=20, **kw):
        seen["connector"] = cid
        return {"ok": True, "status": "success", "message": "ok", "data": {}}

    monkeypatch.setattr("app.tools.builtin.repair.gateway.call", fake_call)
    out = await repair_read.ainvoke({"action": "list_services"})
    assert out["ok"] is True and seen["connector"] == "rc_1"


async def test_连接器不在线给明确错误(monkeypatch):
    conn = _Row("rc_1", "机器A", _caps("list_services"))
    _patch_world(monkeypatch, [SimpleNamespace(id=1, name="otel-demo", enabled=True)],
                 {1: [conn]}, online_ids=set())  # 全部离线
    out = await repair_read.ainvoke({"action": "list_services"})
    assert out["ok"] is False and "不在线" in out["error"]
