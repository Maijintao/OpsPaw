import pytest
from langchain_core.messages import HumanMessage

from app.graph import nodes


@pytest.mark.asyncio
async def test_script_reply_fixed_no_actions():
    from app.core.prompts import SCRIPT_REPLY_CHITCHAT
    out = await nodes.script_reply({"intent": "闲聊"})
    assert out["answer"] == SCRIPT_REPLY_CHITCHAT
    assert out["trace"]["route"] == "fallback_script"
    assert not out.get("suggested_actions")


@pytest.mark.asyncio
async def test_complaint_reply_offers_two_actions():
    out = await nodes.complaint_reply({"messages": [HumanMessage("我要投诉你们")], "conversation_id": 7})
    assert out["answer"] == nodes.COMPLAINT_REPLY
    types = [a["type"] for a in out["suggested_actions"]]
    assert types == ["transfer_human", "create_ticket"]
    ticket = next(a for a in out["suggested_actions"] if a["type"] == "create_ticket")
    assert ticket["draft"]["ticket_type"] == "投诉"
    assert ticket["draft"]["description"] == "我要投诉你们"
