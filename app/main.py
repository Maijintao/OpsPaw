from contextlib import asynccontextmanager
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
    try:
        yield
    finally:
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

from app.api.extract import router as extract_router
app.include_router(extract_router)
