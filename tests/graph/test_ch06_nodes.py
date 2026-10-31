import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.graph import nodes


@pytest.mark.asyncio
async def test_classify_intent_writes_confidence_and_route(monkeypatch):
    async def fake_classify(query, history=""):
        return {"intent": "变更申请", "confidence": 0.83}
    monkeypatch.setattr(nodes.intent_mod, "classify", fake_classify)
    out = await nodes.classify_intent({"messages": [HumanMessage("3003 能重启吗")],
                                       "resolved_query": "服务 3003 还能申请重启吗"})
    assert out["intent"] == "变更申请"
    assert out["intent_confidence"] == 0.83
    assert out["route"] == "change_flow"                 # 变更申请 → change_flow
    assert out["trace"]["route"] == "change_flow"
    assert out["trace"]["intent_confidence"] == 0.83     # 不覆盖 confidence_check 的 confidence 键


def test_get_chat_model_honors_model_override():
    from app.core.llm import get_chat_model
    m = get_chat_model(model="glm-4-flash")
    assert m.model_name == "glm-4-flash"
    d = get_chat_model()
    from app.config import settings
    assert d.model_name == settings.chat_model


@pytest.mark.asyncio
async def test_resolve_reference_passthrough_when_complete(monkeypatch):
    async def fake_resolve(q, history=""):
        return q  # 已完整,原样
    monkeypatch.setattr(nodes.coref, "resolve", fake_resolve)
    out = await nodes.resolve_reference({"messages": [HumanMessage("蓝牙耳机的保修期多久")]})
    assert out["resolved_query"] == "蓝牙耳机的保修期多久"
    assert out["trace"]["coref"] == "passthrough"


@pytest.mark.asyncio
async def test_resolve_reference_rewrites_with_history(monkeypatch):
    async def fake_resolve(q, history=""):
        return "3003 还能申请重启吗"
    monkeypatch.setattr(nodes.coref, "resolve", fake_resolve)
    out = await nodes.resolve_reference({"messages": [
        HumanMessage("order-service 什么时候发布"), AIMessage("预计今晚"), HumanMessage("能回滚吗")]})
    assert out["resolved_query"] == "3003 还能申请重启吗"
    assert out["trace"]["coref"] == "rewrite"


# ---- Task 7: fetch_service + 缺单 interrupt(mock 时代的 _extract_service_id/
# list_user_services 已随真实服务数据层下线,现行语义见 tests 头部注释) ----

def test_match_service_name():
    # mock 时代的「4+位数字=服务编号」正则已删:真实服务用名字,从发现结果反查
    services = [{"service_id": "cartservice"}, {"service_id": "frontend"}]
    assert nodes._match_service_name("cartservice 的CPU曲线", services) == "cartservice"
    assert nodes._match_service_name("看看 Frontend", services) == "frontend"   # 大小写不敏感
    assert nodes._match_service_name("我要扩容", services) is None


async def _stub_discover(monkeypatch, services, error=None):
    async def fake():
        return services, error
    monkeypatch.setattr(nodes.service_catalog, "discover_services", fake)


@pytest.mark.asyncio
async def test_fetch_service_uses_name_in_query(monkeypatch):
    # 真实语义:resolved_query 里出现的服务名对准发现列表,快照同源
    services = [{"service_id": "cartservice", "name": "cartservice",
                 "target": "otel-demo", "status": None}]
    await _stub_discover(monkeypatch, services)

    async def fake_snapshot(sid, discovered=None):
        return {"service_id": sid, "status": "运行中", "reason": "指标与链路抽样均未见异常"}
    monkeypatch.setattr(nodes.service_catalog, "service_snapshot", fake_snapshot)

    out = await nodes.fetch_service({"resolved_query": "cartservice 能重启吗", "user_id": "u1"})
    assert out["service_id"] == "cartservice"
    assert out["service_data"]["service_id"] == "cartservice"
    assert out["trace"]["fetch_service"]["service_id"] == "cartservice"


@pytest.mark.asyncio
async def test_fetch_service_discovery_down_no_empty_picker(monkeypatch):
    # 平台不可达:不弹空选择器,错误装进 service_data 如实注入,让 Agent 告知用户
    await _stub_discover(monkeypatch, [], "接入平台均不可达:otel-demo(ConnectError)")
    out = await nodes.fetch_service({"resolved_query": "我要变更", "user_id": "u1"})
    assert out["service_data"]["code"] == "discovery_down"
    assert "不可达" in out["service_data"]["error"]
    assert "__interrupt__" not in out


@pytest.mark.asyncio
async def test_fetch_service_interrupts_when_missing(monkeypatch):
    # interrupt() 只能在编译图内跑(Task 1 冒烟 D:图外直接调是 RuntimeError),
    # 故缺服务名路径经最小编译图 + InMemorySaver ainvoke 测真实中断 surface。
    services = [{"service_id": "cartservice", "name": "cartservice",
                 "target": "otel-demo", "status": None}]
    await _stub_discover(monkeypatch, services)
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from app.graph.state import ConversationState
    b = StateGraph(ConversationState)
    b.add_node("fetch_service", nodes.fetch_service)
    b.add_edge(START, "fetch_service")
    b.add_edge("fetch_service", END)
    graph = b.compile(checkpointer=InMemorySaver())
    out = await graph.ainvoke({"resolved_query": "我要变更", "user_id": "u1"},
                              {"configurable": {"thread_id": "t-fetch"}})
    payload = out["__interrupt__"][0].value      # Task 1 冒烟 A/C 钉死此取法
    assert payload["type"] == "select_service"
    assert payload["services"][0]["service_id"] == "cartservice"


@pytest.mark.asyncio
async def test_fetch_service_报不在平台的服务也弹选择器(monkeypatch):
    # 变更子流程是确定性节点、不经 agent_tools,工具内那道存在性校验够不着它。
    # 用户报一个平台里查无此名的服务,不能照查,得跟「没给服务名」一样落回选择器。
    services = [{"service_id": "cartservice", "name": "cartservice",
                 "target": "otel-demo", "status": None}]
    await _stub_discover(monkeypatch, services)
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from app.graph.state import ConversationState
    b = StateGraph(ConversationState)
    b.add_node("fetch_service", nodes.fetch_service)
    b.add_edge(START, "fetch_service")
    b.add_edge("fetch_service", END)
    graph = b.compile(checkpointer=InMemorySaver())
    out = await graph.ainvoke({"resolved_query": "ghostservice 我要扩容", "user_id": "u1"},
                              {"configurable": {"thread_id": "t-foreign"}})
    payload = out["__interrupt__"][0].value
    assert payload["type"] == "select_service"                 # 没有照着 ghostservice 查下去
    assert [o["service_id"] for o in payload["services"]] == ["cartservice"]


@pytest.mark.asyncio
async def test_owns_service_is_existence_check(monkeypatch):
    # 真实语义:owns_service =「服务在接入平台中存在」的存在性校验(无 CMDB,归属暂退化)
    services = [{"service_id": "cartservice", "name": "cartservice",
                 "target": "otel-demo", "status": None}]
    await _stub_discover(monkeypatch, services)
    from app.tools import service_catalog
    assert await service_catalog.owns_service("u-42", "cartservice") is True
    assert await service_catalog.owns_service("u-42", "ghostservice") is False
    assert await service_catalog.owns_service("u-42", "") is False


# ---- Task 8: retrieve_runbook 扩写多查 + 去重合并 ----

@pytest.mark.asyncio
async def test_retrieve_runbook_expands_dedups_merges(monkeypatch):
    async def fake_expand(q):
        return ["告警阈值", "重启审批", "变更窗口"]
    # 三条 query 各自召回:id=1 在两条里出现(取高分 0.9),id=2 只在一条
    per_query = {
        "告警阈值": [{"id": 1, "question": "重启", "answer": "重启需值班负责人审批", "rerank_score": 0.7,
                     "section_path": "值班规范/变更审批", "content_type": "policy"}],
        "重启审批": [{"id": 1, "question": "重启", "answer": "重启需值班负责人审批", "rerank_score": 0.9,
                       "section_path": "值班规范/变更审批", "content_type": "policy"},
                      {"id": 2, "question": "告警", "answer": "CPU 持续 5 分钟超过 80% 触发告警", "rerank_score": 0.6,
                       "section_path": "值班规范/告警阈值", "content_type": "policy"}],
        "变更窗口": [],
    }
    async def fake_search(q, **k):
        return per_query.get(q, [])
    monkeypatch.setattr(nodes.query_understanding, "expand_queries", fake_expand)
    monkeypatch.setattr(nodes.retrieval, "search_knowledge", fake_search)
    monkeypatch.setattr(nodes.retrieval, "arrange_head_tail", lambda h: h)

    out = await nodes.retrieve_runbook({"resolved_query": "3003 这个服务能重启吗",
                                       "service_data": {"status": "运行中"}})
    assert len(out["citations"]) == 2                    # id=1 去重(取 0.9)+ id=2
    ids = [c["id"] for c in out["citations"]]
    assert ids == [1, 2]                                 # 按 rerank_score 降序(0.9, 0.6)
    assert "[1]" in out["evidence"] and "[2]" in out["evidence"]
    assert out["trace"]["retrieve_runbook"]["hits"] == 2
    assert out["trace"]["retrieve_runbook"]["queries"] == ["告警阈值", "重启审批", "变更窗口"]


# ---- Task 9: submit_change 拦截 + _agent_messages 变更 适配 ----

@pytest.mark.asyncio
async def test_agent_tools_intercepts_submit_change(monkeypatch):
    services = [{"service_id": "cartservice", "name": "cartservice",
                 "target": "otel-demo", "status": None}]
    await _stub_discover(monkeypatch, services)
    ai = AIMessage(content="", tool_calls=[
        {"id": "r1", "name": "submit_change", "args": {"service_id": "cartservice", "change_type": "重启服务"}}])
    out = await nodes.agent_tools({"messages": [ai], "user_id": "u1"})
    assert out["suggested_actions"][0] == {"type": "change_form",
                                           "draft": {"service_id": "cartservice", "change_type": "重启服务"}}
    tm = out["messages"][0]
    assert tm.name == "submit_change"                       # 合成 ToolMessage 促收敛
    assert "变更" in tm.content


def test_agent_messages_injects_service_and_policy_on_change():
    msgs = nodes._agent_messages({
        "route": "change_flow",
        "service_data": {"service_id": "1001", "status": "运行中", "product": "order-service"},
        "evidence": "[1] 告警阈值: 80%",
        "messages": [HumanMessage("这个服务能重启吗")]})
    assert isinstance(msgs[0], SystemMessage)
    assert "80%" not in msgs[0].content and "order-service" not in msgs[0].content  # 不进 system
    ctx = msgs[-1].content                                   # 本轮材料紧跟用户那句
    assert "80%" in ctx                                 # 政策证据
    assert "order-service" in ctx and "submit_change" in ctx       # 订单数据 + 判定指令
    # 顺序不能反:REFUND_JUDGE_HINT 的措辞假设证据已在前文给过
    assert ctx.index("80%") < ctx.index("submit_change")


def test_agent_messages_knowledge_path_still_injects_evidence():
    msgs = nodes._agent_messages({
        "route": "knowledge", "evidence": "[1] 告警阈值: 80%",
        "messages": [HumanMessage("告警阈值是多少")]})
    assert "80%" not in msgs[0].content                  # 不进 system
    assert "80%" in msgs[-1].content                     # 放宽条件后知识路不回归


# ---- Task 10: script_reply 按意图分文案 ----

@pytest.mark.asyncio
async def test_script_reply_by_intent():
    from app.core.prompts import SCRIPT_REPLY_CHITCHAT, SCRIPT_REPLY_OTHER
    chit = await nodes.script_reply({"intent": "闲聊"})
    assert chit["answer"] == SCRIPT_REPLY_CHITCHAT
    assert chit["trace"]["route"] == "fallback_script"
    other = await nodes.script_reply({"intent": "其他"})
    assert other["answer"] == SCRIPT_REPLY_OTHER
