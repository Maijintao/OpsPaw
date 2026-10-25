import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from app.config import settings
from app.core.llm import get_chat_model, needs_non_streaming_tools
from app.core.prompts import AGENT_SYSTEM
from app.db import repository

logger = logging.getLogger(__name__)
from app.core import intent as intent_mod
from app.graph.routing import INTENT_TO_ROUTE
from app.core.prompts import SCRIPT_REPLY_CHITCHAT, SCRIPT_REPLY_OTHER, COMPLAINT_REPLY_TEXT
from app.core import coref, memory
TURN_CTX_ID = "turn-ctx"

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
    """摘要行 + 完整滑窗(不含本轮最后一条 human),供 coref/意图读上下文。
    之前只取最近 6 轮,"上一轮提问、这一轮确认"的延续经常被裁出窗口,意图识别
    拿不到待确认的提议就判闲聊,对话当场掐断(真实案例:"去呀!"落零模型话术)。
    滑窗本身有 token 上限(与主 Agent 同窗),这里不再二次裁剪。"""
    msgs = memory.build_window(state.get("messages", []),
                               state.get("summary_upto_msg_id") or 0,
                               settings.context_window_max_tokens)
    prior = msgs[:-1] if msgs else []
    lines = []
    for m in prior:
        role = "用户" if isinstance(m, HumanMessage) else "值班猫"
        text = m.content if isinstance(m.content, str) else ""
        if text:
            lines.append(f"{role}:{text}")
    body = "\n".join(lines)
    head = memory.summary_line(state.get("summary"))
    return f"{head}\n{body}".strip() if head else body


async def resolve_reference(state) -> dict:
    """指代消解 + Query 改写(合一):吃最近几轮历史把半截话补成完整问句;已完整则透传。
    写 resolved_query,供 classify_intent 与 change_flow 检索共用(意图识别不再改写)。"""
    query = _user_text(state)
    history = _history_text(state)
    # ch07 验收可观测:每轮必经此节点,摘要行+滑窗在这一条日志里直接可见
    # (model_ctx 只在进 main_agent 的路由才有,闲聊/兜底路由靠这条看)
    logger.info("history_ctx conv=%s(摘要行+滑窗,coref/意图共用)\n%s",
                state.get("conversation_id"), history or "(无历史)")
    resolved = await coref.resolve(query, history)
    mode = "rewrite" if resolved != query else "passthrough"
    return {"resolved_query": resolved, "trace": {"coref": mode}}


def _with_turn_context(window: list, turn_ctx: str) -> list:
    """把本轮材料插在滑窗里最后一条用户消息之后。

    插在这个位置而不是整个列表末尾,是为了 ReAct 轮内也能命中前缀缓存:第 2 步的
    [Sys, 用户问, 材料, AI, Tool] 完整包含第 1 步的 [Sys, 用户问, 材料] 作前缀;
    追加到末尾则第 2 步变成 [Sys, 用户问, AI, Tool, 材料],公共前缀只到用户问那条。
    窗口里没有用户消息(极端裁剪回退分支)时退化成追加到末尾,不抛异常。"""
    msg = HumanMessage(turn_ctx, id=TURN_CTX_ID)
    for i in range(len(window) - 1, -1, -1):
        if isinstance(window[i], HumanMessage):
            return [*window[:i + 1], msg, *window[i + 1:]]
    return [*window, msg]


def _agent_messages(state) -> list:
    """拼装顺序固定(ch07):人设+红线 system → 滑窗原文 → 摘要与本轮材料(紧跟用户那句)。

    整条消息列表里**只有一条 SystemMessage**,内容恒为 AGENT_SYSTEM。这不是洁癖:
    上游的 chat template 会把列表里所有 system 消息上提、合并成一个头部块渲染,所以
    「摘要单独放第二条 system」等于把它拼在了 AGENT_SYSTEM 后面,工具 schema 被挤到
    可变内容之后 —— 而 prompt caching 按渲染后的前缀精确匹配,于是整段前缀全 miss。
    实测:无摘要的会话 cache_read=2048,摘要一进 system 就掉到 0。
    摘要因此跟证据一起走用户侧那条消息。

    滑窗=摘要边界锚点切 + trim_messages token 兜底;State 全量历史不动,这里只现拼精简版。"""
    window = memory.build_window(state.get("messages", []),
                                 state.get("summary_upto_msg_id") or 0,
                                 settings.context_window_max_tokens)
    turn_ctx = _turn_context(state)
    if turn_ctx:
        window = _with_turn_context(window, turn_ctx)
    return [SystemMessage(AGENT_SYSTEM), *window]

def _turn_context(state) -> str:
    summary = memory.summary_system(state.get("summary"))
    return summary.content if summary is not None else ""

async def classify_intent(state) -> dict:
    query = state.get("resolved_query") or _user_text(state)
    result = await intent_mod.classify(query, _history_text(state))
    route = "service_ops" if result.get("error") else INTENT_TO_ROUTE.get(result["intent"], "service_ops")
    trace = {"intent": result["intent"], "intent_confidence": result["confidence"], "route": route}
    if result.get("error"):
        trace["intent_error"] = result["error"]
    return {"intent": result["intent"], "intent_confidence": result["confidence"], "route": route, "trace": trace}

async def script_reply(state) -> dict:
    answer = SCRIPT_REPLY_OTHER if state.get("intent") == "其他" else SCRIPT_REPLY_CHITCHAT
    return {"answer": answer}

async def complaint_reply(state) -> dict:
    return {"answer": COMPLAINT_REPLY_TEXT}

async def main_agent(state) -> dict:
    model = get_chat_model(streaming=True)
    response = await model.ainvoke(_agent_messages(state))
    usage = getattr(response, "usage_metadata", None) or {}
    return {"messages": [response], "steps": state.get("steps", 0) + 1,
            "tokens_used": state.get("tokens_used", 0) + int(usage.get("total_tokens") or 0)}
