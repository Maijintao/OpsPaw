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


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await runtime.init_graph()
    try:
        yield
    finally:
        await runtime.close_graph()
        await engine.dispose()


app = FastAPI(title="OpsPaw", version="0.1.0", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(agent_router)

from app.api.extract import router as extract_router
app.include_router(extract_router)
