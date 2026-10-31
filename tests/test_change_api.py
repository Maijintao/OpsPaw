def test_create_change_writes_ticket(client, monkeypatch):
    from types import SimpleNamespace
    async def fake_create(cid, desc, ttype):
        assert cid == 7 and ttype == "变更" \
            and "cartservice" in desc and "重启服务" in desc
        return "T20260716001"
    from app.api import actions
    async def fake_conversation(cid):
        return SimpleNamespace(user_id="u1")
    monkeypatch.setattr(actions.repository, "get_conversation", fake_conversation)

    async def fake_owns(uid, sid):
        return uid == "u1" and sid == "cartservice"
    monkeypatch.setattr(actions, "owns_service", fake_owns)
    monkeypatch.setattr(actions.repository, "create_ticket", fake_create)
    r = client.post("/api/actions/create-change", json={
        "conversation_id": 7, "service_id": "cartservice", "change_type": "重启服务"})
    assert r.status_code == 200
    assert r.json()["ticket_no"] == "T20260716001"
    assert r.json()["status"] == "变更申请已提交"


def test_create_change_rejects_bad_change_type(client):
    r = client.post("/api/actions/create-change", json={
        "conversation_id": 7, "service_id": "cartservice", "change_type": "乱填"})
    assert r.status_code == 422


def test_create_change_rejects_unowned_service(client, monkeypatch):
    from types import SimpleNamespace
    from app.api import actions

    async def fake_conversation(cid):
        return SimpleNamespace(user_id="u1")
    monkeypatch.setattr(actions.repository, "get_conversation", fake_conversation)

    async def fake_owns(uid, sid):
        return False
    monkeypatch.setattr(actions, "owns_service", fake_owns)
    r = client.post("/api/actions/create-change", json={
        "conversation_id": 7, "service_id": "ghostservice", "change_type": "重启服务"})
    assert r.status_code == 403


def test_resume_endpoint_streams(client, monkeypatch):
    async def fake_stream_resume(cid, resume_value):
        assert cid == 3 and resume_value == "1001"
        yield {"type": "delta", "text": "这一单可以变更"}
        yield {"type": "actions", "items": [{"type": "change_form", "draft": {"service_id": "1001"}}]}
        yield {"type": "done", "conversation_id": 3}
    from app.api import actions
    monkeypatch.setattr(actions.runtime, "stream_resume", fake_stream_resume)
    r = client.post("/api/actions/resume", json={"conversation_id": 3, "service_id": "1001"})
    assert r.status_code == 200
    body = r.text
    assert "这一单可以变更" in body
    assert "change_form" in body and "[DONE]" in body


def test_resume_endpoint_confirmed_branch(client, monkeypatch):
    """ch08:工单预览卡确认/取消走 confirmed 字段,resume 值为 {"confirmed": bool}。"""
    async def fake_stream_resume(cid, resume_value):
        assert cid == 3 and resume_value == {"confirmed": False}
        yield {"type": "delta", "text": "已取消建单"}
        yield {"type": "done", "conversation_id": 3}
    from app.api import actions
    monkeypatch.setattr(actions.runtime, "stream_resume", fake_stream_resume)
    r = client.post("/api/actions/resume", json={"conversation_id": 3, "confirmed": False})
    assert r.status_code == 200 and "已取消建单" in r.text


def test_resume_endpoint_requires_one_of(client):
    """service_id 与 confirmed 都不传 → 400,不进图。"""
    r = client.post("/api/actions/resume", json={"conversation_id": 3})
    assert r.status_code == 400
