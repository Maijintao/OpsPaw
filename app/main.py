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


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await runtime.init_graph()
    registry.scan_builtin()
    from app.api.targets import heal_loop
    healing = asyncio.create_task(heal_loop())
    try:
        yield
    finally:
        healing.cancel()
        with suppress(asyncio.CancelledError):
            await healing
        await runtime.close_graph()
        await engine.dispose()


app = FastAPI(title="OpsPaw", version="0.1.0", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(agent_router)
app.include_router(conversations_router)
app.include_router(tools_router)
app.include_router(actions_router)

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
