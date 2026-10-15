"""Human takeover: one ticket per conversation, the agent stays quiet, staff reply and hand back."""

from sqlalchemy import func, select

from app.db import new_session
from app.models import Run, Ticket
from tests.conftest import by_type


def _handoffs(db, conversation_id=None):
    query = select(func.count(Ticket.id)).where(Ticket.kind == "handoff")
    if conversation_id is not None:
        query = query.where(Ticket.conversation_id == conversation_id)
    return db.execute(query).scalar_one()


def test_asking_again_does_not_queue_the_customer_twice(ask, login):
    jack = login("customer2@harbour.demo")
    conversation_id, first = ask(jack, "put me through to a human")
    assert by_type(first, "final")[0]["status"] == "handoff"
    with new_session() as db:
        runs_before = db.execute(select(func.count(Run.id))).scalar_one()
    _, again = ask(jack, "human! right now please", conversation_id=conversation_id)
    assert not by_type(again, "tool_call"), "a person owns the conversation: no agent run"
    assert "number 1 in the queue" in by_type(again, "final")[0]["text"], "his own ticket is not counted ahead of him"
    with new_session() as db:
        assert _handoffs(db, conversation_id) == 1
        assert db.execute(select(func.count(Run.id))).scalar_one() == runs_before, "no run, no handoff-rate inflation"


def test_queue_position_counts_only_earlier_waiting_customers(ask, login):
    ask(login("customer@harbour.demo"), "put me through to a human")
    _, events = ask(login("customer2@harbour.demo"), "this is unacceptable, I want to complain to a human")
    assert "number 2 in the queue" in by_type(events, "final")[0]["text"]


def test_staff_reply_reaches_the_customer_and_hand_back_restores_the_agent(client, ask, login):
    emma, mia = login("customer@harbour.demo"), login("agent@harbour.demo")
    conversation_id, _ = ask(emma, "put me through to a human")
    queue = client.get("/api/handoffs", headers=mia).json()
    assert [q["conversation_id"] for q in queue] == [conversation_id] and queue[0]["position"] == 1
    ticket_id = queue[0]["ticket_id"]

    r = client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "Hi Emma, Mia here — what can I help with?"},
                    headers=mia)
    assert r.status_code == 200 and r.json()["handoff"]["assignee"] == "Mia"
    _, events = ask(emma, "my robot vacuum still hasn't arrived", conversation_id=conversation_id)
    assert "Mia is with you now" in by_type(events, "final")[0]["text"]
    page = client.get(f"/api/conversations/{conversation_id}/messages", headers=emma).json()
    assert page["handoff"]["assignee"] == "Mia"
    assert [m["role"] for m in page["messages"]] == ["user", "assistant", "agent", "user"]

    assert client.post(f"/api/handoffs/{ticket_id}/close", headers=mia).status_code == 200
    assert client.get("/api/handoffs", headers=mia).json() == []
    _, events = ask(emma, "how long do refunds take", conversation_id=conversation_id)
    assert [e["name"] for e in by_type(events, "tool_call")] == ["search_knowledge"], "the agent answers again"
    assert client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "are you still there?"},
                       headers=mia).status_code == 409


def test_handoff_queue_is_staff_only_and_tenant_scoped(client, ask, login):
    ask(login("customer@harbour.demo"), "put me through to a human")
    assert client.get("/api/handoffs", headers=login("customer@harbour.demo")).status_code == 403
    assert client.get("/api/handoffs", headers=login("admin@rostr.demo")).json() == []
    with new_session() as db:
        ticket_id = db.execute(select(Ticket.id).where(Ticket.kind == "handoff")).scalar_one()
    r = client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "hi"}, headers=login("admin@rostr.demo"))
    assert r.status_code == 404


def test_human_replies_reach_the_model_as_assistant_turns(client, ask, login):
    emma, mia = login("customer@harbour.demo"), login("agent@harbour.demo")
    conversation_id, _ = ask(emma, "put me through to a human")
    ticket_id = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "Logged for you."}, headers=mia)
    client.post(f"/api/handoffs/{ticket_id}/close", headers=mia)
    from app.agent.runtime import AgentRuntime  # noqa: F401  (history is built by the runtime)
    from app.models import Conversation
    from app.services import get_services

    with new_session() as db:
        history = get_services().runtime(db)._history(db.get(Conversation, conversation_id))
    assert {m.role for m in history} <= {"user", "assistant"}, "model APIs reject other roles"
    assert any(m.content == "(human agent) Logged for you." for m in history)


def test_the_tool_itself_reuses_an_open_ticket(settings, ask, login):
    # The runtime keeps the agent out of a conversation that is with a person; if the tool is
    # reached anyway (another entry point, a race), it still must not queue the customer twice.
    from app.agent.business_tools import handoff_to_human
    from app.agent.tools import ToolContext
    from app.models import Tenant, User

    conversation_id, _ = ask(login("customer@harbour.demo"), "put me through to a human")
    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@harbour.demo")).scalar_one()
        ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, emma.tenant_id), user=emma,
                          embedder=None, run_id="test", conversation_id=conversation_id)
        out = handoff_to_human(ctx, {"reason": "asked again"})
        assert out["already_queued"] is True and out["queue_position"] == 1
        assert "already in the queue" in out["message"]
        assert _handoffs(db, conversation_id) == 1


def test_concurrent_handoffs_cannot_open_two_tickets(settings, ask, login, monkeypatch):
    # Simulate the race: the check runs before the other request's insert is visible.
    from app.agent import business_tools, handoff
    from app.agent.tools import ToolContext
    from app.models import Tenant, User

    conversation_id, _ = ask(login("customer@harbour.demo"), "put me through to a human")
    real_lookup = handoff.active_ticket
    calls = []

    def stale_lookup(db, cid):
        calls.append(cid)
        return None if len(calls) == 1 else real_lookup(db, cid)

    monkeypatch.setattr(handoff, "active_ticket", stale_lookup)
    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@harbour.demo")).scalar_one()
        ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, emma.tenant_id), user=emma,
                          embedder=None, run_id="test", conversation_id=conversation_id)
        out = business_tools.handoff_to_human(ctx, {"reason": "a second request arriving at the same time"})
        assert out["already_queued"] is True, "the unique index rejected the second insert"
        assert _handoffs(db, conversation_id) == 1


def test_dashboard_reports_the_queue_and_first_response(client, ask, login):
    emma, mia, admin = (login(e) for e in ("customer@harbour.demo", "agent@harbour.demo",
                                           "admin@harbour.demo"))
    ask(login("customer2@harbour.demo"), "put me through to a human")
    ask(emma, "put me through to a human")
    queue = client.get("/api/metrics/overview", headers=admin).json()["handoff_queue"]
    assert queue["waiting"] == 2 and queue["avg_first_response_s"] is None
    ticket_id = client.get("/api/handoffs", headers=mia).json()[1]["ticket_id"]
    client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "Hi, I'm looking at this now."}, headers=mia)
    queue = client.get("/api/metrics/overview", headers=admin).json()["handoff_queue"]
    assert (queue["waiting"], queue["in_progress"], queue["handoffs"]) == (1, 1, 2)
    assert queue["avg_first_response_s"] is not None and queue["longest_wait_s"] >= 0


def test_a_reply_that_read_the_ticket_before_a_close_cannot_reopen_it(client, ask, login, monkeypatch):
    from app.api import handoffs as api
    from app.api.common import get_owned
    from app.models import Conversation, Message

    emma, mia = login("customer@harbour.demo"), login("agent@harbour.demo")
    conversation_id, _ = ask(emma, "put me through to a human")
    ticket_id = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    assert client.post(f"/api/handoffs/{ticket_id}/close", headers=mia).status_code == 200

    def stale_check(db, principal, tid):  # what a reply that read the ticket just before the close saw
        ticket = get_owned(db, Ticket, tid, principal.tenant.id)
        return ticket, db.get(Conversation, ticket.conversation_id)

    monkeypatch.setattr(api, "_active", stale_check)
    r = client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "still there?"}, headers=mia)
    assert r.status_code == 409
    with new_session() as db:
        assert db.get(Ticket, ticket_id).status == "closed"
        assert db.get(Conversation, conversation_id).status == "bot"
        assert not db.execute(select(Message).where(Message.conversation_id == conversation_id,
                                                    Message.role == "agent")).first()


def test_giving_up_at_the_step_limit_really_hands_the_customer_over(settings, client, ask, login):
    # Review round 2, N-016: the run was labelled handoff, but no ticket existed and the
    # conversation stayed with the bot.
    settings.max_agent_steps = 1
    conversation_id, events = ask(login("customer@harbour.demo"), "where is SO20261008002")
    final = by_type(events, "final")[0]
    assert final["status"] == "handoff" and "in the queue" in final["text"]
    with new_session() as db:
        from app.models import Conversation
        assert _handoffs(db, conversation_id) == 1
        assert db.get(Conversation, conversation_id).status == "handoff"
    assert any(q["conversation_id"] == conversation_id
               for q in client.get("/api/handoffs", headers=login("agent@harbour.demo")).json())


def test_a_model_refusal_hands_over_too(ask, login, monkeypatch):
    from app.llm.base import ChatResult, StreamEvent, Usage
    from app.services import get_services

    def refusing_stream(messages, tools, model, options=None):
        yield StreamEvent(type="done", result=ChatResult(content="", tool_calls=[], usage=Usage(), model=model,
                                                          finish_reason="refusal"))

    monkeypatch.setattr(get_services().provider, "stream", refusing_stream)
    conversation_id, events = ask(login("customer@harbour.demo"), "help me write something nasty about a colleague")
    assert by_type(events, "final")[0]["status"] == "handoff"
    with new_session() as db:
        assert _handoffs(db, conversation_id) == 1


def test_a_run_past_its_deadline_hands_over_instead_of_waiting(settings, client, ask, login):
    # Review round 2, N-018: no budget for the whole run (6 steps x 2 tiers x 3 attempts x 60 s).
    settings.run_deadline_seconds = 1e-9  # already spent when the first model call would start
    conversation_id, events = ask(login("customer@harbour.demo"), "how long do refunds take")
    final = by_type(events, "final")[0]
    assert final["status"] == "handoff" and "took too long" in final["text"]
    run_id = events[0]["run_id"]
    trace = client.get(f"/api/runs/{run_id}", headers=login("admin@harbour.demo")).json()
    assert any(s["name"] == "run_deadline" for s in trace["spans"])
    with new_session() as db:
        assert _handoffs(db, conversation_id) == 1
