"""修复层的纯逻辑回归:动作目录、计划 hash、预检查规则、令牌格式。

不碰数据库、不碰网络——状态机里最容易改坏又最不该靠人肉点页面验证的就是这几条。
"""
import pytest

from app.repair import catalog, enrollment, plans


def test_catalog_is_fixed_and_all_need_approval():
    from app.repair import policy
    acts = catalog.view()
    by_action = {a["action"]: a for a in acts}
    assert {a for a in by_action if not by_action[a]["read_only"]} == {
        "restart_service", "start_service", "stop_service", "recreate_service",
        "set_flag", "restore_flags", "truncate_service_logs"}
    assert {a for a in by_action if by_action[a]["read_only"]} == {
        "list_services", "inspect_service", "service_logs",
        "service_events", "service_resources",
        "list_flags", "get_flag", "service_connections"}
    assert all(by_action[a]["requires_approval"] for a in catalog.ACTIONS)
    assert not any(by_action[a]["requires_approval"] for a in catalog.READ_ACTIONS)
    # 参数键全部在已知规范内(值校验在 _normalize_params),容器类动作必须含 service
    valid_keys = {"service", "flag", "variant", "target_host", "target_port",
                  "tail", "limit"}
    assert all(set(by_action[a]["params"]) <= valid_keys for a in catalog.ACTIONS)
    assert all("service" in by_action[a]["params"]
               for a in ("restart_service", "start_service", "stop_service",
                         "recreate_service", "truncate_service_logs"))
    assert by_action["set_flag"]["params"] == ["flag", "variant"]
    assert by_action["restore_flags"]["params"] == []
    assert "service" not in by_action["set_flag"]["params"]

    ok, why = policy.evaluate("restart_service", "root_cause", "pod_failure",
                              "high", "present")
    assert ok and not why
    # set_flag 可自动(写前记旧值可回切),症状复检 skipped 也放行
    ok, why = policy.evaluate("set_flag", "root_cause", "code_error",
                              "high", "skipped")
    assert ok and not why
    blocked = [
        ("stop_service", "root_cause", "pod_failure", "high", "present"),
        ("recreate_service", "root_cause", "pod_failure", "high", "present"),
        ("restart_service", "inconclusive", "pod_failure", "high", "present"),
        ("restart_service", "root_cause", "network_delay", "high", "present"),
        ("restart_service", "root_cause", "pod_failure", "low", "present"),
        ("restart_service", "root_cause", "pod_failure", "high", "unknown"),
    ]
    for action, verdict, ft, tier, sym in blocked:
        ok, why = policy.evaluate(action, verdict, ft, tier, sym)
        assert not ok and why


def test_recreate_service_is_never_auto_executed():
    """重建动作永远不走自动车道:即使其他条件全最优,也必须人工确认。"""
    from app.repair import policy
    assert "recreate_service" not in policy.AUTO_ACTIONS
    ok, why = policy.evaluate("recreate_service", "root_cause", "pod_failure",
                              "high", "present")
    assert not ok
    assert any("白名单" in r for r in why)


def test_plan_hash_is_stable_and_covers_every_field():
    base = plans._hash(1, "rc_x", "restart_service", "ad", {"service": "ad"})
    assert base == plans._hash(1, "rc_x", "restart_service", "ad", {"service": "ad"})
    changed = [
        plans._hash(2, "rc_x", "restart_service", "ad", {"service": "ad"}),
        plans._hash(1, "rc_y", "restart_service", "ad", {"service": "ad"}),
        plans._hash(1, "rc_x", "stop_service", "ad", {"service": "ad"}),
        plans._hash(1, "rc_x", "restart_service", "cart", {"service": "cart"}),
        plans._hash(1, "rc_x", "restart_service", "ad", {"service": "ad", "x": 1}),
    ]
    assert all(h != base for h in changed)


def test_preflight_rules():
    with pytest.raises(plans.PlanError):
        plans._preflight_check("restart_service", {"running": False})
    with pytest.raises(plans.PlanError):
        plans._preflight_check("stop_service", {"running": False})
    with pytest.raises(plans.PlanError):
        plans._preflight_check("start_service", {"running": True})
    plans._preflight_check("restart_service", {"running": True})
    plans._preflight_check("start_service", {"running": False})


def test_enrollment_token_shape_is_strict():
    for bad in ("", "abc", "rct_only", "xxx_abc_def", "rct__secret"):
        with pytest.raises(ValueError):
            enrollment._parse(bad)
    token_id, secret = enrollment._parse("rct_abc123_def456")
    assert token_id == "abc123" and secret == "def456"


class _Target:
    def __init__(self, sources):
        self.sources = sources


async def test_symptom_check_present(monkeypatch):
    from app.repair import symptom

    async def _target(_tid):
        return _Target([{"type": "prometheus", "url": "http://prom"}])

    async def _profiles(prom, services=None, hours=24):
        return {"ad": {"abnormal_dims": ["error"], "err_rate": 0.3, "base_err_rate": 0.0}}

    monkeypatch.setattr(symptom.repository, "get_target", _target)
    monkeypatch.setattr(symptom.detect, "service_profiles", _profiles)
    r = await symptom.check(1, "ad")
    assert r["status"] == "present" and r["dims"] == ["error"]


async def test_symptom_check_resolved(monkeypatch):
    from app.repair import symptom

    async def _target(_tid):
        return _Target([{"type": "prometheus", "url": "http://prom"}])

    async def _profiles(prom, services=None, hours=24):
        return {"ad": {"abnormal_dims": [], "err_rate": 0.0, "base_err_rate": 0.0}}

    monkeypatch.setattr(symptom.repository, "get_target", _target)
    monkeypatch.setattr(symptom.detect, "service_profiles", _profiles)
    assert (await symptom.check(1, "ad"))["status"] == "resolved"


async def test_symptom_check_unknown_without_prometheus(monkeypatch):
    from app.repair import symptom

    async def _target(_tid):
        return _Target([{"type": "jaeger", "url": "http://j"}])

    monkeypatch.setattr(symptom.repository, "get_target", _target)
    assert (await symptom.check(1, "ad"))["status"] == "unknown"


class _Plan:
    id = "pl_test"
    target_id = 1
    connector_id = "rc_test"
    action = "restart_service"
    service = "ad"
    status = "approved"
    params = {"service": "ad"}
    conversation_id = 1
    diagnosis_id = None
    reason = ""
    risk = "中"
    created_at = None
    plan_hash = plans._hash(1, "rc_test", "restart_service", "ad", {"service": "ad"})
    from datetime import datetime as _dt, timedelta as _td
    expires_at = _dt.now() + _td(minutes=10)


class _Approval:
    plan_id = "pl_test"
    status = "active"
    plan_hash = _Plan.plan_hash
    from datetime import datetime as _dt, timedelta as _td
    expires_at = _dt.now() + _td(minutes=10)


async def test_execute_stops_when_symptom_resolved(monkeypatch):
    """症状已消失时,执行入口必须直接收手——一次连接器调用都不能发出去。"""
    from app.repair import gateway, symptom

    calls = {"status": [], "audit": []}

    async def _plan(_pid):
        return _Plan()

    async def _approval(_aid):
        return _Approval()

    async def _status(_pid, st):
        calls["status"].append(st)

    async def _audit(event, **kw):
        calls["audit"].append(event)

    async def _symptom(_tid, _svc):
        return {"status": "resolved", "reason": "症状已消失"}

    async def _no_call(*a, **k):
        raise AssertionError("症状已消失时不该调用连接器")

    monkeypatch.setattr(plans.repository, "get_repair_plan", _plan)
    monkeypatch.setattr(plans.repository, "get_repair_approval", _approval)
    monkeypatch.setattr(plans.repository, "set_repair_plan_status", _status)
    monkeypatch.setattr(plans.repository, "add_repair_audit", _audit)
    monkeypatch.setattr(plans.gateway, "is_online", lambda _cid: True)
    monkeypatch.setattr(plans.gateway, "call", _no_call)
    monkeypatch.setattr(symptom, "check", _symptom)

    r = await plans.execute("pl_test", "ap_test")
    assert r["status"] == "symptom_resolved"
    assert calls["status"] == ["symptom_resolved"]
    assert "repair_symptom_resolved" in calls["audit"]


async def test_resolve_auto_action_by_container_state(monkeypatch):
    """自动修复的动作由系统按容器现状定,不让用户挑:在跑的重启,停了的启动,
    不存在的如实报错。"""
    from app.repair import plans

    async def running(cid, action, params, timeout=15, **kw):
        assert action == "inspect_service"
        return {"ok": True, "data": {"found": True, "running": True}}

    monkeypatch.setattr(plans.gateway, "call", running)
    assert (await plans.resolve_auto_action("rc_x", "ad"))[0] == "restart_service"

    async def stopped(cid, action, params, timeout=15, **kw):
        return {"ok": True, "data": {"found": True, "running": False}}

    monkeypatch.setattr(plans.gateway, "call", stopped)
    assert (await plans.resolve_auto_action("rc_x", "ad"))[0] == "start_service"

    async def gone(cid, action, params, timeout=15, **kw):
        return {"ok": True, "data": {"found": False, "running": False}}

    monkeypatch.setattr(plans.gateway, "call", gone)
    with pytest.raises(plans.PlanError):
        await plans.resolve_auto_action("rc_x", "ad")


async def test_symptom_no_data_means_unknown(monkeypatch):
    """服务停机/零流量时画像全空,分位判据"无异常维"不能被当成"症状已消失"。"""
    from app.repair import symptom

    async def _target(_tid):
        return _Target([{"type": "prometheus", "url": "http://prom"}])

    async def _profiles(prom, services=None, hours=24):
        return {"cart": {"calls": 0, "err_rate": None, "p95_ms": None,
                         "cpu": None, "mem": None, "abnormal_dims": []}}

    monkeypatch.setattr(symptom.repository, "get_target", _target)
    monkeypatch.setattr(symptom.detect, "service_profiles", _profiles)
    r = await symptom.check(1, "cart")
    assert r["status"] == "unknown" and "停机" in r["reason"]


async def test_auto_approve_mode_reads_settings(monkeypatch):
    from app.repair import plans

    async def auto_map():
        return {plans.APPROVAL_MODE_KEY: "auto"}

    monkeypatch.setattr(plans.repository, "get_settings_map", auto_map)
    assert await plans.auto_approve_enabled() is True

    async def manual_map():
        return {plans.APPROVAL_MODE_KEY: "manual"}

    monkeypatch.setattr(plans.repository, "get_settings_map", manual_map)
    assert await plans.auto_approve_enabled() is False

    async def bad_map():
        return {plans.APPROVAL_MODE_KEY: "garbage"}

    monkeypatch.setattr(plans.repository, "get_settings_map", bad_map)
    assert await plans.auto_approve_enabled() is False   # 坏配置当人工审批,不拦流程


class _Conn:
    id = "rc_x"
    target_id = 1
    status = "active"
    capabilities = [{"action": "restart_service"}, {"action": "stop_service"}]


async def test_auto_approve_mode_bypasses_policy(monkeypatch):
    """后台开了「自动通过」:策略门槛不满足也免审批直接执行;
    白名单外的动作(stop)不享受这个待遇。"""
    from app.repair import gateway, plans, symptom

    async def _conn(_cid):
        return _Conn()

    async def _noop(*a, **k):
        return None

    async def _get_plan(_pid):
        return _Plan()

    async def _sym(_tid, _svc):
        return {"status": "unknown", "reason": "无数据"}

    ran = []

    async def _fake_auto_run(_pid, _aid):
        ran.append(_pid)

    async def _approve(plan_id, approved_by="admin"):
        assert approved_by == "policy:auto-approve"
        return {"plan_id": plan_id, "approval_id": "ap_x"}

    async def _settings_map():
        return {plans.APPROVAL_MODE_KEY: "auto"}

    monkeypatch.setattr(plans.repository, "get_repair_connector", _conn)
    monkeypatch.setattr(plans.repository, "create_repair_plan", _noop)
    monkeypatch.setattr(plans.repository, "add_repair_audit", _noop)
    monkeypatch.setattr(plans.repository, "get_repair_plan", _get_plan)
    monkeypatch.setattr(plans.repository, "get_settings_map", _settings_map)
    monkeypatch.setattr(plans.gateway, "is_online", lambda _cid: True)
    monkeypatch.setattr(symptom, "check", _sym)
    monkeypatch.setattr(plans, "approve", _approve)
    monkeypatch.setattr(plans, "_auto_run", _fake_auto_run)

    # 策略门槛本来不满足(verdict 不是根因),但开了自动通过 → 免审批执行
    r = await plans.create_auto(1, "rc_x", "restart_service", "ad", "test",
                                verdict="inconclusive")
    assert r["auto_started"] is True and "自动通过" in r["auto_reason"]
    import asyncio
    await asyncio.sleep(0)          # 让后台自动执行任务真的跑一拍
    assert ran == ["pl_test"]

    # 白名单外的动作不享受自动通过
    r2 = await plans.create_auto(1, "rc_x", "stop_service", "ad", "test",
                                 verdict="root_cause")
    assert r2["auto_started"] is False
