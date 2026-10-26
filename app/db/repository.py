from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, select, update

import app.db.base as db
from app.db.models import Conversation, Message, Ticket, ToolAuditLog, Target

_TICKET_SEQ = 0

def _gen_ticket_no() -> str:
    global _TICKET_SEQ
    _TICKET_SEQ += 1
    return f"T{datetime.now():%Y%m%d%H%M%S}{_TICKET_SEQ:03d}"


async def create_conversation(user_id: str) -> int:
    async with db.async_session() as s:
        conv = Conversation(user_id=user_id)
        s.add(conv)
        await s.commit()
        return conv.id


async def get_conversation(conversation_id: int) -> Conversation | None:
    async with db.async_session() as s:
        return await s.get(Conversation, conversation_id)


async def append_message(
    conversation_id: int,
    role: str,
    content: str | None = None,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
    meta: dict | None = None,
) -> int:
    async with db.async_session() as s:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            meta=meta,
        )
        s.add(msg)
        await s.commit()
        return msg.id


async def list_messages(conversation_id: int) -> list[Message]:
    async with db.async_session() as s:
        result = await s.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)
        )
        return list(result.scalars())


async def create_ticket(conversation_id: int, description: str, ticket_type: str) -> str:
    ticket_no = _gen_ticket_no()
    async with db.async_session() as s:
        s.add(
            Ticket(
                ticket_no=ticket_no,
                conversation_id=conversation_id,
                description=description,
                ticket_type=ticket_type,
            )
        )
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.status = "已转人工"
        await s.commit()
    return ticket_no


async def count_messages_after(conversation_id: int, after_id: int | None) -> int:
    """距上次摘要新增了多少条(after_id 空 = 从未摘要,数全量)。摘要触发判据。
    注:此处数全角色,摘要源 list_dialog_messages 只取 user/assistant——当前一致
    (tool 不落库);若未来 tool 行落库,两处口径需同步收紧。"""
    async with db.async_session() as s:
        q = (select(func.count()).select_from(Message)
             .where(Message.conversation_id == conversation_id))
        if after_id:
            q = q.where(Message.id > after_id)
        return int((await s.execute(q)).scalar_one())


async def list_dialog_messages(conversation_id: int) -> list[Message]:
    """user/assistant 消息按 id 升序(摘要任务源数据;tool 行不落库,过滤一道保险)。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id,
                   Message.role.in_(("user", "assistant")))
            .order_by(Message.id)
        )
        return list(result.scalars())


async def list_conversations(user_id: str, limit: int = 50) -> list[dict]:
    """某用户的会话列表(新在前,带首问预览 + 有无摘要标记),前端多会话切换用。"""
    async with db.async_session() as s:
        convs = list((await s.execute(
            select(Conversation).where(Conversation.user_id == user_id)
            .order_by(Conversation.id.desc()).limit(limit)
        )).scalars())
        out = []
        for c in convs:
            first = (await s.execute(
                select(Message.content)
                .where(Message.conversation_id == c.id, Message.role == "user")
                .order_by(Message.id).limit(1)
            )).scalar()
            out.append({
                "id": c.id, "status": c.status,
                "title": c.title,
                "preview": (first or "(空会话)")[:40],
                "has_summary": bool(c.summary),
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            })
        return out


async def update_conversation_title(conversation_id: int, title: str) -> None:
    """会话小标题生成成功后落库(只写一次,已有值不覆盖)。"""
    async with db.async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is not None and not conv.title:
            conv.title = title
            await s.commit()


async def delete_conversation(conversation_id: int) -> bool:
    """删除会话及其内容本体(messages/tickets)。两类关联行不删只摘引用:
    tool_audit_logs(审计必须留痕,无 FK 本可悬空,摘成 NULL 免得 join 出错数据)与
    low_confidence_questions(飞轮池子是资产,不随会话蒸发)。LangGraph checkpointer
    的线程状态另在 runtime.delete_thread 清,这里只管主库。"""
    async with db.async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is None:
            return False
        await s.execute(delete(Message).where(Message.conversation_id == conversation_id))
        await s.execute(delete(Ticket).where(Ticket.conversation_id == conversation_id))
        await s.execute(update(ToolAuditLog)
                        .where(ToolAuditLog.conversation_id == conversation_id)
                        .values(conversation_id=None))
        await s.delete(conv)
        await s.commit()
        return True


async def update_conversation_summary(conversation_id: int, summary: str, upto_msg_id: int) -> None:
    """摘要成功后原子更新两字段(一起写,不会出现摘要新边界旧)。"""
    async with db.async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv is not None:
            conv.summary = summary
            conv.summary_upto_msg_id = upto_msg_id
            await s.commit()


async def insert_tool_audit(
    conversation_id: int | None,
    tool_call_id: str | None,
    tool_name: str,
    tool_source: str,
    mcp_server: str | None,
    arguments: dict | None,
    result_summary: str | None,
    status: str,
    error_message: str | None,
    retry_count: int,
    duration_ms: int | None,
) -> None:
    """工具调用审计落一条。调用方(engine)自行 try/except——审计失败不许反拦工具执行。"""
    async with db.async_session() as s:
        s.add(ToolAuditLog(
            conversation_id=conversation_id, tool_call_id=tool_call_id,
            tool_name=tool_name, tool_source=tool_source, mcp_server=mcp_server,
            arguments=arguments, result_summary=result_summary, status=status,
            error_message=error_message, retry_count=retry_count, duration_ms=duration_ms,
        ))
        await s.commit()


async def list_targets() -> list[Target]:
    async with db.async_session() as s:
        rows = (await s.execute(select(Target).order_by(Target.id))).scalars().all()
        return list(rows)


async def ensure_targets_table() -> None:
    """项目没有迁移框架,历史建表走 sql/ 手工 DDL;这里只对新表做启动自建:
    checkfirst=True 内部先查表是否存在,只补建缺失的 targets 一张,已有表一概不碰。

    结构对不上(表里还是旧的双列 URL 版、没有 sources 列)时重建——targets 表当日成立、
    无存量数据,不值得写迁移;重建前查一遍行数,有数据就不动表只告警,宁可起不来也不删人的配置。"""
    import logging

    from sqlalchemy import inspect, text

    def _ensure(sync_conn) -> None:
        insp = inspect(sync_conn)
        if not insp.has_table("targets"):
            Target.__table__.create(sync_conn)
            return
        cols = {c["name"] for c in insp.get_columns("targets")}
        if "cluster" not in cols:      # 多云分组列:老库补一列,不重建表、不动数据
            sync_conn.execute(text(
                "ALTER TABLE targets ADD COLUMN cluster VARCHAR(64) DEFAULT ''"))
        if "sources" in cols:
            return
        n_rows = sync_conn.execute(text("SELECT COUNT(*) FROM targets")).scalar() or 0
        if n_rows:
            logging.getLogger("app").error(
                "targets 表是旧结构且有 %s 行数据,不自动重建;请手工迁移后重启", n_rows)
            return
        Target.__table__.drop(sync_conn)
        Target.__table__.create(sync_conn)

    async with db.engine.begin() as conn:
        await conn.run_sync(_ensure)


async def create_target(name: str, sources: list, cluster: str = "") -> Target:
    async with db.async_session() as s:
        row = Target(name=name, sources=sources, cluster=cluster or "")
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def update_target(target_id: int, name: str, sources: list,
                        cluster: str = "") -> Target | None:
    async with db.async_session() as s:
        row = await s.get(Target, target_id)
        if row is None:
            return None
        row.name, row.sources = name, sources
        row.cluster = cluster or ""
        await s.commit()
        await s.refresh(row)
        return row


async def get_target(target_id: int) -> Target | None:
    async with db.async_session() as s:
        return await s.get(Target, target_id)


async def delete_target(target_id: int) -> bool:
    async with db.async_session() as s:
        row = await s.get(Target, target_id)
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
        return True


async def update_target_sources(target_id: int, sources: list) -> Target | None:
    """只改 sources(指纹刷新/自愈改址专用):不碰 name——机器自动写回不许动人工起的字段。"""
    async with db.async_session() as s:
        row = await s.get(Target, target_id)
        if row is None:
            return None
        row.sources = sources
        await s.commit()
        await s.refresh(row)
        return row
