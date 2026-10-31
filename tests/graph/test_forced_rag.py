import pytest
from langchain_core.messages import HumanMessage

from app.graph import nodes


@pytest.mark.asyncio
async def test_forced_rag_builds_evidence(monkeypatch):
    hits = [{"id": 1, "question": "告警阈值", "answer": "CPU 持续 5 分钟超过 80% 触发告警", "rerank_score": 0.9,
             "section_path": "值班规范/告警阈值", "content_type": "policy"}]
    monkeypatch.setattr(nodes.query_understanding, "understand",
                        _aval({"standard": "告警阈值", "expanded": []}))
    monkeypatch.setattr(nodes.retrieval, "search_knowledge", _aval(hits))
    monkeypatch.setattr(nodes.retrieval, "arrange_head_tail", lambda h: h)

    out = await nodes.retrieve_knowledge({"messages": [HumanMessage("告警阈值是多少")]})
    assert "[1]" in out["evidence"]
    assert out["citations"][0]["n"] == 1
    assert out["trace"]["forced_rag"] is True


@pytest.mark.asyncio
async def test_forced_rag_zero_hit_still_empty_evidence(monkeypatch):
    # 拒答闸已下线:零命中不再走 fallback 分支,证据为空照常进 Agent
    monkeypatch.setattr(nodes.query_understanding, "understand",
                        _aval({"standard": "注销账号", "expanded": []}))
    monkeypatch.setattr(nodes.retrieval, "search_knowledge", _aval([]))
    out = await nodes.retrieve_knowledge({"messages": [HumanMessage("怎么注销账号")]})
    assert out["evidence"] == ""
    assert out["trace"]["forced_rag"] is True


@pytest.mark.asyncio
async def test_classify_intent_sets_route_in_state(monkeypatch):
    # route 必须落进 State,否则 _agent_messages 判不出知识路、强制检索证据不会注入
    async def fake_classify(query, history=""):
        return {"intent": "变更申请", "confidence": 0.8}
    monkeypatch.setattr(nodes.intent_mod, "classify", fake_classify)
    out = await nodes.classify_intent({"messages": [HumanMessage("我要扩容")]})
    assert out["intent"] == "变更申请"
    assert out["route"] == "change_flow"      # ch06:变更申请 → change_flow 出口
    assert out["trace"]["route"] == "change_flow"


def _aval(value):
    async def _f(*a, **k):
        return value
    return _f
