import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRequest
from app.schemas.extract import OpsTicket, ExtractRequest, RequestType


def test_chat_request_rejects_empty_message():
    with pytest.raises(ValidationError):
        ChatRequest(session_id="s1", message="")


def test_extract_request_rejects_empty_text():
    with pytest.raises(ValidationError):
        ExtractRequest(text="")


def test_ticket_service_id_nullable():
    t = OpsTicket(service_id=None, request_type="重启", expected_action="重启 1001")
    assert t.service_id is None
    assert t.request_type is RequestType.RESTART


def test_ticket_rejects_unknown_request_type():
    with pytest.raises(ValidationError):
        OpsTicket(service_id=None, request_type="砍价", expected_action="x")


def test_ticket_normalizes_placeholder_service_id_to_none():
    for placeholder in ("null", "None", "无", " N/A ", ""):
        t = OpsTicket(
            service_id=placeholder, request_type="投诉", expected_action="x"
        )
        assert t.service_id is None, placeholder
    assert OpsTicket(
        service_id="1001", request_type="重启", expected_action="x"
    ).service_id == "1001"
