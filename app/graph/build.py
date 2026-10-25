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
    builder.add_edge(START, "resolve_reference")
    builder.add_edge("resolve_reference", "classify_intent")
    builder.add_conditional_edges("classify_intent", route_by_intent, {
        "escalate": "complaint_reply", "fallback_script": "script_reply",
        "knowledge": "main_agent", "change_flow": "main_agent", "service_ops": "main_agent"})
    builder.add_edge("script_reply", "log")
    builder.add_edge("complaint_reply", "log")
    builder.add_node("agent_tools", nodes.agent_tools)
    builder.add_conditional_edges("main_agent", should_continue, {"continue": "agent_tools", "stop": "log"})
    builder.add_edge("agent_tools", "main_agent")
    builder.add_edge("log", END)
    return builder.compile(checkpointer=checkpointer)
