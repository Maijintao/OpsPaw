import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.api import targets

@pytest.mark.asyncio
async def test_platform_crud(db_clean, monkeypatch):
    async def health(row):
        return [{**src, "online": True, "detail": "test"} for src in row.sources]
    if hasattr(targets, "probe_target"):
        monkeypatch.setattr(targets, "probe_target", health)
    if hasattr(targets, "_capture_fp"):
        async def capture(sources): return sources
        monkeypatch.setattr(targets, "_capture_fp", capture)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"name": "platform-a", "cluster": "cluster-a", "sources": [{"type": "jaeger", "url": "http://jaeger.test/api/traces"}]}
        response = await client.post("/api/targets", json=body)
        assert response.status_code == 200
        tid = response.json()["id"]
        assert response.json()["sources"][0]["url"] == "http://jaeger.test"
        assert (await client.get("/api/targets")).json()["targets"][0]["cluster"] == "cluster-a"
        body["name"] = "platform-renamed"
        assert (await client.put(f"/api/targets/{tid}", json=body)).json()["name"] == "platform-renamed"
        assert (await client.delete(f"/api/targets/{tid}")).status_code == 200
        assert (await client.delete(f"/api/targets/{tid}")).status_code == 404


@pytest.mark.asyncio
async def test_probe_validates_data_and_platform_identity(monkeypatch):
    import httpx
    async def get(url, path, params=None, headers=None):
        payload = {"data": ["shared-service"]} if "services" in path else {"status": "success", "data": {"result": []}}
        return httpx.Response(200, json=payload)
    monkeypatch.setattr(targets, "_get", get)
    assert (await targets._probe_jaeger("http://jaeger.test"))["online"] is True
    assert (await targets._probe_prometheus("http://prom.test"))["online"] is True
    async def invalid(*a, **kw): return httpx.Response(200, text="not json")
    monkeypatch.setattr(targets, "_get", invalid)
    assert (await targets._probe_jaeger("http://jaeger.test"))["online"] is False
