from app.db import repository


async def test_insert_and_list_pending(db_session_factory):
    cid = await repository.insert_knowledge_chunk("cat", "告警阈值", "CPU 持续 5 分钟超过 80% 触发告警", content_type="policy")
    pending = await repository.list_pending_chunks()
    assert [c.id for c in pending] == [cid]
    assert pending[0].questions == "告警阈值"


async def test_mark_vectorized_removes_from_pending(db_session_factory):
    cid = await repository.insert_knowledge_chunk("cat", "q", "a")
    await repository.mark_chunk_vectorized(cid, str(cid))
    assert await repository.list_pending_chunks() == []
    assert await repository.count_chunks_by_status("done") == 1


async def test_set_neighbors(db_session_factory):
    a = await repository.insert_knowledge_chunk("c", "qa", "aa")
    b = await repository.insert_knowledge_chunk("c", "qb", "ab")
    await repository.set_chunk_neighbors(b, prev_id=a, next_id=None)
    pending = {c.id: c for c in await repository.list_pending_chunks()}
    assert pending[b].prev_chunk_id == a


async def test_staging_flow(db_session_factory):
    i1 = await repository.insert_staging("b1", "conv:1", "怎么扩容", "扩容需值班负责人审批")
    await repository.insert_staging("b1", "conv:2", "怎么回滚", "回滚需值班负责人审批")
    extracted = await repository.list_staging_by_status("extracted")
    assert len(extracted) == 2
    await repository.set_staging_status([i1], "discarded")
    assert len(await repository.list_staging_by_status("extracted")) == 1
    assert len(await repository.list_staging_by_status("discarded")) == 1


async def test_list_all_questions(db_session_factory):
    await repository.insert_knowledge_chunk("c", "告警阈值是多少", "CPU 持续 5 分钟超过 80% 触发告警")
    assert "告警阈值是多少" in await repository.list_all_questions()
