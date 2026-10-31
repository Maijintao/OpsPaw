import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.db.base import Base, engine
from app.db import models
from app.graph import runtime
from app.api.chat import router as chat_router
from app.api.agent import router as agent_router
from app.api.conversations import router as conversations_router
from app.api.tools import router as tools_router
from app.tools import registry
from app.api.actions import router as actions_router
from app.api.repair import router as repair_router
from app.api.sentinels import ensure_tables as ensure_sentinel_tables
from app.api.sentinels import start_scheduler as start_sentinel_scheduler
from app.api.sentinels import stop_scheduler as stop_sentinel_scheduler
from app.api.sentinels import router as sentinels_router
from app.api.incidents import router as incidents_router
from app.api.admin import router as admin_router
from app.api.jobs import router as jobs_router
from app.api.observability import router as observability_router
from app.api.settings import router as settings_router
from app.api.topics import router as topics_router
from app.api.kb import router as kb_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.db import repository
    await repository.ensure_targets_table()
    await repository.ensure_diagnosis_table()
    await repository.ensure_repair_tables()
    await repository.ensure_repair_runs_table()
    await ensure_sentinel_tables()
    await repository.ensure_incident_tables()
    await runtime.init_graph()
    registry.scan_builtin()
    start_sentinel_scheduler()
    from app.api.targets import heal_loop
    healing = asyncio.create_task(heal_loop())
    try:
        yield
    finally:
        healing.cancel()
        with suppress(asyncio.CancelledError):
            await healing
        await stop_sentinel_scheduler()
        await runtime.close_graph()
        await engine.dispose()


app = FastAPI(title="OpsPaw", version="0.1.0", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(agent_router)
app.include_router(conversations_router)
app.include_router(tools_router)
app.include_router(actions_router)
app.include_router(repair_router)
app.include_router(sentinels_router)
app.include_router(incidents_router)
app.include_router(admin_router)
app.include_router(jobs_router)
app.include_router(observability_router)
app.include_router(settings_router)
app.include_router(topics_router)
app.include_router(kb_router)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")

@app.get("/", include_in_schema=False)
async def chat_page():
    return FileResponse(_STATIC / "index.html")

@app.get("/tools", include_in_schema=False)
async def tools_page():
    return FileResponse(_STATIC / "tools.html")

from app.api.extract import router as extract_router
app.include_router(extract_router)


from app.api.targets import router as targets_router
app.include_router(targets_router)


from app.api.grafana import router as grafana_router
app.include_router(grafana_router)


@app.get("/platforms", include_in_schema=False)
async def platforms_page():
    return FileResponse(_STATIC / "platforms.html")


from app.api.diagnosis import router as diagnosis_router
app.include_router(diagnosis_router)


@app.get("/diagnosis", include_in_schema=False)
async def diagnosis_page():
    return FileResponse(_STATIC / "diagnosis.html")


@app.get("/repair/{repair_id}", include_in_schema=False)
async def repair_page(repair_id: str) -> FileResponse:
    return FileResponse(_STATIC / "repair.html")


@app.get("/incident/{iid}", include_in_schema=False)
async def incident_page(iid: int) -> FileResponse:
    return FileResponse(_STATIC / "incident.html")


@app.get("/alerts", include_in_schema=False)
async def alerts_page() -> FileResponse:
    return FileResponse(_STATIC / "alerts.html")


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(_STATIC / "admin.html")


@app.get("/observability", include_in_schema=False)
async def observability_page() -> FileResponse:
    return FileResponse(_STATIC / "observability.html")


@app.get("/topics", include_in_schema=False)
async def topics_page() -> FileResponse:
    return FileResponse(_STATIC / "topics.html")


@app.get("/kb", include_in_schema=False)
async def kb_page() -> FileResponse:
    return FileResponse(_STATIC / "kb.html")


@app.get("/incidents", include_in_schema=False)
async def incidents_page() -> FileResponse:
    return FileResponse(_STATIC / "incidents.html")
