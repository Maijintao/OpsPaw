"""后台作业注册表与 API 的安全边界。"""
import pathlib

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api import jobs as jobs_api
from app.core import jobs


@pytest.fixture
def app():
    test_app = FastAPI()
    test_app.include_router(jobs_api.router)
    return test_app


@pytest.fixture
async def client(app):
    async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


async def test_unknown_job_is_rejected(client):
    for method, path in (
        ("get", "/api/jobs/rm -rf"),
        ("post", "/api/jobs/rm -rf"),
        ("post", "/api/jobs/未注册/stop"),
    ):
        response = await getattr(client, method)(path)
        assert response.status_code == 404


async def test_running_job_refuses_reentry(client, monkeypatch):
    monkeypatch.setitem(
        jobs._RUNS, "kb-build", jobs.JobRun(status="running"))
    response = await client.post("/api/jobs/kb-build")
    assert response.status_code == 409
    assert "正在运行" in response.json()["detail"]


async def test_status_covers_registry(client):
    body = (await client.get("/api/jobs")).json()
    assert {item["name"] for item in body["jobs"]} == set(jobs.JOBS)


def test_registered_python_scripts_exist():
    root = pathlib.Path(__file__).resolve().parents[1]
    missing = []
    for spec in jobs.JOBS.values():
        if len(spec.argv) >= 2 and spec.argv[1].startswith("scripts/"):
            if not (root / spec.argv[1]).is_file():
                missing.append(spec.argv[1])
    assert not missing, missing
