"""ch07 后台滚动摘要:轮结束后计数触发,asyncio 后台跑,不阻塞当轮回复。
失败只 log 不重试——触发条件仍满足,下一轮自然重触发;summary 两字段只在成功后原子更新。"""
import asyncio
import logging
import time

from pydantic import BaseModel, Field

from app.config import settings
from app.core import llm
from app.core.llm import get_chat_model
from app.core.prompts import SUMMARY_PROMPT
from app.db import repository

logger = logging.getLogger(__name__)

_running: dict[int, asyncio.Task] = {}   # cid -> 在跑任务(防抖:在跑不重复起)


class _Summary(BaseModel):
    summary: str = Field(description="早期对话滚动摘要,只含事实与诉求")


def compute_boundary(messages, keep_turns: int) -> int | None:
    """新摘要边界 = 倒数第 keep_turns 轮用户消息的前一条消息 id(滑窗从其后接原文)。
    不足 keep_turns 轮 / 边界落在会话开头 → None(本次放弃)。
    messages:按 id 升序的 user/assistant 行(需 .id/.role)。"""
    user_idx = [i for i, m in enumerate(messages) if m.role == "user"]
    if len(user_idx) <= keep_turns:
        return None
    cut = user_idx[-keep_turns]
    if cut == 0:
        return None
    return messages[cut - 1].id


async def summarize_dialog(old_summary: str, dialog: str) -> str:
    """LLM 滚动重写:旧摘要 + 新滑出段 → 新摘要。严格 JSON(structured output)。"""
    model = llm.structured(_Summary, model=settings.summary_model or None)
    r: _Summary = await (SUMMARY_PROMPT | model).ainvoke(
        {"old_summary": old_summary or "(无)", "dialog": dialog})
    return (r.summary or "").strip()


async def run_summary(conversation_id: int) -> None:
    """任务体:读消息 → 算边界 → 压缩 → 原子更新两字段。异常往外抛,由 done 回调统一 log。"""
    t0 = time.monotonic()
    conv = await repository.get_conversation(conversation_id)
    if conv is None:
        return
    msgs = await repository.list_dialog_messages(conversation_id)
    boundary = compute_boundary(msgs, settings.context_window_turns)
    old_upto = conv.summary_upto_msg_id or 0
    if boundary is None or boundary <= old_upto:
        logger.info("summary skip conv=%s boundary=%s upto=%s",
                    conversation_id, boundary, old_upto)
        return
    seg = [m for m in msgs if old_upto < m.id <= boundary]
    dialog = "\n".join(
        f"{'用户' if m.role == 'user' else '值班猫'}:{m.content}" for m in seg if m.content)
    logger.info("summary start conv=%s msgs=%s upto %s->%s",
                conversation_id, len(seg), old_upto, boundary)
    summary = await summarize_dialog(conv.summary or "", dialog)
    if not summary:
        raise ValueError("摘要为空,放弃更新")
    await repository.update_conversation_summary(conversation_id, summary, boundary)
    logger.info("summary done conv=%s upto=%s len=%s cost=%.0fms",
                conversation_id, boundary, len(summary), (time.monotonic() - t0) * 1000)


async def maybe_schedule_summary(conversation_id: int) -> None:
    """轮结束后调用:新增消息数达阈值且无在跑任务 → create_task 后台跑,不 await(不阻塞回复)。
    本函数在用户回复路径上,任何异常只 log 不外抛(触发条件仍在,下一轮重来)。"""
    try:
        if conversation_id in _running:
            return
        conv = await repository.get_conversation(conversation_id)
        if conv is None:
            return
        n = await repository.count_messages_after(conversation_id, conv.summary_upto_msg_id)
        if n < settings.summary_trigger_messages:
            return
        # 上面两个 await 期间可能有并发触发已入表(TOCTOU),入表前再查一次防双起
        if conversation_id in _running:
            return
        logger.info("summary trigger conv=%s new_msgs=%s", conversation_id, n)
        task = asyncio.create_task(run_summary(conversation_id))
        _running[conversation_id] = task

        def _done(t: asyncio.Task) -> None:
            if _running.get(conversation_id) is t:   # 只清自己,防误清后来者
                _running.pop(conversation_id, None)
            if not t.cancelled() and t.exception() is not None:
                logger.error("summary failed conv=%s", conversation_id, exc_info=t.exception())

        task.add_done_callback(_done)
    except Exception:
        logger.exception("summary trigger check failed conv=%s", conversation_id)
