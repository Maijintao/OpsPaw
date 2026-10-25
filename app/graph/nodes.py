import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from app.config import settings
from app.core.llm import get_chat_model, needs_non_streaming_tools
from app.core.prompts import AGENT_SYSTEM
from app.db import repository

logger = logging.getLogger(__name__)

def _user_text(state) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return m.content or ""
    return ""


def resolve_answer(state) -> str:
    """最终答复:确定性节点写在 state['answer'];Agent 答复取最后一条 AIMessage 文本。"""
    if state.get("answer"):
        return state["answer"]
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, str):
                return content
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


async def log_node(state) -> dict:
    """日志记录:留痕 intent/route/trace(可观测地基),并落 MySQL 一条 assistant 消息(审计)。"""
    logger.info(
        "turn conv=%s intent=%s route=%s resolved=%r trace=%s",
        state.get("conversation_id"), state.get("intent"), state.get("route"),
        state.get("resolved_query"), state.get("trace", {}),
    )
    answer = resolve_answer(state)
    if state.get("conversation_id"):
        # suggested_actions 随消息落库(meta 列):前端历史回载要重建诊断卡/服务卡,
        # 之前不落库靠文案精确匹配,卡片刷新即丢。meta 只喂前端,不进 LLM 上下文。
        #
        # citations 同理:引用角标 [n] 在直播时由前端 decorateCitations 渲染成可点的
        # 珊瑚色上标,但回载时这份数据没有 → 只剩普通正文的 "[1]",颜色和浮层全丢。
        # 引用本身是模型输出的一部分,和正文一起落库才自洽。
        actions = state.get("suggested_actions") or None
        cites = state.get("citations") or None
        meta: dict = {}
        if actions:
            meta["suggested_actions"] = actions
        if cites:
            meta["citations"] = cites
        await repository.append_message(
            state["conversation_id"], "assistant",
            content=answer or None,
            meta=meta or None)
    return {}

def _history_text(state) -> str:
    return "\n".join(f"{m.type}:{m.content}" for m in state.get("messages", [])[:-1] if isinstance(m.content, str))

def _agent_messages(state) -> list:
    return [SystemMessage(AGENT_SYSTEM), *state.get("messages", [])]

async def main_agent(state) -> dict:
    model = get_chat_model(streaming=True)
    response = await model.ainvoke(_agent_messages(state))
    usage = getattr(response, "usage_metadata", None) or {}
    return {"messages": [response], "steps": state.get("steps", 0) + 1,
            "tokens_used": state.get("tokens_used", 0) + int(usage.get("total_tokens") or 0)}
