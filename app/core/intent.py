import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.core import llm
from app.core.llm import get_chat_model
from app.core.prompts import INTENT_CLASSIFY_PROMPT

logger = logging.getLogger("app")

INTENTS = ("日志指标", "服务状态", "运维知识", "变更申请", "故障处理", "投诉",
           "建单跟进", "配置管理", "闲聊", "其他")


class _Intent(BaseModel):
    intent: Literal["日志指标", "服务状态", "运维知识", "变更申请", "故障处理", "投诉",
                    "建单跟进", "配置管理", "闲聊", "其他"] = Field(description="十类意图之一")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="判断把握 0-1")


async def classify(query: str, history: str = "") -> dict:
    """十类意图 + confidence(ch08 增「人工」:明确要求建工单/转人工)。扁平字段避开 glm 嵌套 502。

    **分类器失败 ≠ 用户说不清**:模型调用抛异常时返回带 error 的结果,由调用方转 Agent 处理;
    绝不能当成「其他」——那会把上游抖动变成一句「抱歉,我不太确定您的意思」,把系统的错
    算在用户头上(2026-10-22 实测:一句「商品无法加载了,你去看看什么原因」被判成听不懂,
    单独测这句话分类器给的是故障处理 0.90——差的就是那一次调用失败)。
    失败重试一次:瞬时抖动占多数,多给一次机会比直接降级划算。
    模型默认走 settings.intent_model(空则回落 chat_model)——先求准。"""
    model = llm.structured(_Intent, model=settings.intent_model or None)
    last: Exception | None = None
    for _ in range(2):
        try:
            r: _Intent = await (INTENT_CLASSIFY_PROMPT | model).ainvoke(
                {"query": query, "history": history or "(无)"})
            intent = r.intent if r.intent in INTENTS else "其他"
            return {"intent": intent, "confidence": float(r.confidence)}
        except Exception as exc:  # noqa: BLE001  重试后仍失败才降级
            last = exc
    logger.warning("意图分类失败(已重试):%s", last)
    return {"intent": "其他", "confidence": 0.0,
            "error": f"{type(last).__name__}: {str(last)[:120]}"}
