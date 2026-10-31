"""独立主题分类推理接口。问题池分布和批量落库等 RAG 数据接口待数据表就绪后接入。"""
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.config import settings
from app.core.taxonomy import TOPIC_CLASSES
from app.db import repository

router = APIRouter(prefix="/api/topics", tags=["topics"])

class ClassifyBody(BaseModel):
    texts: list[str] = Field(min_length=1)

@router.get("")
async def topics():
    online = False
    try:
        async with httpx.AsyncClient(timeout=settings.tool_default_timeout, trust_env=False) as client:
            response = await client.get(settings.classifier_base_url.rstrip("/") + "/healthz")
            online = response.is_success
    except httpx.HTTPError:
        pass
    try:
        distribution = await repository.topic_distribution()
        pool_ready = True
        pool_error = None
    except Exception as exc:
        distribution = None
        pool_ready = False
        pool_error = f"{type(exc).__name__}: {exc}"
    return {"topics": [{"name": item.name, "boundary": item.boundary,
                        "examples": list(item.examples), "severity": item.severity}
                       for item in TOPIC_CLASSES],
            "classifier_online": online,
            "pool_ready": pool_ready,
            "pool": distribution,
            "pool_note": None if pool_ready else f"问题池暂时读不到：{pool_error}"}

@router.post("/classify")
async def classify(body: ClassifyBody):
    try:
        async with httpx.AsyncClient(timeout=settings.tool_default_timeout, trust_env=False) as client:
            response = await client.post(settings.classifier_base_url.rstrip("/") + "/classify",
                                         json={"texts": body.texts})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, "分类器返回错误") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, "分类器未启动或模型文件未就绪") from exc
