import asyncio
import logging
from collections.abc import AsyncIterator

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.config import settings
from app.db import repository
from app.graph.build import build_graph

logger = logging.getLogger(__name__)

# 会流式吐答复 token 的节点(其余节点的模型调用 token 不进 delta)
ANSWER_NODES = {"main_agent"}
# 确定性节点把答复写在 state['answer'],整块作为 delta 吐出
DETERMINISTIC_ANSWER_NODES = {"script_reply", "complaint_reply"}
# 产出编号引用的检索节点(知识路 retrieve_knowledge + runbook 检索路 retrieve_runbook)
CITATION_NODES = set()

# ---- 轮次与 SSE 连接解耦 ----
# 之前整轮图的执行拴在 SSE 生成器里,用户切页=连接断=生成器被取消=跑到一半的轮次
# 被连坐杀掉:答复没落库、检查点留半截状态,下一句直接"回答错误"(真实案例)。
# 现在轮次由独立任务驱动,事件进队列,SSE 只消费队列——断开的只是"看","跑"照常完成。
_TURN_DONE = object()                      # 队列哨兵:本轮事件流结束
_TURN_LOCKS: dict[int, asyncio.Lock] = {}  # 同会话串行,防两轮交叉写检查点
_TURN_TASKS: dict[int, asyncio.Task] = {}  # 强引用防 GC:没人 await 也必须跑完

_graph = None
_cm = None  # AsyncSqliteSaver context manager,持有以免被 GC
_checkpointer = None   # 删除会话时清线程状态用


class ConversationNotFound(Exception):
    pass


async def init_graph() -> None:
    """lifespan 起:开持久 checkpointer + setup + 编译图。"""
    global _graph, _cm, _checkpointer
    _cm = AsyncSqliteSaver.from_conn_string(settings.checkpointer_db_path)
    checkpointer = await _cm.__aenter__()
    await checkpointer.setup()
    _checkpointer = checkpointer
    # ch09:编译时挂一次 Langfuse 回调,全图自动 trace(未配置时原样返回,零依赖)
    _graph = build_graph(checkpointer=checkpointer)
    logger.info("图已编译,checkpointer=%s", settings.checkpointer_db_path)


async def close_graph() -> None:
    global _graph, _cm, _checkpointer
    if _cm is not None:
        await _cm.__aexit__(None, None, None)
        _cm = None
    _graph = None
    _checkpointer = None


async def delete_thread(conversation_id: int) -> None:
    """删除会话时同步清 checkpointer 里的线程状态(checkpoints/writes 两表按 thread_id 删)。
    不清的话主库行没了、图状态还在:切换/新建撞上同 id 语义虽不会复现,但状态文件越积越大,
    且「快照尽力回捞」这类按 thread 读的路径会捞到幽灵数据。清失败不挡删除(主库已删,尽力而为)。"""
    if _checkpointer is None:
        return
    tid = str(conversation_id)
    try:
        conn = _checkpointer.conn
        await conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (tid,))
        await conn.execute("DELETE FROM writes WHERE thread_id = ?", (tid,))
        await conn.commit()
        logger.info("会话 %s 的 checkpointer 线程状态已清", conversation_id)
    except Exception:  # noqa: BLE001
        logger.warning("清 checkpointer 线程 %s 失败(不影响会话删除)", conversation_id,
                       exc_info=True)


def get_graph():
    if _graph is None:
        raise RuntimeError("图未初始化,应在 FastAPI lifespan 调 init_graph()")
    return _graph


async def get_turn_snapshot(conversation_id: int) -> dict:
    """👎 快照尽力回捞(ch09):读该会话 checkpointer 终态,返回最近一轮的用户问题与召回快照。
    调用方拿去比对被踩的 question,对得上才用快照——踩历史消息时快照可能已是别轮的,不硬塞。"""
    config = {"configurable": {"thread_id": str(conversation_id)}}
    st = await get_graph().aget_state(config)
    values = getattr(st, "values", None) or {}
    question = ""
    for m in reversed(values.get("messages", [])):
        if getattr(m, "type", "") == "human":
            question = m.content if isinstance(m.content, str) else ""
            break
    return {"question": question, "snapshot": values.get("retrieved_snapshot") or []}


def dedup_actions(actions: list) -> list:
    """跨多个 ReAct 轮次累积的 suggested_actions 可能重复(如 agent 多次提议
    create_ticket);按 type 去重,保留每种 type 首次出现的那条。"""
    seen = set()
    out = []
    for action in actions:
        t = action.get("type")
        if t in seen:
            continue
        seen.add(t)
        out.append(action)
    return out


async def _ensure_conversation(user_id: str, conversation_id: int | None) -> tuple[int, str, int]:
    """返回 (cid, summary, summary_upto_msg_id):入口一次库读把 ch07 摘要两字段一并带出。"""
    if conversation_id is None:
        return await repository.create_conversation(user_id), "", 0
    conv = await repository.get_conversation(conversation_id)
    if conv is None:
        raise ConversationNotFound(conversation_id)
    return conversation_id, conv.summary or "", conv.summary_upto_msg_id or 0


def _graph_input(user_id: str, message: str, cid: int, msg_id: int,
                 summary: str, summary_upto: int) -> dict:
    # 显式重置每轮的输出通道:checkpointer 按 thread_id 持久化 State,这些无 reducer 的标量
    # 会跨轮残留(如上一轮闲聊/投诉的 answer、建工单可选项串到本轮),须在入口清零。
    # messages 有 add_messages reducer(追加=跨轮历史),不清;steps/tokens_used 是本轮预算。
    # ch07:用户消息带 db-{msg_id} 锚点(滑窗与摘要边界对齐);summary 两字段每轮从库刷新。
    return {"messages": [HumanMessage(message, id=f"db-{msg_id}")], "user_id": user_id,
            "conversation_id": cid, "steps": 0, "tokens_used": 0,
            "answer": "", "suggested_actions": [], "evidence": "",
            "citations": [],
            "evidence_confidence": 0.0, "retrieved_snapshot": [],
            "resolved_query": "", "intent_confidence": 0.0,
            "service_id": "", "service_data": {},            "summary": summary, "summary_upto_msg_id": summary_upto,
            "trace": None}  # merge 通道:None=重置哨兵(见 merge_dict),清上一轮 trace 残留


def _interrupt_payload(state: dict):
    """从终态提取中断负载(整个 __interrupt__[0].value:含 type + services/preview 等);
    无中断返回 None。ch08 起中断不止服务选择器,负载透传、消费方按 type 分发。"""
    intr = state.get("__interrupt__")
    if not intr:
        return None
    return intr[0].value


async def run_turn(user_id, message, conversation_id) -> dict:
    """非流式:落 user 消息 → ainvoke → 返回终态(供 /api/agent、eval)。遇 interrupt 带 services。"""
    cid, summary, upto = await _ensure_conversation(user_id, conversation_id)
    msg_id = await repository.append_message(cid, "user", content=message)
    # ch09:langfuse_session_id 让同一会话多轮在 Langfuse 界面串成一组(未挂回调时无害)
    config = {"configurable": {"thread_id": str(cid)}}
    final = await get_graph().ainvoke(
        _graph_input(user_id, message, cid, msg_id, summary, upto), config)
    return {"conversation_id": cid, "state": final, "interrupt": _interrupt_payload(final)}


async def resume_turn(conversation_id: int, resume_value) -> dict:
    """非流式续跑(供 /api/agent、eval):Command(resume) 回填后跑到下一个中断或结束。"""
    if await repository.get_conversation(conversation_id) is None:
        raise ConversationNotFound(conversation_id)
    config = {"configurable": {"thread_id": str(conversation_id)},
              "metadata": {"langfuse_session_id": str(conversation_id)}}
    final = await get_graph().ainvoke(Command(resume=resume_value), config)
    return {"conversation_id": conversation_id, "state": final,
            "interrupt": _interrupt_payload(final)}


async def _stream_events(cid: int, stream_source) -> AsyncIterator[dict]:
    """astream 多模式 → 事件 dict。stream_source 为 _graph_input(...)(新一轮)或 Command(resume=...)(续跑)。
    遇 interrupt(updates 块含 __interrupt__,Task 1 冒烟钉死)→ 吐 interrupt 事件并结束本次流
    (前端点选后走 /api/actions/resume 续流)。"""
    config = {"configurable": {"thread_id": str(cid)}}
    actions: list = []
    async for mode, chunk in get_graph().astream(
        stream_source, config, stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            msg, meta = chunk
            if meta.get("langgraph_node") in ANSWER_NODES:
                text = msg.content if isinstance(msg.content, str) else ""
                if text:
                    yield {"type": "delta", "text": text}
        elif mode == "updates":
            if "__interrupt__" in chunk:
                payload = chunk["__interrupt__"][0].value
                # 带上 conversation_id:中断时不发 done 帧,前端要靠这个才知道续跑用哪个会话。
                # ch08:负载按中断类型透传(select_service 带 services,confirm_ticket 带 preview),
                # 前端按 kind 分发。
                ev = {"type": "interrupt", "kind": payload.get("type", ""), "conversation_id": cid}
                for k in ("services", "preview"):
                    if payload.get(k) is not None:
                        ev[k] = payload[k]
                yield ev
                return
            for node, upd in chunk.items():
                if not isinstance(upd, dict):
                    continue
                if node in DETERMINISTIC_ANSWER_NODES and upd.get("answer"):
                    yield {"type": "delta", "text": upd["answer"]}
                if node in CITATION_NODES and upd.get("citations"):
                    yield {"type": "citations", "items": upd["citations"]}
                if node == "agent_tools":
                    for m in upd.get("messages", []):
                        name = getattr(m, "name", None)
                        # submit_change 仍是「拦成前端表单」的合成消息,不发工具帧;
                        # create_ticket ch08 起确认后真执行,照常发帧
                        if name and name != "submit_change":
                            yield {"type": "tool", "name": name}
                if upd.get("suggested_actions"):
                    actions.extend(upd["suggested_actions"])
    if actions:
        yield {"type": "actions", "items": dedup_actions(actions)}
    yield {"type": "done", "conversation_id": cid}


async def _drive_turn(cid: int, stream_source, q: asyncio.Queue) -> None:
    """轮次驱动任务:与 SSE 连接解耦。连接断开只影响队列消费,图照常跑完——
    答复落库、摘要照常,用户切页回来刷新就能看到完整回复。任务内的错误转成
    error 事件帧,不在连接侧炸成"上游模型暂时不可用"的假象。"""
    try:
        async with _TURN_LOCKS.setdefault(cid, asyncio.Lock()):
            async for ev in _stream_events(cid, stream_source):
                await q.put(ev)
    except Exception:  # noqa: BLE001
        logger.exception("流式轮次失败 conv=%s", cid)
        await q.put({"type": "error", "message": "上游模型暂时不可用,请稍后重试"})
    finally:
        if _TURN_TASKS.get(cid) is asyncio.current_task():
            _TURN_TASKS.pop(cid, None)
        await q.put(_TURN_DONE)


async def _drain(q: asyncio.Queue) -> AsyncIterator[dict]:
    while True:
        ev = await q.get()
        if ev is _TURN_DONE:
            return
        yield ev


async def _decoupled(cid: int, stream_source) -> AsyncIterator[dict]:
    """把轮次包成独立任务 + 队列,SSE 侧只负责 drain。"""
    q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_drive_turn(cid, stream_source, q))
    _TURN_TASKS[cid] = task
    async for ev in _drain(q):
        yield ev


async def stream_turn(user_id, message, conversation_id) -> AsyncIterator[dict]:
    """流式:落 user 消息 → 后台任务驱动 astream → 事件 dict(供 /api/chat 转 SSE)。
    会话不存在/落库失败仍在连接期抛出,由端点转 SSE 错误帧;图执行期的错误走队列。"""
    cid, summary, upto = await _ensure_conversation(user_id, conversation_id)
    msg_id = await repository.append_message(cid, "user", content=message)
    async for ev in _decoupled(cid, _graph_input(user_id, message, cid, msg_id, summary, upto)):
        yield ev


async def stream_resume(conversation_id: int, resume_value) -> AsyncIterator[dict]:
    """前端点选服务后续跑同一会话的暂停流(Command(resume) 续 astream,事件与 stream_turn 同构)。"""
    if await repository.get_conversation(conversation_id) is None:
        raise ConversationNotFound(conversation_id)
    async for ev in _decoupled(conversation_id, Command(resume=resume_value)):
        yield ev
