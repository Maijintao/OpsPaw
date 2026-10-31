from sqlalchemy import select

from app.db.models import KnowledgeChunk, QaExtractionStaging


async def test_knowledge_chunk_roundtrip(db_session_factory):
    async with db_session_factory() as s:
        row = KnowledgeChunk(category="故障政策", questions="告警阈值", answer="CPU 持续 5 分钟超过 80% 触发告警")
        s.add(row)
        await s.commit()
        await s.refresh(row)
        assert row.id is not None
        assert row.vectorize_status == "pending"
        assert row.is_key_clause == 0


async def test_staging_roundtrip(db_session_factory):
    async with db_session_factory() as s:
        row = QaExtractionStaging(batch_no="b1", question="怎么扩容", answer="扩容需值班负责人审批")
        s.add(row)
        await s.commit()
        got = (await s.execute(select(QaExtractionStaging))).scalars().all()
        assert len(got) == 1
        assert got[0].status == "extracted"
