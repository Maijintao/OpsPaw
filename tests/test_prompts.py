from langchain_core.messages import HumanMessage

from app.core.prompts import CUSTOMER_SERVICE_PROMPT, EXTRACT_PROMPT, AGENT_SYSTEM, AGENT_PROMPT


def test_customer_service_prompt_renders_with_history():
    msgs = CUSTOMER_SERVICE_PROMPT.format_messages(
        history=[HumanMessage("服务 1001 现在什么状态?")]
    )
    assert msgs[0].type == "system"
    assert msgs[-1].content == "服务 1001 现在什么状态?"
    # 行为约束必须写进 system prompt
    for keyword in ("值班猫", "不", "服务"):
        assert keyword in msgs[0].content


def test_extract_prompt_renders_text():
    msgs = EXTRACT_PROMPT.format_messages(text="服务 1001 内存爆了要重启")
    assert msgs[0].type == "system"
    assert "服务 1001 内存爆了要重启" in msgs[-1].content


def test_agent_system_covers_tool_principles():
    for kw in ["值班猫", "工具", "query_faq", "create_ticket", "不要臆造"]:
        assert kw in AGENT_SYSTEM


def test_agent_prompt_has_history_placeholder():
    assert any(getattr(m, "variable_name", None) == "history"
               for m in AGENT_PROMPT.messages)
