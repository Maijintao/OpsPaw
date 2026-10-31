import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.core import llm
from app.core.llm import get_chat_model
from app.core.prompts import INTENT_CLASSIFY_PROMPT

logger = logging.getLogger("app")

# 自我介绍是**代码级短路**(见 is_self_intro),模型不参与——它不是十类意图里的一类,
# 而是一个"必须先答"的动作。列进来只为让成本页/评测脚本认得这个标签。
INTENTS = ("日志指标", "服务状态", "运维知识", "变更申请", "故障处理", "投诉",
           "建单跟进", "配置管理", "闲聊", "其他", "自我介绍")

# 判"在问你自己是谁":要求**自称 + 询问自己**同时出现。
# 只认"你"不认"你们"——"介绍一下你们的变更流程"是在问业务,不能当自我介绍答。
_SELF_REF = re.compile(r"你(?!们)|您(?!们)")
_SELF_ASK = re.compile(
    r"是谁|叫什么|是什么"
    r"|(能|会|可以|擅长)(做|干)(什么|啥)(?!操作|才|之后|以后)"
    # 「你会什么」要认,「你会什么情况下重启」不能认——后面接条件词的是在问做法,不是问能力
    r"|(会|懂)(什么|啥)(?!情况|时候|条件|场景|情形)"
    r"|(有|会)(什么|哪些)(功能|能力|本事)"
    r"|(干|做)(什么|啥)的|干嘛的"
    r"|介绍.{0,4}你")


# 问"你是什么模型"是在打听底层模型,不是在了解产品能干什么——那是 agent 的边界外,
# 该走正常兜底(如实说不确定),不能拿自我介绍顶上去。**我们对外是 agent,不报底层模型。**
_ASK_MODEL = re.compile(r"模型|大模型|LLM|GPT|Claude|DeepSeek|通义|文心|参数量?|哪家",
                        re.IGNORECASE)


def asks_about_model(query: str) -> bool:
    """是不是在打听底层模型。这类问题走"其他"兜底(如实说不确定),不进 Agent、不报模型。"""
    return bool(_ASK_MODEL.search((query or "").strip()))


def is_self_intro(query: str) -> bool:
    """这句话是不是在让值班猫做自我介绍。"""
    q = (query or "").strip()
    if not q:
        return False
    if asks_about_model(q):
        return False          # 打听底层模型的,不归自我介绍管
    # 「自我介绍」本身就是诉求,不必再要求出现自称(没人会让运维机器人替别人写自我介绍)
    if "自我介绍" in q:
        return True
    return bool(_SELF_REF.search(q)) and bool(_SELF_ASK.search(q))


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
    模型默认走 settings.intent_model(空则回落 chat_model)——先求准。

    自我介绍先短路:问"你是谁"不花一次模型调用,也绝不能落成"我暂时还不会回答这个"——
    那是把"了解产品"当成了"说不清"。"""
    if is_self_intro(query):
        return {"intent": "自我介绍", "confidence": 1.0}
    # 打听底层模型的:归"其他",走"抱歉+把话题引回值班"那句。**对外是 agent,不报模型**,
    # 也不交给模型自己判——同一句话两次判出闲聊/其他,回话就会忽冷忽热。
    if asks_about_model(query):
        return {"intent": "其他", "confidence": 1.0}
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
