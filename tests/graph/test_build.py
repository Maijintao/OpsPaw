from app.graph.build import _builder


def test_graph_has_all_nodes():
    g = _builder().compile()
    names = set(g.get_graph().nodes)
    for n in ["resolve_reference", "classify_intent", "retrieve_knowledge",
              "main_agent", "agent_tools", "complaint_reply", "script_reply",
              "fetch_service", "retrieve_runbook", "log"]:
        assert n in names, f"缺节点 {n}"
    # 拒答闸已下线:生成前证据闸与兜底出口节点一并移除
    for gone in ["confidence_check", "fallback_reply"]:
        assert gone not in names, f"{gone} 应已随拒答闸下线移除"
    assert "chitchat_reply" not in names, "chitchat_reply 应已被 script_reply 取代"


def test_graph_compiles_with_checkpointer():
    from langgraph.checkpoint.memory import InMemorySaver
    g = _builder().compile(checkpointer=InMemorySaver())
    assert g is not None
