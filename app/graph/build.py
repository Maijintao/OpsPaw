from langgraph.graph import END, START, StateGraph
from app.graph import nodes
from app.graph.state import ConversationState
from app.graph.routing import route_by_intent
from app.graph.routing import should_continue

def build_graph(checkpointer=None):
    builder = StateGraph(ConversationState)
    builder.add_node("main_agent", nodes.main_agent)
    builder.add_node("log", nodes.log_node)
    builder.add_node("classify_intent", nodes.classify_intent)
    builder.add_node("script_reply", nodes.script_reply)
    builder.add_node("complaint_reply", nodes.complaint_reply)
    builder.add_node("resolve_reference", nodes.resolve_reference)
    builder.add_node("fetch_service", nodes.fetch_service)
    builder.add_node("retrieve_knowledge", nodes.retrieve_knowledge)
    builder.add_node("retrieve_runbook", nodes.retrieve_runbook)
    builder.add_edge(START, "resolve_reference")
    builder.add_edge("resolve_reference", "classify_intent")
    builder.add_conditional_edges("classify_intent", route_by_intent, {
        "escalate": "complaint_reply", "fallback_script": "script_reply",
        "knowledge": "retrieve_knowledge", "change_flow": "fetch_service",
        "service_ops": "main_agent"})
    builder.add_edge("retrieve_knowledge", "main_agent")
    builder.add_edge("fetch_service", "retrieve_runbook")
    builder.add_edge("retrieve_runbook", "main_agent")
    builder.add_edge("script_reply", "log")
    builder.add_edge("complaint_reply", "log")
    builder.add_node("agent_tools", nodes.agent_tools)
    builder.add_conditional_edges("main_agent", should_continue, {"continue": "agent_tools", "stop": "log"})
    builder.add_edge("agent_tools", "main_agent")
    builder.add_edge("log", END)
    from app.core.observability import attach_observability
    return attach_observability(builder.compile(checkpointer=checkpointer))
