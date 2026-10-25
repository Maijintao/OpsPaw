import logging

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.runnables import Runnable

from app.core.llm import structured
from app.core.prompts import EXTRACT_PROMPT
from app.schemas.extract import ExtractRequest, OpsTicket

logger = logging.getLogger(__name__)
router = APIRouter()


def get_extractor() -> Runnable:
    # structured():function calling 路,json_schema/response_format 在 DeepSeek 官方端点 400
    return EXTRACT_PROMPT | structured(OpsTicket)


@router.post("/api/extract", response_model=OpsTicket)
async def extract(
    req: ExtractRequest, extractor: Runnable = Depends(get_extractor)
) -> OpsTicket:
    try:
        return await extractor.ainvoke({"text": req.text})
    except Exception as exc:
        logger.exception("结构化提取失败")
        raise HTTPException(status_code=502, detail="上游模型暂时不可用,请稍后重试") from exc
