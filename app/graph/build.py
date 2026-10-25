from langgraph.graph import END, START, StateGraph
from app.graph import nodes
from app.graph.state import ConversationState

def build_graph(checkpointer=None):
    builder = StateGraph(ConversationState)
    builder.add_node("main_agent", nodes.main_agent)
    builder.add_node("log", nodes.log_node)
    builder.add_edge(START, "main_agent")
    builder.add_edge("main_agent", "log")
    builder.add_edge("log", END)
    return builder.compile(checkpointer=checkpointer)
