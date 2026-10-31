"""拒答闸下线后的知识路检索行为:证据恒产出(弱也交 Agent),低置信只落池不拦路。"""
import pytest

from app.graph import nodes


def _mk_hits(*scores):
    return [{"id": i, "question": f"q{i}", "answer": f"a{i}", "rerank_score": s,
             "section_path": "s", "content_type": "faq"} for i, s in enumerate(scores)]


@pytest.fixture
def stub_retrieval(monkeypatch):
    async def fake_understand(q):
        return {"standard": q, "expanded": []}
    monkeypatch.setattr(nodes.query_understanding, "understand", fake_understand)

    def set_hits(hits):
        async def fake_search(*a, **k):
            return hits
        monkeypatch.setattr(nodes.retrieval, "search_knowledge", fake_search)
    return set_hits


def _state(text="猫窝能水洗吗"):
    from langchain_core.messages import HumanMessage
    return {"messages": [HumanMessage(text)], "conversation_id": 1}


async def test_low_confidence_still_returns_evidence_and_records_pool(
        stub_retrieval, monkeypatch):
    monkeypatch.setattr("app.config.settings.evidence_confidence_threshold", 0.5)
    stub_retrieval(_mk_hits(0.15, 0.14))          # 弱证据 → 不再拒答,照常交 Agent
    recorded = {}

    async def fake_insert(cid, q, source, reason, retrieved_chunks=None):
        recorded.update(cid=cid, q=q, source=source, reason=reason, chunks=retrieved_chunks)
        return 1
    monkeypatch.setattr(nodes.repository, "insert_low_confidence", fake_insert)

    out = await nodes.retrieve_knowledge(_state())
    assert out["evidence"]                        # 弱证据也产出编号证据
    assert out["citations"]
    assert out["evidence_confidence"] < 0.5
    assert "confidence_signals" in out["trace"]
    assert recorded["source"] == "retrieval_low_conf"
    assert recorded["chunks"] and recorded["chunks"][0]["question"] == "q0"
    assert "evidence_confidence=0." in recorded["reason"]


async def test_zero_hit_records_empty_snapshot(stub_retrieval, monkeypatch):
    """走了检索但零命中:证据为空、落池快照存 [](「已检索,零命中」强信号),不折叠成 NULL。"""
    monkeypatch.setattr("app.config.settings.evidence_confidence_threshold", 0.5)
    stub_retrieval([])
    recorded = {}

    async def fake_insert(cid, q, source, reason, retrieved_chunks=None):
        recorded["chunks"] = retrieved_chunks
        return 1
    monkeypatch.setattr(nodes.repository, "insert_low_confidence", fake_insert)

    out = await nodes.retrieve_knowledge(_state())
    assert out["evidence"] == "" and out["citations"] == []
    assert recorded["chunks"] == []               # 三态语义:[] ≠ NULL


async def test_strong_evidence_no_pool_write(stub_retrieval, monkeypatch):
    monkeypatch.setattr("app.config.settings.evidence_confidence_threshold", 0.3)
    stub_retrieval(_mk_hits(0.9, 0.4, 0.3))

    def _boom(*a, **k):
        raise AssertionError("强证据不应落池")
    monkeypatch.setattr(nodes.repository, "insert_low_confidence", _boom)

    out = await nodes.retrieve_knowledge(_state())
    assert len(out["retrieved_snapshot"]) == 3    # 走了检索就留快照,供事后 👎 回捞
    assert out["citations"]
