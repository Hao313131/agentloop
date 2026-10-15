"""The approval policy must judge the refund that will actually be executed, whatever
shape the model's arguments take (real models omit optional fields and vary casing)."""

from sqlalchemy import select

from app.db import new_session
from app.llm.base import ChatResult, LLMProvider, ToolCall, Usage
from app.models import Approval, Order, Refund
from app.services import get_services
from tests.conftest import by_type


class ScriptedRefundModel(LLMProvider):
    """Calls request_refund once with fixed arguments, then answers in plain text."""

    name = "scripted"

    def __init__(self, arguments: dict):
        self.arguments = arguments

    def chat(self, messages, tools, model, options=None) -> ChatResult:
        if messages[-1].role == "user":
            call = ToolCall(id="call_1", name="request_refund", arguments=dict(self.arguments))
            return ChatResult(content="", tool_calls=[call], usage=Usage(), model=model, finish_reason="tool_calls")
        return ChatResult(content="Done — I've processed that for you.", tool_calls=[], usage=Usage(), model=model)


def _use_model(monkeypatch, arguments: dict) -> None:
    monkeypatch.setattr(get_services(), "provider", ScriptedRefundModel(arguments))


def test_refund_without_amount_needs_approval_for_the_full_order_amount(ask, login, monkeypatch):
    # The schema makes "amount" optional ("defaults to the amount paid"); the policy used to read it as 0.
    _use_model(monkeypatch, {"order_id": "so20261008002", "reason": "Faulty on arrival"})
    _, events = ask(login("customer@harbour.demo"), "I'd like a refund for SO20261008002")

    approval = by_type(events, "approval_required")[0]
    assert approval["arguments"]["amount"] == 1299.0 and approval["arguments"]["order_id"] == "SO20261008002"
    assert "$1,299.00 AUD" in by_type(events, "final")[0]["text"]
    with new_session() as db:
        assert db.get(Order, "SO20261008002").status == "shipped", "no money moves before approval"
        assert db.execute(select(Refund).where(Refund.order_id == "SO20261008002")).first() is None
        stored = db.get(Approval, approval["approval_id"])
        assert stored.arguments["amount"] == 1299.0, "the reviewer sees the amount that will be paid"


def test_zero_amount_is_rejected_instead_of_becoming_a_full_refund(ask, login, monkeypatch):
    _use_model(monkeypatch, {"order_id": "SO20261008002", "amount": 0, "reason": "Faulty on arrival"})
    _, events = ask(login("customer@harbour.demo"), "I'd like a refund for SO20261008002")
    result = [e for e in by_type(events, "tool_result") if e["name"] == "request_refund"][0]
    assert result["output"]["error"] == "The refund amount must be greater than $0"
    assert not by_type(events, "approval_required")
    with new_session() as db:
        assert db.get(Order, "SO20261008002").status == "shipped"


def test_non_finite_amount_is_rejected(ask, login, monkeypatch):
    _use_model(monkeypatch, {"order_id": "SO20261008002", "amount": "NaN", "reason": "Faulty on arrival"})
    _, events = ask(login("customer@harbour.demo"), "I'd like a refund for SO20261008002")
    result = [e for e in by_type(events, "tool_result") if e["name"] == "request_refund"][0]
    assert result["output"]["error"] == "The refund amount is not a valid number"
    with new_session() as db:
        assert db.get(Order, "SO20261008002").status == "shipped"
        assert db.execute(select(Refund).where(Refund.order_id == "SO20261008002")).first() is None


def test_pending_duplicate_is_detected_regardless_of_order_id_casing(ask, login, monkeypatch):
    customer = login("customer@harbour.demo")
    _use_model(monkeypatch, {"order_id": "so20261008002", "amount": 1299, "reason": "Faulty on arrival"})
    _, first = ask(customer, "I'd like a refund for SO20261008002")
    assert by_type(first, "approval_required")
    _use_model(monkeypatch, {"order_id": "SO20261008002", "amount": 1299, "reason": "Faulty on arrival"})
    _, second = ask(customer, "I'd like a refund for SO20261008002")
    result = [e for e in by_type(second, "tool_result") if e["name"] == "request_refund"][0]
    assert "already waiting for approval" in result["output"]["error"]
