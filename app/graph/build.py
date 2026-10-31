from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.routing import route_by_intent, should_continue
from app.graph.state import ConversationState


def _builder() -> StateGraph:
    b = StateGraph(ConversationState)
    # 节点
    b.add_node("resolve_reference", nodes.resolve_reference)
    b.add_node("classify_intent", nodes.classify_intent)
    b.add_node("retrieve_knowledge", nodes.retrieve_knowledge)
    b.add_node("main_agent", nodes.main_agent)
    b.add_node("agent_tools", nodes.agent_tools)
    b.add_node("complaint_reply", nodes.complaint_reply)
    b.add_node("script_reply", nodes.script_reply)
    b.add_node("fetch_service", nodes.fetch_service)
    b.add_node("retrieve_runbook", nodes.retrieve_runbook)
    b.add_node("log", nodes.log_node)

    # 骨架:消解 → 意图 → 分流
    b.add_edge(START, "resolve_reference")
    b.add_edge("resolve_reference", "classify_intent")
    b.add_conditional_edges("classify_intent", route_by_intent, {
        "escalate": "complaint_reply",
        "fallback_script": "script_reply",
        "knowledge": "retrieve_knowledge",
        "change_flow": "fetch_service",
        "service_ops": "main_agent",
    })
    # 变更子流程确定性链:定位服务 → 检索 runbook → 交主力 Agent 判能不能变更
    b.add_edge("fetch_service", "retrieve_runbook")
    b.add_edge("retrieve_runbook", "main_agent")
    # 知识路:检索 → 直接进 Agent(生成前置信度闸已下线,弱证据交 Agent 带免责说明答)
    b.add_edge("retrieve_knowledge", "main_agent")
    # 主力 Agent ReAct 环
    b.add_conditional_edges("main_agent", should_continue, {
        "continue": "agent_tools",
        "stop": "log",
    })
    b.add_edge("agent_tools", "main_agent")
    # 确定性出口 → 日志 → END
    b.add_edge("complaint_reply", "log")
    b.add_edge("script_reply", "log")
    b.add_edge("log", END)
    return b


def build_graph(checkpointer=None):
    return _builder().compile(checkpointer=checkpointer)
