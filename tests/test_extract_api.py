from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda

from app.api import extract as extract_api
from app.main import app
from app.schemas.extract import OpsTicket


def override(runnable):
    app.dependency_overrides[extract_api.get_extractor] = lambda: runnable
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_extract_returns_structured_json():
    ticket = OpsTicket(
        service_id="1001", request_type="重启", expected_action="重启服务 1001 释放内存"
    )
    client = override(RunnableLambda(lambda _: ticket))
    resp = client.post("/api/extract", json={"text": "服务 1001 内存爆了,帮我重启"})
    assert resp.status_code == 200
    assert resp.json() == {
        "service_id": "1001",
        "request_type": "重启",
        "expected_action": "重启服务 1001 释放内存",
    }


def test_extract_upstream_failure_returns_502():
    def boom(_):
        raise RuntimeError("upstream down")

    client = override(RunnableLambda(boom))
    resp = client.post("/api/extract", json={"text": "随便说点什么"})
    assert resp.status_code == 502
    assert "detail" in resp.json()


def test_extract_validates_empty_text():
    client = override(RunnableLambda(lambda _: None))
    assert client.post("/api/extract", json={"text": ""}).status_code == 422
