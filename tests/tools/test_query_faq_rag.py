import pytest

from app.tools.builtin import faq


@pytest.mark.asyncio
async def test_query_faq_returns_evidence(monkeypatch):
    async def fake_understand(q): return {"standard": q, "expanded": ["告警阈值"]}
    monkeypatch.setattr("app.core.query_understanding.understand", fake_understand)
    async def fake_search(query, strategy="hybrid_rerank", top_k=None, category=None,
                          client=None, collection="knowledge", bm25_text=None):
        return [
            {"id": 1, "rerank_score": 0.9, "question": "告警阈值", "answer": "CPU 持续 5 分钟超过 80% 触发告警",
             "section_path": "值班规范", "content_type": "faq", "category": "值班规范"},
            {"id": 2, "rerank_score": 0.6, "question": "变更窗口", "answer": "每天 02:00-04:00",
             "section_path": "值班规范", "content_type": "policy", "category": "变更申请"},
        ]
    monkeypatch.setattr("app.core.retrieval.search_knowledge", fake_search)

    out = await faq.query_faq.ainvoke({"keyword": "邮费多少"})
    assert out["citations"][0]["n"] == 1 and out["citations"][0]["section_path"]
    assert "[1]" in out["evidence"]


@pytest.mark.asyncio
async def test_query_faq_zero_hit_returns_empty_evidence(monkeypatch):
    # 拒答闸已下线:无召回不再返回 sufficient=False,证据为空交 Agent 如实说明
    async def fake_understand(q): return {"standard": q, "expanded": []}
    monkeypatch.setattr("app.core.query_understanding.understand", fake_understand)
    async def fake_search(**kw): return []
    monkeypatch.setattr("app.core.retrieval.search_knowledge", lambda *a, **k: fake_search(**k))
    out = await faq.query_faq.ainvoke({"keyword": "火星车怎么买"})
    assert out["evidence"] == "" and out["citations"] == []


@pytest.mark.asyncio
async def test_query_faq_category_fallback(monkeypatch):
    # 模型猜错 category(与入库值不符)→ 过滤后空;应回退到不过滤重试并命中
    async def fake_understand(q): return {"standard": q, "expanded": []}
    monkeypatch.setattr("app.core.query_understanding.understand", fake_understand)

    async def fake_search(query, strategy="hybrid_rerank", top_k=None,
                          category=None, client=None, collection="knowledge", bm25_text=None):
        if category:  # 猜错的品类 → 精确过滤命中空
            return []
        return [{"id": 1, "rerank_score": 0.9, "question": "告警阈值", "answer": "CPU 持续 5 分钟超过 80% 触发告警",
                 "section_path": "值班规范", "content_type": "faq", "category": "值班规范"}]
    monkeypatch.setattr("app.core.retrieval.search_knowledge", fake_search)

    out = await faq.query_faq.ainvoke({"keyword": "邮费", "category": "不存在的品类"})
    assert out["citations"][0]["id"] == 1


@pytest.mark.asyncio
async def test_query_faq_low_score_still_returns_hits(monkeypatch):
    # 弱证据(分数低于旧拒答闸)也照常返回条目,答不答/怎么说明交 Agent 决定
    async def fake_understand(q): return {"standard": q, "expanded": []}
    monkeypatch.setattr("app.core.query_understanding.understand", fake_understand)
    async def fake_search(query, **kw):
        return [{"id": 1, "rerank_score": 0.12, "question": "告警阈值", "answer": "CPU 持续 5 分钟超过 80% 触发告警",
                 "section_path": "p", "content_type": "faq", "category": "值班规范"}]
    monkeypatch.setattr("app.core.retrieval.search_knowledge", fake_search)
    out = await faq.query_faq.ainvoke({"keyword": "Pro型号能自动铲屎吗"})
    assert out["citations"] and out["citations"][0]["id"] == 1
