"""Failure handling around human-in-the-loop resume: crashes, retries and races."""

import pytest
from sqlalchemy import func, select

from app.agent import runtime as agent_runtime
from app.agent.runtime import ResumeError
from app.agent.tools import ToolContext
from app.db import new_session
from app.models import Approval, Message, Order, Refund, Run, Tenant, User
from app.services import get_services
from tests.conftest import by_type


def _break_model(monkeypatch):
    """Make the next model call crash with a non-LLMError (a bug, a driver error...)."""

    def broken(*args, **kwargs):
        raise RuntimeError("connection reset by upstream")

    monkeypatch.setattr(get_services().provider, "stream", broken)


def _pending_refund(ask, login) -> tuple[dict, int, int]:
    customer = login("customer@harbour.demo")
    conversation_id, events = ask(customer, "I'd like a refund for SO20261008002")
    return customer, conversation_id, by_type(events, "approval_required")[0]["approval_id"]


def _refund_count(db) -> int:
    return db.execute(select(func.count(Refund.id)).where(Refund.order_id == "SO20261008002")).scalar_one()


def test_crash_before_the_decision_takes_effect_reopens_the_approval(client, ask, login, monkeypatch):
    customer, conversation_id, approval_id = _pending_refund(ask, login)
    admin = login("admin@harbour.demo")
    _break_model(monkeypatch)

    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "comment": "past the return window"}, headers=admin)
    assert r.status_code == 503 and "is back in the pending queue and can be decided again" in r.json()["detail"]
    with new_session() as db:
        approval = db.get(Approval, approval_id)
        run = db.get(Run, approval.run_id)
        assert approval.status == "pending" and approval.reviewer_id is None
        assert run.status == "failed" and "connection reset by upstream" in run.error
        assert db.get(Order, "SO20261008002").status == "shipped"
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"]
    assert messages[-1]["meta"]["status"] == "waiting_approval", "the customer is still told to wait"

    monkeypatch.undo()  # the model is back: the same approval can be decided again
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 200 and "is approved" in r.json()["final"]["text"]
    with new_session() as db:
        assert db.get(Run, db.get(Approval, approval_id).run_id).status == "completed"
        assert _refund_count(db) == 1


def test_crash_after_the_refund_keeps_the_decision_and_tells_the_customer(client, ask, login, monkeypatch):
    customer, conversation_id, approval_id = _pending_refund(ask, login)
    admin = login("admin@harbour.demo")
    _break_model(monkeypatch)  # the refund runs, then writing the answer crashes

    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "approved"
    with new_session() as db:
        run = db.get(Run, db.get(Approval, approval_id).run_id)
        assert run.status == "failed" and run.error and run.finished_at is not None
        assert _refund_count(db) == 1
    last = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"][-1]
    assert "is approved" in last["content"] and last["meta"]["fallback"] is True
    assert client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                       headers=admin).status_code == 409


def test_two_reviewers_deciding_at_once_run_the_tool_once(client, ask, login):
    _, conversation_id, approval_id = _pending_refund(ask, login)
    services = get_services()
    first, second = new_session(), new_session()
    try:
        # Both requests load the approval while it is still pending.
        mine, theirs = first.get(Approval, approval_id), second.get(Approval, approval_id)
        admin = first.execute(select(User).where(User.email == "admin@harbour.demo")).scalar_one()
        agent = second.execute(select(User).where(User.email == "agent@harbour.demo")).scalar_one()
        services.runtime(first).resume(mine, "approve", admin)
        with pytest.raises(ValueError, match="This approval has already been decided"):
            services.runtime(second).resume(theirs, "approve", agent)
    finally:
        first.close()
        second.close()
    with new_session() as db:
        assert db.get(Approval, approval_id).reviewer_id == admin.id
        assert _refund_count(db) == 1
        answers = db.execute(select(Message).where(
            Message.conversation_id == conversation_id, Message.role == "assistant")).scalars().all()
        assert len(answers) == 2, "the paused answer plus exactly one resumed answer"


def test_a_reviewer_who_decides_during_the_whatsapp_notice_succeeds_first_time(client, ask, login, monkeypatch):
    # The approval is committed once it is in the queue, and the notification call after that can take
    # seconds. The pending call used to be saved only after the notice: a reviewer who decided
    # in between got an error and the approval went back to pending.
    services = get_services()
    decisions = []

    def notice_while_a_reviewer_decides(settings, approval, tenant, transport=None):
        with new_session() as db:
            admin = db.execute(select(User).where(User.email == "admin@harbour.demo")).scalar_one()
            try:
                decisions.append(services.runtime(db).resume(db.get(Approval, approval.id), "approve", admin))
            except ResumeError as exc:
                decisions.append(exc)
        return {"sent": True, "mode": "test"}

    monkeypatch.setattr(agent_runtime, "notify_approval", notice_while_a_reviewer_decides)
    customer = login("customer@harbour.demo")
    conversation_id, events = ask(customer, "I'd like a refund for SO20261008002")
    assert len(decisions) == 1 and not isinstance(decisions[0], ResumeError), decisions
    assert "is approved" in by_type(decisions[0], "final")[0]["text"]
    assert [e["type"] for e in events][-3:] == ["approval_required", "final", "done"]
    with new_session() as db:
        approval = db.get(Approval, by_type(events, "approval_required")[0]["approval_id"])
        run = db.get(Run, approval.run_id)
        assert approval.status == "approved" and run.status == "completed" and run.error is None
        assert _refund_count(db) == 1
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"]
    assert [m["meta"]["status"] for m in messages if m["role"] == "assistant"] == ["waiting_approval", "completed"]


def test_refund_execution_is_idempotent_per_approval(client, ask, login):
    _, _, approval_id = _pending_refund(ask, login)
    services = get_services()
    with new_session() as db:
        approval = db.get(Approval, approval_id)
        run = db.get(Run, approval.run_id)
        ctx = ToolContext(db=db, settings=services.settings, tenant=db.get(Tenant, run.tenant_id),
                          user=db.get(User, run.user_id), embedder=services.embedder, run_id=run.id,
                          conversation_id=run.conversation_id, approval_id=approval.id)
        tool = services.registry.get("request_refund")
        tool.precheck(ctx, approval.arguments)
        first = tool.handler(ctx, approval.arguments)
        tool.precheck(ctx, approval.arguments)  # a retry passes the precheck instead of a duplicate refund
        second = tool.handler(ctx, approval.arguments)
        assert first["refund_id"] == second["refund_id"] and _refund_count(db) == 1


def test_unexpected_crash_marks_a_streaming_run_failed(ask, login, monkeypatch):
    _break_model(monkeypatch)
    _, events = ask(login("customer@harbour.demo"), "Can you help me with my order, please?")
    assert by_type(events, "error")
    with new_session() as db:
        run = db.get(Run, events[0]["run_id"])
        assert run.status == "failed" and "RuntimeError" in run.error


def test_a_failed_whatsapp_notice_does_not_turn_an_executed_approval_into_an_error(client, ask, login, monkeypatch):
    # The refund is paid and committed before the customer is told on WhatsApp. A network error
    # there used to surface as a 500, so the reviewer saw a failure for a decision that had
    # taken effect (and a retry answered "already been decided").
    import httpx

    from app.channels.whatsapp import WhatsAppClient
    from app.models import Conversation

    customer, conversation_id, approval_id = _pending_refund(ask, login)
    with new_session() as db:
        conversation = db.get(Conversation, conversation_id)
        conversation.channel, conversation.external_id = "whatsapp", "wa_refund"
        db.commit()

    def unreachable(self, to, text, reply_to=None):
        raise httpx.ConnectError("whatsapp unreachable")

    monkeypatch.setattr(WhatsAppClient, "send_text", unreachable)
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=login("admin@harbour.demo"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval"]["status"] == "approved" and body["delivery"]["sent"] is False
    with new_session() as db:
        assert _refund_count(db) == 1
