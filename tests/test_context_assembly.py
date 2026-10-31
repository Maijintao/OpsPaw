"""ch07 上下文拼装(纯函数级):拼装顺序固定、滑窗从摘要边界后接原文、无摘要不插额外 system。"""
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.graph.nodes import _agent_messages, _history_text


def _state(n_turns=3, summary="", upto=0):
    msgs = []
    db_id = 1
    for i in range(n_turns):
        msgs.append(HumanMessage(f"问题{i}", id=f"db-{db_id}"))
        msgs.append(AIMessage(f"回答{i}"))
        db_id += 2
    return {"messages": msgs, "summary": summary, "summary_upto_msg_id": upto}


def test_agent_messages_order_with_summary():
    """拼装顺序固定:人设 system 打头 → 滑窗原文 → 摘要与本轮材料紧跟最后一条用户消息。

    摘要不再单独占一条 system:上游模板会把所有 system 上提合并,摘要每会话不同,
    放 system 里会把工具 schema 挤到可变内容之后,前缀缓存整段作废(实测 2048→0)。
    """
    ms = _agent_messages(_state(summary="用户问过服务1001", upto=0))
    assert isinstance(ms[0], SystemMessage) and "值班猫" in ms[0].content   # 人设红线打头
    assert sum(isinstance(m, SystemMessage) for m in ms) == 1            # 全列表只此一条
    assert isinstance(ms[1], HumanMessage)
    ctx = [m for m in ms if "服务1001" in (m.content or "")]
    assert ctx and not isinstance(ctx[0], SystemMessage)                 # 摘要走用户侧


def test_agent_messages_no_summary_no_extra_system():
    ms = _agent_messages(_state(summary="", upto=0))
    assert isinstance(ms[0], SystemMessage)
    assert not isinstance(ms[1], SystemMessage)     # 无摘要不插第二条 system


def test_agent_messages_window_cut_by_boundary():
    ms = _agent_messages(_state(n_turns=6, summary="早期摘要", upto=6))
    win = [m for m in ms if not isinstance(m, SystemMessage)]
    assert win[0].id == "db-7"                      # 滑窗从边界后第一条用户消息接原文


def test_history_text_prepends_summary_and_windows():
    text = _history_text(_state(n_turns=6, summary="用户问过服务1001", upto=6))
    assert text.startswith("(早前对话摘要:用户问过服务1001")
    assert "问题0" not in text                       # 边界前原文不出现
    assert "问题3" in text                           # 窗内原文在(排除当前最后一条 human)


def test_history_text_no_summary_same_as_before():
    text = _history_text(_state(n_turns=2))
    assert "摘要" not in text and "问题0" in text


def test_log_model_context_shows_summary_and_window(caplog):
    import logging
    from app.graph.nodes import _agent_messages, _log_model_context
    state = _state(n_turns=6, summary="用户问过服务1001,留了手机13800138000", upto=6)
    msgs = _agent_messages(state)
    with caplog.at_level(logging.INFO, logger="app.graph.nodes"):
        _log_model_context(state, msgs)
    text = caplog.text
    assert "model_ctx" in text
    assert "服务1001" in text                 # 摘要全文可见
    assert "[human] '问题3" in text           # 滑窗每条消息角色+预览可见
    assert "问题0" not in text                # 边界前原文不进上下文,也不该出现在日志
