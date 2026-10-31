from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, select, update

import app.db.base as db          # 用模块属性引用,便于测试 monkeypatch async_session
from app.db.models import (
    AppSetting,
    Conversation,
    DiagnosisRun,
    EvalRun,
    FaithCase,
    KnowledgeChunk,
    LowConfidenceQuestion,
    Message,
    QaExtractionStaging,
    RepairApproval,
    RepairAuditLog,
    RepairConnector,
    RepairEnrollmentToken,
    RepairExecution,
    RepairPlan,
    ReviewQueue,
    RepairRun,
    Target,
    Ticket,
    ToolAuditLog,
    TopicClassification,
)

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


# ---- ch07 会话上下文管理(滑窗 + 异步摘要)----


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
        await s.execute(update(LowConfidenceQuestion)
                        .where(LowConfidenceQuestion.conversation_id == conversation_id)
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


# ---- ch03 知识库 chunk / 暂存表 ----


async def insert_knowledge_chunk(
    category: str, questions: str, answer: str,
    section_path: str | None = None, content_type: str | None = None,
    is_key_clause: int = 0,
) -> int:
    async with db.async_session() as s:
        row = KnowledgeChunk(
            category=category, questions=questions, answer=answer,
            section_path=section_path, content_type=content_type,
            is_key_clause=is_key_clause,
        )
        s.add(row)
        await s.commit()
        return row.id


async def list_pending_chunks(include_done: bool = False) -> list[KnowledgeChunk]:
    """返回待向量化块；切换 Milvus 实例时可显式包含已完成块做迁移。"""
    stmt = select(KnowledgeChunk).order_by(KnowledgeChunk.id)
    if not include_done:
        stmt = stmt.where(KnowledgeChunk.vectorize_status == "pending")
    async with db.async_session() as s:
        result = await s.execute(stmt)
        return list(result.scalars())


async def mark_chunk_vectorized(chunk_id: int, vector_id: str) -> None:
    async with db.async_session() as s:
        row = await s.get(KnowledgeChunk, chunk_id)
        if row is not None:
            row.vector_id = vector_id
            row.vectorize_status = "done"
            await s.commit()


async def set_chunk_neighbors(chunk_id: int, prev_id: int | None, next_id: int | None) -> None:
    async with db.async_session() as s:
        row = await s.get(KnowledgeChunk, chunk_id)
        if row is not None:
            row.prev_chunk_id = prev_id
            row.next_chunk_id = next_id
            await s.commit()


async def list_chunks_by_content_types(content_types) -> list[KnowledgeChunk]:
    """按 content_type 取块(文档类建库材料)。飞轮写回的块 content_type 不在这几类里,
    所以按这个条件取到的就是「来自 data/kb/*.md 的那部分」,补库改文件时只动它们。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.content_type.in_(list(content_types)))
            .order_by(KnowledgeChunk.id)
        )
        return list(result.scalars())


async def repend_chunk_text(chunk_id: int, questions: str, answer: str) -> None:
    """改写一块的正文并退回 pending,等 vectorize_pending 重嵌。

    为什么不是「删掉重插」:Milvus 那边 upsert 的主键就是这个 id,原地重嵌等于原地更新,
    引用角标、prev/next 邻居指针、飞轮写回的块都不受影响。整库重建(kb-reset)会连飞轮
    审核通过写回的那些块一起清掉,补库这种小改动犯不上。"""
    async with db.async_session() as s:
        row = await s.get(KnowledgeChunk, chunk_id)
        if row is not None:
            row.questions = questions
            row.answer = answer
            row.vectorize_status = "pending"
            await s.commit()


async def count_chunks_by_status(status: str) -> int:
    async with db.async_session() as s:
        result = await s.execute(
            select(func.count()).select_from(KnowledgeChunk)
            .where(KnowledgeChunk.vectorize_status == status)
        )
        return int(result.scalar_one())


async def list_all_questions() -> list[str]:
    async with db.async_session() as s:
        result = await s.execute(select(KnowledgeChunk.questions))
        return list(result.scalars())


async def count_chunks_by_content_types(content_types) -> int:
    async with db.async_session() as s:
        result = await s.execute(
            select(func.count()).select_from(KnowledgeChunk)
            .where(KnowledgeChunk.content_type.in_(list(content_types)))
        )
        return int(result.scalar_one())


async def knowledge_stats() -> dict:
    """知识库盘点:总块数 + 双写状态分布 + 内容类型分布 + 关键条款数。
    录入页的库存概览一次拿齐,不必让页面发四五个请求各问一个数。"""
    async with db.async_session() as s:
        total = (await s.execute(select(func.count()).select_from(KnowledgeChunk))).scalar_one()
        by_status = {k: int(v) for k, v in (await s.execute(
            select(KnowledgeChunk.vectorize_status, func.count())
            .group_by(KnowledgeChunk.vectorize_status))).all()}
        by_type = {(k or "未标注"): int(v) for k, v in (await s.execute(
            select(KnowledgeChunk.content_type, func.count())
            .group_by(KnowledgeChunk.content_type))).all()}
        key_clause = (await s.execute(
            select(func.count()).select_from(KnowledgeChunk)
            .where(KnowledgeChunk.is_key_clause == 1))).scalar_one()
    return {"total": int(total), "pending": by_status.get("pending", 0),
            "done": by_status.get("done", 0), "by_content_type": by_type,
            "key_clause": int(key_clause)}


async def list_recent_chunks(limit: int = 20) -> list[KnowledgeChunk]:
    async with db.async_session() as s:
        result = await s.execute(
            select(KnowledgeChunk).order_by(KnowledgeChunk.id.desc()).limit(limit)
        )
        return list(result.scalars())


async def list_chunk_pairs() -> list[tuple[str, str]]:
    """全表的 (questions, answer) 对,给录入查重算指纹。
    只按问法查重会误杀同一节切出来的多块——表格按行拆、超长散文递归切,这些块共用节标题,
    问法一模一样、正文各不相同,是该留的。"""
    async with db.async_session() as s:
        result = await s.execute(select(KnowledgeChunk.questions, KnowledgeChunk.answer))
        return [(q, a) for q, a in result.all()]


async def staging_stats() -> dict:
    """抽 QA 暂存表盘点:三个状态各几条 + 批次数 + 最近批次号。
    「先进暂存、整体去重再入库」这一步在页面上要看得见,靠的就是这三个状态的落差。"""
    async with db.async_session() as s:
        counts = {k: int(v) for k, v in (await s.execute(
            select(QaExtractionStaging.status, func.count())
            .group_by(QaExtractionStaging.status))).all()}
        batches = (await s.execute(
            select(func.count(func.distinct(QaExtractionStaging.batch_no))))).scalar_one()
        latest = (await s.execute(
            select(QaExtractionStaging.batch_no)
            .order_by(QaExtractionStaging.id.desc()).limit(1))).scalar_one_or_none()
    return {"counts": {k: counts.get(k, 0)
                       for k in ("extracted", "kept", "discarded")},
            "total": sum(counts.values()), "batches": int(batches), "latest_batch": latest}


async def insert_staging(batch_no: str, source_ref: str | None, question: str, answer: str) -> int:
    async with db.async_session() as s:
        row = QaExtractionStaging(
            batch_no=batch_no, source_ref=source_ref, question=question, answer=answer
        )
        s.add(row)
        await s.commit()
        return row.id


async def list_staging_by_status(status: str) -> list[QaExtractionStaging]:
    async with db.async_session() as s:
        result = await s.execute(
            select(QaExtractionStaging)
            .where(QaExtractionStaging.status == status)
            .order_by(QaExtractionStaging.id)
        )
        return list(result.scalars())


async def list_staging_by_ids(ids: list[int], status: str | None = None) -> list[QaExtractionStaging]:
    """按 id 取暂存行,可再限定状态。人工采纳/弃用时用它——限定 status='kept' 就能挡住
    重复采纳(点两次只有第一次拿得到行)和拿已弃用的行来入库。"""
    if not ids:
        return []
    async with db.async_session() as s:
        q = select(QaExtractionStaging).where(QaExtractionStaging.id.in_(ids))
        if status is not None:
            q = q.where(QaExtractionStaging.status == status)
        return list((await s.execute(q.order_by(QaExtractionStaging.id))).scalars())


async def set_staging_status(ids: list[int], status: str) -> None:
    if not ids:
        return
    async with db.async_session() as s:
        for i in ids:
            row = await s.get(QaExtractionStaging, i)
            if row is not None:
                row.status = status
        await s.commit()


async def insert_low_confidence(
    conversation_id: int | None, raw_question: str, source: str, reason: str | None,
    retrieved_chunks: list | None = None,
) -> int:
    async with db.async_session() as s:
        row = LowConfidenceQuestion(
            conversation_id=conversation_id, raw_question=raw_question,
            source=source, reason=reason, retrieved_chunks=retrieved_chunks,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def list_conversations_with_messages() -> list[tuple[int, list[Message]]]:
    """挖知识用:每个会话及其按序消息(供拼成对话文本喂 LLM)。"""
    async with db.async_session() as s:
        conv_ids = list(
            (await s.execute(select(Conversation.id).order_by(Conversation.id))).scalars()
        )
        out: list[tuple[int, list[Message]]] = []
        for cid in conv_ids:
            msgs = list(
                (await s.execute(
                    select(Message).where(Message.conversation_id == cid).order_by(Message.id)
                )).scalars()
            )
            out.append((cid, msgs))
        return out


# ---- ch09 数据飞轮(待审队列 + 评估轮次)----


async def fetch_unmatched_low_conf(limit: int) -> list[LowConfidenceQuestion]:
    """飞轮处理游标:尚未归并(matched_review_id IS NULL)的池内问题,按 id 升序。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(LowConfidenceQuestion)
            .where(LowConfidenceQuestion.matched_review_id.is_(None))
            .order_by(LowConfidenceQuestion.id).limit(limit)
        )
        return list(result.scalars())


async def list_review_candidates(limit: int = 200) -> list[dict]:
    """查重候选:全部状态的缺口行(用户拍板:比全部,驳回即终审),updated_at 倒序截断防 token 爆。"""
    async with db.async_session() as s:
        result = await s.execute(
            select(ReviewQueue.id, ReviewQueue.normalized_question)
            .order_by(ReviewQueue.updated_at.desc()).limit(limit)
        )
        return [{"id": r.id, "normalized_question": r.normalized_question} for r in result]


async def insert_review_item(normalized_question: str, ai_suggested_answer: str | None) -> int:
    async with db.async_session() as s:
        row = ReviewQueue(normalized_question=normalized_question,
                          ai_suggested_answer=ai_suggested_answer)
        s.add(row)
        await s.commit()
        return row.id


async def increment_occurrence(review_id: int) -> None:
    """查重命中:只累加次数,状态不动(命中已驳回/已通过也一样——驳回即终审)。
    原子 UPDATE,不走读改写,两批飞轮重叠时不丢计数。"""
    async with db.async_session() as s:
        await s.execute(
            update(ReviewQueue)
            .where(ReviewQueue.id == review_id)
            .values(occurrence_count=ReviewQueue.occurrence_count + 1)
        )
        await s.commit()


async def set_matched_review(lcq_id: int, review_id: int) -> None:
    async with db.async_session() as s:
        row = await s.get(LowConfidenceQuestion, lcq_id)
        if row is not None:
            row.matched_review_id = review_id
            await s.commit()


async def list_review_queue(status: str | None) -> list[ReviewQueue]:
    """审核页列表:按出现次数降序(频次=优先级),同频新的在前。"""
    async with db.async_session() as s:
        q = select(ReviewQueue).order_by(
            ReviewQueue.occurrence_count.desc(), ReviewQueue.id.desc())
        if status:
            q = q.where(ReviewQueue.review_status == status)
        return list((await s.execute(q)).scalars())


async def get_review_detail(review_id: int) -> tuple[ReviewQueue, list[LowConfidenceQuestion]] | None:
    """详情:缺口行 + 归并进来的原话流水(带 source/快照,审核人判断真缺还是没检到)。"""
    async with db.async_session() as s:
        item = await s.get(ReviewQueue, review_id)
        if item is None:
            return None
        raws = list((await s.execute(
            select(LowConfidenceQuestion)
            .where(LowConfidenceQuestion.matched_review_id == review_id)
            .order_by(LowConfidenceQuestion.id)
        )).scalars())
        return item, raws


async def update_review_status(review_id: int, status: str, approved_answer: str | None = None) -> bool:
    """仅「待审」可流转(通过/驳回都是终态);返回是否真的更新了。"""
    async with db.async_session() as s:
        row = await s.get(ReviewQueue, review_id)
        if row is None or row.review_status != "待审":
            return False
        row.review_status = status
        if approved_answer is not None:
            row.approved_answer = approved_answer
        await s.commit()
        return True


async def delete_knowledge_chunks(ids: list[int]) -> None:
    """审核写回失败的回滚:删掉本次插入的 pending chunk,防重试造出重复知识条目。"""
    if not ids:
        return
    async with db.async_session() as s:
        for i in ids:
            row = await s.get(KnowledgeChunk, i)
            if row is not None:
                await s.delete(row)
        await s.commit()


async def insert_eval_run(triggered_by: str, dataset_size: int, metrics: dict) -> int:
    async with db.async_session() as s:
        row = EvalRun(triggered_by=triggered_by, dataset_size=dataset_size, metrics=metrics)
        s.add(row)
        await s.commit()
        return row.id


async def list_eval_runs(limit: int = 10) -> list[EvalRun]:
    async with db.async_session() as s:
        result = await s.execute(select(EvalRun).order_by(EvalRun.id.desc()).limit(limit))
        return list(result.scalars())


# ---- ch08 工具审计 ----


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


# ---------- ch10 主题分类 ----------

def _pool_text_stmt():
    """池问题取文本的公共查询:优先归并后的标准化问法,没归并退回原话。"""
    return (
        select(LowConfidenceQuestion.id, LowConfidenceQuestion.raw_question,
               ReviewQueue.normalized_question)
        .outerjoin(ReviewQueue, LowConfidenceQuestion.matched_review_id == ReviewQueue.id)
        .order_by(LowConfidenceQuestion.id)
    )


async def list_pool_texts() -> list[dict]:
    """ch10 训练语料捞取:全量低置信度问题。"""
    async with db.async_session() as s:
        rows = (await s.execute(_pool_text_stmt())).all()
    return [{"question_id": qid, "text": norm or raw} for qid, raw, norm in rows]


async def list_unclassified_questions(limit: int = 500) -> list[dict]:
    """ch10 旁路批处理捞取:尚未归类、且已归并的低置信度问题。
    分类器只吃归并阶段产出的标准化问法;未归并(无 matched_review_id)的
    不进分类,留到下一轮归并后再归——保证输入永远是语义完整的单句,不喂原话。"""
    stmt = (
        _pool_text_stmt()
        .outerjoin(TopicClassification,
                   TopicClassification.question_id == LowConfidenceQuestion.id)
        .where(TopicClassification.id.is_(None))
        .where(LowConfidenceQuestion.matched_review_id.is_not(None))
        .limit(limit)
    )
    async with db.async_session() as s:
        rows = (await s.execute(stmt)).all()
    return [{"question_id": qid, "text": norm or raw} for qid, raw, norm in rows]


async def insert_topic_classifications(rows: list[dict]) -> int:
    """批量写归类结果;rows: [{question_id, labels}]。"""
    async with db.async_session() as s:
        s.add_all([TopicClassification(question_id=r["question_id"], labels=r["labels"])
                   for r in rows])
        await s.commit()
    return len(rows)


async def topic_distribution(samples_per_class: int = 3) -> dict:
    """主题分布:17 类各自问题量 + 每类样例;labels JSON 在 Python 侧聚合(量级小)。"""
    from app.core.taxonomy import TOPIC_NAMES

    stmt = (
        select(TopicClassification.labels, LowConfidenceQuestion.raw_question,
               ReviewQueue.normalized_question, TopicClassification.classified_at)
        .join(LowConfidenceQuestion,
              TopicClassification.question_id == LowConfidenceQuestion.id)
        .outerjoin(ReviewQueue, LowConfidenceQuestion.matched_review_id == ReviewQueue.id)
    )
    async with db.async_session() as s:
        rows = (await s.execute(stmt)).all()
    counts = {name: 0 for name in TOPIC_NAMES}
    samples: dict[str, list[str]] = {name: [] for name in TOPIC_NAMES}
    latest = None
    classified_count = 0
    for labels, raw, norm, ts in rows:
        text = norm or raw
        valid_labels = [lb for lb in (labels or []) if lb in counts]
        if valid_labels:
            classified_count += 1
            latest = ts if latest is None or ts > latest else latest
        for lb in valid_labels:
            counts[lb] += 1
            # 样例去重:归并后同一句标准化问法会对应池里好几行,三条样例都一样就白给了
            if len(samples[lb]) < samples_per_class and text not in samples[lb]:
                samples[lb].append(text)
    return {
        "total": classified_count,
        "latest": latest.isoformat() if latest else None,
        "classes": [{"label": n, "count": counts[n], "samples": samples[n]}
                    for n in TOPIC_NAMES],
    }


async def topic_questions(label: str, page: int = 1, size: int = 20) -> dict:
    """某一类目下已归类的问题,分页。总数与分布页那张图同源,都是数 labels 命中。

    labels 是 JSON 数组,筛选与分页都在 Python 侧做:归类总量是问题池规模(百级),
    一次全捞不贵,换来的是不依赖 MySQL 的 JSON 函数,sqlite 上跑测试也一致。
    """
    stmt = (
        select(TopicClassification.question_id, TopicClassification.labels,
               TopicClassification.classified_at, LowConfidenceQuestion.raw_question,
               LowConfidenceQuestion.source, LowConfidenceQuestion.created_at,
               ReviewQueue.normalized_question, ReviewQueue.occurrence_count,
               ReviewQueue.review_status)
        .join(LowConfidenceQuestion,
              TopicClassification.question_id == LowConfidenceQuestion.id)
        .outerjoin(ReviewQueue, LowConfidenceQuestion.matched_review_id == ReviewQueue.id)
        .order_by(TopicClassification.question_id.desc())
    )
    async with db.async_session() as s:
        rows = (await s.execute(stmt)).all()

    # Historical classifications may still be stored in the shared database.
    # Only current on-call taxonomy labels are exposed by the active dashboard.
    from app.core.taxonomy import TOPIC_NAMES
    if label not in TOPIC_NAMES:
        return {"label": label, "total": 0, "page": 1, "pages": 1, "items": []}
    hit = [r for r in rows if label in (r[1] or [])]
    size = max(1, min(size, 100))
    pages = max(1, -(-len(hit) // size))
    page = max(1, min(page, pages))
    items = [
        {"question_id": qid, "labels": labels,
         "text": norm or raw, "raw_question": raw,
         "normalized": norm is not None,
         "source": source, "occurrence_count": occ,
         "review_status": status,
         "asked_at": asked.isoformat() if asked else None,
         "classified_at": ts.isoformat() if ts else None}
        for qid, labels, ts, raw, source, asked, norm, occ, status
        in hit[(page - 1) * size: page * size]
    ]
    return {"label": label, "total": len(hit), "page": page, "size": size,
            "pages": pages, "items": items}


# ---- ch04 编造个案台账 ----


async def upsert_faith_case(eval_id: str, bucket: str, query: str, answer: str, reason: str,
                            citations: list | None = None, strategy: str = "hybrid_rerank",
                            judge_model: str | None = None) -> tuple[int, bool]:
    """一题一行:已有就更新成最近一次并 seen_count+1,返回 (id, 是否复发)。

    复发 = 这条之前被标过「已解决 / 无需解决」,现在又被判编造。那不是新问题,而是上次
    没改对(或者改动被回退),所以状态退回「未解决」重新进待处理列表——resolved_at 留着,
    列表上据此打「复发」标。
    """
    async with db.async_session() as s:
        row = (await s.execute(
            select(FaithCase).where(FaithCase.eval_id == eval_id))).scalar_one_or_none()
        now = datetime.now()
        if row is None:
            row = FaithCase(eval_id=eval_id, bucket=bucket, query=query, answer=answer,
                            reason=reason, citations=citations, strategy=strategy,
                            judge_model=judge_model, status="未解决", seen_count=1,
                            first_seen_at=now, last_seen_at=now)
            s.add(row)
            await s.commit()
            return row.id, False
        reopened = row.status != "未解决"
        row.bucket, row.query, row.answer, row.reason = bucket, query, answer, reason
        row.strategy, row.judge_model = strategy, judge_model
        if citations is not None:
            row.citations = citations
        row.seen_count = (row.seen_count or 0) + 1
        row.last_seen_at = now
        row.status = "未解决"
        await s.commit()
        return row.id, reopened


async def list_faith_cases(status: str | None = None, page: int = 1,
                           size: int = 5) -> tuple[list[FaithCase], int, dict]:
    """分页列表 + 三个状态各自的条数。未解决排前面,同状态内最近判出的排前面。"""
    async with db.async_session() as s:
        counts = {k: 0 for k in ("未解决", "已解决", "无需解决")}
        for st, n in (await s.execute(
                select(FaithCase.status, func.count()).group_by(FaithCase.status))).all():
            counts[st] = int(n)
        q = select(FaithCase)
        if status:
            q = q.where(FaithCase.status == status)
        total = int((await s.execute(
            select(func.count()).select_from(q.subquery()))).scalar_one())
        rows = (await s.execute(
            q.order_by(case((FaithCase.status == "未解决", 0), else_=1),
                       FaithCase.last_seen_at.desc(), FaithCase.id.desc())
            .limit(size).offset(max(0, (page - 1) * size)))).scalars()
        return list(rows), total, counts


async def faith_case_status_map() -> dict[str, str]:
    """eval_id → 处置状态。给幻觉率算「本轮」用:报告只给这一轮判出了哪几题,
    每题现在是什么处置得回台账查——台账是跨轮累计的,不能直接拿它的总数当本轮判出。"""
    async with db.async_session() as s:
        rows = (await s.execute(select(FaithCase.eval_id, FaithCase.status))).all()
        return {eid: st for eid, st in rows}


async def set_faith_case_status(case_id: int, status: str,
                                resolution: str | None = None) -> FaithCase | None:
    """人工处置:已解决 / 无需解决 / 退回未解决。

    处置要留交代:标「已解决」得写清怎么解决的,标「无需解决」得写清为什么不用改——
    半年后回头看,一个没写理由的「无需解决」和没处理过没区别。校验在 API 层(能回 400),
    这里只负责落库:退回未解决时连 resolved_at 和说明一起清掉。
    """
    async with db.async_session() as s:
        row = await s.get(FaithCase, case_id)
        if row is None:
            return None
        row.status = status
        if status == "未解决":
            row.resolved_at, row.resolution = None, None
        else:
            row.resolved_at = datetime.now()
            row.resolution = resolution
        await s.commit()
        await s.refresh(row)
        return row


# ---------- 检测靶子(外接数据源注册表) ----------

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


async def list_targets() -> list[Target]:
    async with db.async_session() as s:
        rows = (await s.execute(select(Target).order_by(Target.id))).scalars().all()
        return list(rows)


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


# ---------- 修复连接器(注册令牌 + 连接器身份) ----------

async def ensure_repair_tables() -> None:
    """修复连接器五张表启动自建(同 targets 范式:checkfirst,已有表不碰)。
    已有表只补列,不改旧列——计划表新增 conversation_id 时老库能平滑接上。"""
    from sqlalchemy import inspect, text

    tables = (RepairEnrollmentToken.__table__, RepairConnector.__table__,
              RepairPlan.__table__, RepairApproval.__table__,
              RepairExecution.__table__, RepairAuditLog.__table__)

    def _ensure(sync_conn) -> None:
        for table in tables:
            table.create(sync_conn, checkfirst=True)
        insp = inspect(sync_conn)
        have = {c["name"] for c in insp.get_columns("repair_plans")}
        if "conversation_id" not in have:
            sync_conn.execute(text("ALTER TABLE repair_plans ADD COLUMN conversation_id BIGINT"))

    async with db.engine.begin() as conn:
        await conn.run_sync(_ensure)


async def create_enrollment_token(token_id: str, target_id: int, token_hash: str,
                                  expires_at: datetime) -> None:
    async with db.async_session() as s:
        s.add(RepairEnrollmentToken(token_id=token_id, target_id=target_id,
                                    token_hash=token_hash, expires_at=expires_at))
        await s.commit()


async def get_enrollment_token(token_id: str) -> RepairEnrollmentToken | None:
    async with db.async_session() as s:
        return await s.get(RepairEnrollmentToken, token_id)


async def mark_enrollment_used(token_id: str) -> None:
    async with db.async_session() as s:
        row = await s.get(RepairEnrollmentToken, token_id)
        if row is not None and row.used_at is None:
            row.used_at = datetime.now()
            await s.commit()


async def create_repair_connector(connector_id: str, target_id: int, name: str,
                                  secret_hash: str, capabilities: list,
                                  host_info: dict) -> None:
    async with db.async_session() as s:
        s.add(RepairConnector(id=connector_id, target_id=target_id, name=name,
                              status="online", secret_hash=secret_hash,
                              capabilities=capabilities, host_info=host_info,
                              last_seen_at=datetime.now(),
                              created_at=datetime.now()))
        await s.commit()


async def get_repair_connector(connector_id: str) -> RepairConnector | None:
    async with db.async_session() as s:
        return await s.get(RepairConnector, connector_id)


async def list_repair_connectors(target_id: int | None = None) -> list[RepairConnector]:
    async with db.async_session() as s:
        stmt = select(RepairConnector).order_by(RepairConnector.created_at)
        if target_id is not None:
            stmt = stmt.where(RepairConnector.target_id == target_id)
        return list((await s.execute(stmt)).scalars().all())


async def touch_repair_connector(connector_id: str, capabilities: list | None = None,
                                 host_info: dict | None = None) -> None:
    """心跳:刷新最后在线时间;能力清单以连接器最近一次上报为准(它升级了适配器要能反映出来)。"""
    async with db.async_session() as s:
        row = await s.get(RepairConnector, connector_id)
        if row is None:
            return
        row.last_seen_at = datetime.now()
        if row.status != "revoked":
            row.status = "online"
        if capabilities is not None:
            row.capabilities = capabilities
        if host_info is not None:
            row.host_info = host_info
        await s.commit()


async def revoke_repair_connector(connector_id: str) -> bool:
    async with db.async_session() as s:
        row = await s.get(RepairConnector, connector_id)
        if row is None:
            return False
        row.status = "revoked"
        row.revoked_at = datetime.now()
        await s.commit()
        return True


async def delete_repair_connectors_of_target(target_id: int) -> None:
    """断开平台时一并清掉它的连接器记录(令牌随平台失效,不留悬空身份)。
    计划和执行记录保留:那是审计账,不跟着配置一起删。"""
    async with db.async_session() as s:
        await s.execute(delete(RepairConnector).where(RepairConnector.target_id == target_id))
        await s.execute(delete(RepairEnrollmentToken)
                        .where(RepairEnrollmentToken.target_id == target_id))
        await s.commit()


# ---------- 修复计划 / 审批 / 执行 ----------

async def create_repair_plan(row: dict) -> None:
    async with db.async_session() as s:
        s.add(RepairPlan(**row))
        await s.commit()


async def get_repair_plan(plan_id: str) -> RepairPlan | None:
    async with db.async_session() as s:
        return await s.get(RepairPlan, plan_id)


async def list_repair_plans(target_id: int, limit: int = 10) -> list[RepairPlan]:
    async with db.async_session() as s:
        stmt = (select(RepairPlan).where(RepairPlan.target_id == target_id)
                .order_by(RepairPlan.created_at.desc()).limit(limit))
        return list((await s.execute(stmt)).scalars().all())


async def set_repair_plan_status(plan_id: str, status: str) -> None:
    async with db.async_session() as s:
        row = await s.get(RepairPlan, plan_id)
        if row is not None:
            row.status = status
            await s.commit()


async def create_repair_approval(row: dict) -> None:
    async with db.async_session() as s:
        s.add(RepairApproval(**row))
        await s.commit()


async def get_repair_approval(approval_id: str) -> RepairApproval | None:
    async with db.async_session() as s:
        return await s.get(RepairApproval, approval_id)


async def set_repair_approval_status(approval_id: str, status: str) -> None:
    async with db.async_session() as s:
        row = await s.get(RepairApproval, approval_id)
        if row is not None:
            row.status = status
            await s.commit()


async def create_repair_execution(row: dict) -> None:
    async with db.async_session() as s:
        s.add(RepairExecution(**row))
        await s.commit()


async def finish_repair_execution(execution_id: str, status: str, result: dict | None,
                                  verification: dict | None, error: str | None) -> None:
    async with db.async_session() as s:
        row = await s.get(RepairExecution, execution_id)
        if row is None:
            return
        row.status = status
        row.result = result
        row.verification = verification
        row.error = (error or "")[:500] or None
        row.finished_at = datetime.now()
        await s.commit()


async def get_repair_execution(execution_id: str) -> RepairExecution | None:
    async with db.async_session() as s:
        return await s.get(RepairExecution, execution_id)


async def latest_execution_for_plan(plan_id: str) -> RepairExecution | None:
    async with db.async_session() as s:
        stmt = (select(RepairExecution).where(RepairExecution.plan_id == plan_id)
                .order_by(RepairExecution.started_at.desc()).limit(1))
        return (await s.execute(stmt)).scalars().first()


async def latest_repair_execution(target_id: int, service: str,
                                  action: str) -> RepairExecution | None:
    """同一平台同一服务同一动作的最近一次执行——冷却时间靠它判,防重启循环。"""
    async with db.async_session() as s:
        stmt = (select(RepairExecution)
                .join(RepairPlan, RepairPlan.id == RepairExecution.plan_id)
                .where(RepairPlan.target_id == target_id,
                       RepairPlan.service == service,
                       RepairPlan.action == action)
                .order_by(RepairExecution.started_at.desc()).limit(1))
        return (await s.execute(stmt)).scalars().first()


# ---------- 修复审计 ----------

async def add_repair_audit(event_type: str, *, target_id: int | None = None,
                           connector_id: str | None = None, plan_id: str | None = None,
                           execution_id: str | None = None, actor: str = "admin",
                           payload: dict | None = None) -> None:
    """审计只记 ID、状态和安全摘要;失败不抛——审计写不进去不该拦业务。"""
    try:
        async with db.async_session() as s:
            s.add(RepairAuditLog(target_id=target_id, connector_id=connector_id,
                                 plan_id=plan_id, execution_id=execution_id,
                                 actor=actor, event_type=event_type,
                                 payload=payload, created_at=datetime.now()))
            await s.commit()
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("app").warning("修复审计写入失败 event=%s", event_type,
                                         exc_info=True)


async def list_repair_audit(target_id: int, limit: int = 20) -> list[RepairAuditLog]:
    async with db.async_session() as s:
        stmt = (select(RepairAuditLog).where(RepairAuditLog.target_id == target_id)
                .order_by(RepairAuditLog.created_at.desc(), RepairAuditLog.id.desc())
                .limit(limit))
        return list((await s.execute(stmt)).scalars().all())


# ---------- API 设置(app_settings 覆盖项 + 嵌入指纹) ----------

async def ensure_settings_table() -> None:
    """app_settings 启动自建(同 ensure_targets_table 范式:checkfirst,只补建不迁移)。"""
    async with db.engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: AppSetting.__table__.create(sync_conn, checkfirst=True))


async def get_settings_map() -> dict[str, str]:
    async with db.async_session() as s:
        rows = (await s.execute(select(AppSetting))).scalars().all()
        return {r.skey: r.svalue for r in rows}


async def upsert_settings(items: dict[str, str]) -> None:
    """逐键 upsert:只写传进来的键,不碰别的覆盖项。"""
    async with db.async_session() as s:
        for k, v in items.items():
            row = await s.get(AppSetting, k)
            if row is None:
                s.add(AppSetting(skey=k, svalue=v))
            else:
                row.svalue = v
        await s.commit()


async def mark_all_chunks_pending() -> int:
    """嵌入配置变更后的全量重嵌前置:全部 chunk 置 pending(向量作废由 drop 集合负责)。
    返回重置的块数,给前端确认弹窗与后台任务日志用。"""
    async with db.async_session() as s:
        result = await s.execute(update(KnowledgeChunk).values(vectorize_status="pending"))
        await s.commit()
        return result.rowcount or 0


# ---- 诊断任务存档(卡片持久化:进程重启后卡片回放/轮询仍能拿到完整结论)----

async def ensure_diagnosis_table() -> None:
    """diagnosis_runs 启动自建(同 ensure_settings_table 范式:checkfirst,只补建不迁移)。
    新增的 platform / confirmation 两列用 ALTER 补——老库里已有诊断存档,不能重建。"""
    from sqlalchemy import inspect, text

    def _ensure(sync_conn) -> None:
        DiagnosisRun.__table__.create(sync_conn, checkfirst=True)
        insp = inspect(sync_conn)
        have = {c["name"] for c in insp.get_columns("diagnosis_runs")}
        add = {"platform": "VARCHAR(128)",
               "confirmation": "VARCHAR(16) DEFAULT 'pending'",
               "confirmed_at": "DATETIME",
               "confirmed_by": "VARCHAR(64)"}
        for name, ddl in add.items():
            if name not in have:
                sync_conn.execute(text(f"ALTER TABLE diagnosis_runs ADD COLUMN {name} {ddl}"))

    async with db.engine.begin() as conn:
        await conn.run_sync(_ensure)


async def ensure_incident_tables() -> None:
    """incidents / incident_events 启动自建(同 ensure_diagnosis_table 范式:
    checkfirst,只补建不迁移)。"""
    from app.db.models import Incident, IncidentEvent

    def _ensure(sync_conn) -> None:
        Incident.__table__.create(sync_conn, checkfirst=True)
        IncidentEvent.__table__.create(sync_conn, checkfirst=True)

    async with db.engine.begin() as conn:
        await conn.run_sync(_ensure)


async def save_diagnosis_run(run: dict) -> None:
    """诊断终态落库(整单覆盖式 upsert,同一 id 只有一份终态)。失败不抛——存档是增强,
    不能反过来弄死诊断任务本身,调用方已自行兜底。"""
    import logging

    row = {
        "id": run["id"], "conversation_id": run.get("conversation_id"),
        "service": run.get("service") or "", "symptom": run.get("symptom"),
        "platform": run.get("platform"),
        "status": run.get("status") or "done",
        "verdict": (run.get("result") or {}).get("verdict"),
        "root_cause_service": (run.get("result") or {}).get("root_cause_service"),
        "confirmation": run.get("confirmation") or "pending",
        "events": run.get("events") or [], "result": run.get("result"),
    }
    try:
        async with db.async_session() as s:
            await s.merge(DiagnosisRun(**row))
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logging.getLogger("app").warning("诊断任务 %s 落库失败(%s),卡片仅本次进程内可见",
                                         run.get("id"), type(e).__name__)


async def get_diagnosis_run(diag_id: str) -> dict | None:
    """按 id 取存档,重建成 API 同形的 run 载荷(内存没有时兜底用)。"""
    async with db.async_session() as s:
        row = await s.get(DiagnosisRun, diag_id)
    if row is None:
        return None
    return {"id": row.id, "service": row.service, "symptom": row.symptom or "",
            "platform": row.platform,
            "status": row.status, "stage": "done" if row.status == "done" else "failed",
            "confirmation": row.confirmation or "pending",
            "events": row.events or [], "result": row.result or {},
            "conversation_id": row.conversation_id,
            "created_at": row.created_at.isoformat() if row.created_at else None}


async def confirm_diagnosis(diag_id: str, confirmed: bool, by: str = "admin") -> dict | None:
    """记录用户对根因的确认/否认。未确认的结论不得用来启动修复(计划创建会校验)。"""
    async with db.async_session() as s:
        row = await s.get(DiagnosisRun, diag_id)
        if row is None:
            return None
        row.confirmation = "confirmed" if confirmed else "rejected"
        row.confirmed_at = datetime.now()
        row.confirmed_by = by
        await s.commit()
        return {"id": row.id, "confirmation": row.confirmation,
                "confirmed_at": row.confirmed_at.isoformat(timespec="seconds")}


async def find_confirmed_diagnosis(conversation_id: int,
                                   service: str) -> DiagnosisRun | None:
    """这个会话里、针对这个服务、用户已确认的最近一次诊断——计划创建的门槛。"""
    async with db.async_session() as s:
        stmt = (select(DiagnosisRun)
                .where(DiagnosisRun.conversation_id == conversation_id,
                       DiagnosisRun.root_cause_service == service,
                       DiagnosisRun.confirmation == "confirmed",
                       DiagnosisRun.status == "done")
                .order_by(DiagnosisRun.created_at.desc()).limit(1))
        return (await s.execute(stmt)).scalars().first()


async def find_latest_confirmed_diagnosis(conversation_id: int) -> DiagnosisRun | None:
    """会话内最近一次已确认根因的诊断(修复 agent 派单用,不限服务——用户说"修复"
    时不该逼他再报一遍服务名,最近确认的那单就是要修的)。"""
    async with db.async_session() as s:
        stmt = (select(DiagnosisRun)
                .where(DiagnosisRun.conversation_id == conversation_id,
                       DiagnosisRun.confirmation == "confirmed",
                       DiagnosisRun.status == "done",
                       DiagnosisRun.verdict.in_(("root_cause", "critical_failure")))
                .order_by(DiagnosisRun.created_at.desc()).limit(1))
        return (await s.execute(stmt)).scalars().first()


# ---- 修复 agent 任务存档(修复卡片持久化:同 diagnosis_runs 范式)----

async def ensure_repair_runs_table() -> None:
    """repair_runs 启动自建(同 ensure_diagnosis_table 范式:checkfirst,只补建不迁移)。"""
    async with db.engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: RepairRun.__table__.create(sync_conn, checkfirst=True))


async def save_repair_run(run: dict) -> None:
    """修复终态落库(整单覆盖式 upsert)。失败不抛——存档是增强,不能弄死修复任务。"""
    import logging
    row = {
        "id": run["id"], "conversation_id": run.get("conversation_id"),
        "diagnosis_id": run.get("diagnosis_id"),
        "service": run.get("service") or "", "platform": run.get("platform"),
        "status": run.get("status") or "done",
        "verdict": (run.get("result") or {}).get("verdict"),
        "action": (run.get("result") or {}).get("action"),
        "events": run.get("events") or [], "result": run.get("result"),
    }
    try:
        async with db.async_session() as s:
            await s.merge(RepairRun(**row))
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logging.getLogger("app").warning("修复任务 %s 落库失败(%s),卡片仅本次进程内可见",
                                         run.get("id"), type(e).__name__)


async def get_repair_run(repair_id: str) -> dict | None:
    """按 id 取存档,重建成 API 同形的 run 载荷(内存没有时兜底用)。"""
    async with db.async_session() as s:
        row = await s.get(RepairRun, repair_id)
    if row is None:
        return None
    return {"id": row.id, "service": row.service, "platform": row.platform,
            "diagnosis_id": row.diagnosis_id,
            "status": row.status, "stage": "done" if row.status == "done" else "failed",
            "events": row.events or [], "result": row.result or {},
            "conversation_id": row.conversation_id,
            "created_at": row.created_at.isoformat() if row.created_at else None}


async def latest_repair_run_for_diagnosis(diag_id: str) -> dict | None:
    """某诊断单最近一次修复任务(聊天卡片回放:刷新后修复卡靠它恢复)。"""
    async with db.async_session() as s:
        stmt = (select(RepairRun).where(RepairRun.diagnosis_id == diag_id)
                .order_by(RepairRun.created_at.desc()).limit(1))
        row = (await s.execute(stmt)).scalars().first()
    if row is None:
        return None
    return {"id": row.id, "status": row.status}


async def recent_incident_windows(since: datetime, until: datetime) -> list[tuple[float, float]]:
    """最近被判为真故障(root_cause/critical_failure)的诊断采样窗 [window_start, window_end]。

    用途:基线分布剔除这些时段。故障期的指标值会混进"过去 24 小时"的历史分布,把
    正常范围的估计整体抬高——同一天里第二次发生同款故障时,当前值落在被污染的
    "正常范围"内,粗筛失明(实测:早间注入的 cart 故障把历史错误率 max 抬到 0.33%,
    下午同款故障 0.14% 判成正常)。诊断存档本身就是"哪些时段出过事"的权威记录,
    拿它洗基线是零成本的。until 用来约束"只看诊断时刻之前已知的故障"(离线回放
    把时钟拨回过去时,不能拿未来才发生的故障洗过去的基线)。库不可读返回空——
    基线洗刷是增强,失败不拖垮粗筛。

    **按窗口时间筛,不按行创建时间筛**(2026-10-17 实测修正):created_at 由
    `server_default=func.now()` 落库——sqlite 上是 UTC,MySQL 上是会话时区,而窗口
    用的是本地 epoch,两者可能差整整一个时区。旧实现拿 created_at 当边界,实测在
    回放里把 8 小时**之后**才发生的故障窗也剔了(时钟拨回 14:42,却剔到 21:59 的窗),
    同时把 24 小时历史最早那 8 小时的故障窗漏掉——两个方向的错都直接改历史分布。
    现在只用一个宽松到能盖住任何时区差的粗筛把候选行捞出来,再按窗口自身的 epoch
    判"与 [since, until] 是否重叠",语义与时区都对齐。"""
    lo, hi = since.timestamp(), until.timestamp()
    margin = 86400          # 粗筛放宽一天:只用来少读行,不参与判定
    out: list[tuple[float, float]] = []
    try:
        async with db.async_session() as s:
            rows = (await s.execute(
                select(DiagnosisRun)
                .where(DiagnosisRun.verdict.in_(("root_cause", "critical_failure")))
                .where(DiagnosisRun.created_at >= since - timedelta(seconds=margin))
                .where(DiagnosisRun.created_at <= until + timedelta(seconds=margin))
            )).scalars().all()
        for r in rows:
            dq = ((r.result or {}).get("data_quality") or {}).get("trace") or {}
            ws, we = dq.get("window_start"), dq.get("window_end")
            if not (ws and we):
                continue
            ws, we = float(ws), float(we)
            if we <= ws:
                continue
            if we <= lo or ws >= hi:      # 与查询区间不重叠
                continue
            out.append((ws, we))
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("app").warning("诊断窗读取失败(%s),基线不剔除故障时段", e)
    return out


async def recent_change_signals(service: str, since: datetime, until: datetime) -> list[dict]:
    """窗内变更信号(本产品自己的库,不是外部发布系统):"有人动过/申请动过这个服务"的痕迹。
    - tickets:ticket_type='变更' 且描述里提到服务名的工单(变更表单落库,描述含服务名);
    - tool_audit_logs:submit_change 工具调用且 arguments.service_id 命中(审计表,含被拦下的)。
    服务匹配从宽:服务名是描述/参数的子串——宁可多报候选痕迹,由归因员结合证据判断,
    不在这里做精确闸。库不可读时返回空——变更维是证据增强,失败不拖垮诊断。"""
    out: list[dict] = []
    svc = (service or "").strip().lower()
    if not svc:
        return out
    try:
        async with db.async_session() as s:
            rows = (await s.execute(
                select(Ticket)
                .where(Ticket.ticket_type == "变更")
                .where(Ticket.created_at >= since)
                .where(Ticket.created_at <= until)
                .order_by(Ticket.created_at.desc())
                .limit(50))).scalars().all()
            for t in rows:
                if svc in (t.description or "").lower():
                    out.append({"kind": "change_ticket", "ts": t.created_at.isoformat(),
                                "detail": (t.description or "")[:160],
                                "status": t.status})
            audits = (await s.execute(
                select(ToolAuditLog)
                .where(ToolAuditLog.tool_name == "submit_change")
                .where(ToolAuditLog.created_at >= since)
                .where(ToolAuditLog.created_at <= until)
                .order_by(ToolAuditLog.created_at.desc())
                .limit(50))).scalars().all()
            for a in audits:
                args = a.arguments or {}
                if svc in str(args.get("service_id") or "").lower():
                    out.append({"kind": "change_request", "ts": a.created_at.isoformat(),
                                "detail": f"变更申请(status={a.status})",
                                "status": a.status})
    except Exception:  # noqa: BLE001  变更维尽力而为,失败不打断诊断
        import logging
        logging.getLogger("app").warning("变更信号查询失败 service=%s", service, exc_info=True)
        return []
    return sorted(out, key=lambda x: x["ts"], reverse=True)
