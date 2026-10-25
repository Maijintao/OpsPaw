"""会话标题:满 2 轮用户消息后,后台用小模型起一个 4~10 字的小标题,写 conversations.title。
失败只 log 不重试——title 仍为空,下一轮自然重触发;前端在 title 为空时回退显示首问截断。
防抖/异常处理模式与 summarizer 一致。"""
import asyncio
import logging

from pydantic import BaseModel, Field

from app.core import llm
from app.core.prompts import TITLE_PROMPT
from app.db import repository

logger = logging.getLogger(__name__)

_running: set[int] = set()   # 在跑会话集合(防抖)


class _Title(BaseModel):
    title: str = Field(description="会话小标题,4~10 个字")


async def gen_title(dialog: str) -> str:
    """LLM 起标题,严格 JSON(structured output)。"""
    model = llm.structured(_Title)
    r: _Title = await (TITLE_PROMPT | model).ainvoke({"dialog": dialog})
    return (r.title or "").strip()[:32]


async def run_title(conversation_id: int) -> None:
    """任务体:已有 title 跳过;用户消息 < 2 轮跳过;否则拼前 6 条对话起标题并落库。"""
    conv = await repository.get_conversation(conversation_id)
    if conv is None or conv.title:
        return
    msgs = await repository.list_dialog_messages(conversation_id)
    n_user = sum(1 for m in msgs if m.role == "user")
    if n_user < 2:
        return
    dialog = "\n".join(
        f"{'用户' if m.role == 'user' else '助手'}:{(m.content or '')[:200]}"
        for m in msgs[:6] if m.content)
    if not dialog:
        return
    title = await gen_title(dialog)
    if not title:
        raise ValueError("标题为空,放弃更新")
    await repository.update_conversation_title(conversation_id, title)
    logger.info("title done conv=%s title=%s", conversation_id, title)


async def maybe_schedule_title(conversation_id: int) -> None:
    """轮结束后调用:title 已有/不足 2 轮直接跳过;否则 create_task 后台跑,不 await。
    本函数在回复路径上,任何异常只 log 不外抛。"""
    try:
        if conversation_id in _running:
            return
        conv = await repository.get_conversation(conversation_id)
        if conv is None or conv.title:
            return
        msgs = await repository.list_dialog_messages(conversation_id)
        if sum(1 for m in msgs if m.role == "user") < 2:
            return
        if conversation_id in _running:
            return
        _running.add(conversation_id)
        task = asyncio.create_task(run_title(conversation_id))

        def _done(t: asyncio.Task) -> None:
            _running.discard(conversation_id)
            if not t.cancelled() and t.exception() is not None:
                logger.error("title failed conv=%s", conversation_id, exc_info=t.exception())

        task.add_done_callback(_done)
    except Exception:
        logger.exception("title trigger check failed conv=%s", conversation_id)
