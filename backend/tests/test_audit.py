import csv
import io

from tests.conftest import by_type

CONFLICTING = {"title": "Refund timing supplement",
               "content": "## Refund timing\n- Card refunds take 1-2 business days to appear on your statement."}


def _audit(client, headers, **params):
    r = client.get("/api/audit", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_risk_limit_change_is_recorded_with_who_before_after_and_the_request(client, login):
    admin = login("admin@harbour.demo")
    r = client.patch("/api/tenant", json={"auto_refund_limit": 5000, "ticket_sla_hours": 24}, headers=admin)
    before = client.get("/api/audit", headers=admin).json()
    entry = before[0]
    assert entry["action"] == "tenant.update" and entry["action_label"] == "Changed tenant settings"
    assert entry["actor"]["email"] == "admin@harbour.demo"
    assert entry["detail"]["changes"]["auto_refund_limit"][1] == 5000
    assert entry["detail"]["changes"]["auto_refund_limit"][0] != 5000
    assert entry["request_id"] == r.headers["X-Request-ID"], "joins the access log and the trace"
    # a PATCH that changes nothing leaves no entry
    client.patch("/api/tenant", json={"auto_refund_limit": 5000}, headers=admin)
    assert len(client.get("/api/audit", headers=admin).json()) == len(before)


def test_knowledge_changes_are_recorded_with_the_changed_lines(client, login):
    admin = login("admin@harbour.demo")
    doc = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()
    client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={
        "title": "Refund timing supplement",
        "content": "## Refund timing\n- Card refunds take 1-3 business days to appear on your statement.",
        "visibility": "public", "version": doc["version"]})
    client.delete(f"/api/kb/documents/{doc['id']}", headers=admin)
    entries = _audit(client, admin)
    assert [e["action"] for e in entries[:3]] == ["kb.document.delete", "kb.document.update", "kb.document.create"]
    assert {e["target"] for e in entries[:3]} == {f"document:{doc['id']}"}
    diff = entries[1]["detail"]["diff"]
    assert "-- Card refunds take 1-2 business days to appear on your statement." in diff
    assert "+- Card refunds take 1-3 business days to appear on your statement." in diff
    assert entries[1]["detail"]["from_version"] == 1 and entries[1]["detail"]["to_version"] == 2


def test_flywheel_conflict_and_regression_gate_decisions_are_recorded(client, ask, login):
    admin = login("admin@harbour.demo")
    _, events = ask(login("customer@harbour.demo"), "How do I join the trade-in program?")
    message_id = by_type(events, "final")[0]["message_id"]
    feedback = client.post(f"/api/messages/{message_id}/feedback", json={
        "rating": "correction", "reason_category": "knowledge_gap",
        "correction": "The trade-in program covers phones and tablets only, and pays $50-$800 once approved."},
        headers=login("agent@harbour.demo")).json()
    client.post(f"/api/flywheel/patches/{feedback['patch_id']}/apply", headers=admin)
    client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin)
    conflict_id = client.get("/api/kb/conflicts", headers=admin).json()[0]["id"]
    client.post(f"/api/kb/conflicts/{conflict_id}/resolve", json={"note": "The published policy stands"},
                headers=admin)
    case = client.get("/api/evals/cases", headers=admin).json()[0]
    client.patch(f"/api/evals/cases/{case['id']}", json={"status": "archived"}, headers=admin)
    actions = [e["action"] for e in _audit(client, admin)]
    for action in ("flywheel.patch.apply", "kb.conflict.resolve", "eval.case.update"):
        assert action in actions
    archived = _audit(client, admin, action="eval.case.update")[0]
    assert archived["detail"]["changes"]["status"] == [case["status"], "archived"]


def test_an_erasure_entry_does_not_keep_the_erased_identity(client, login):
    admin = login("admin@harbour.demo")
    me = client.get("/api/auth/me", headers=login("customer@harbour.demo")).json()
    client.delete(f"/api/admin/users/{me['id']}/personal-data", headers=admin)
    entry = _audit(client, admin, action="user.erase")[0]
    assert entry["target"] == f"user:{me['id']}"
    assert me["email"] not in str(entry) and me["name"] not in str(entry)


def test_the_audit_log_is_for_administrators_of_the_same_tenant(client, login):
    admin = login("admin@harbour.demo")
    client.patch("/api/tenant", json={"auto_refund_limit": 300}, headers=admin)
    assert client.get("/api/audit", headers=login("agent@harbour.demo")).status_code == 403
    assert client.get("/api/audit", headers=login("admin@rostr.demo")).json() == []


def _three_changes(client, admin):
    for limit in (100, 200, 300):
        client.patch("/api/tenant", json={"auto_refund_limit": limit}, headers=admin)


def test_an_untouched_audit_log_verifies(client, login):
    admin = login("admin@harbour.demo")
    _three_changes(client, admin)
    report = client.get("/api/audit/verify", headers=admin).json()
    assert report == {"ok": True, "entries": 3, "last_seq": 3, "altered": [], "missing": [], "unsigned": 0}
    assert client.get("/api/audit/verify", headers=login("agent@harbour.demo")).status_code == 403


def test_editing_or_deleting_an_entry_in_the_database_is_detected(client, login):
    # Someone with database access but without AGENTLOOP_AUDIT_KEY hides that the refund limit was
    # raised: rewriting the entry breaks its MAC, deleting it leaves a gap in the numbers.
    from sqlalchemy import delete, select, update

    from app.db import new_session
    from app.models import AuditLog

    admin = login("admin@harbour.demo")
    _three_changes(client, admin)
    with new_session() as db:
        first, second, _ = db.execute(select(AuditLog).order_by(AuditLog.seq)).scalars().all()
        db.execute(update(AuditLog).where(AuditLog.id == first.id)
                   .values(detail={"changes": {"auto_refund_limit": [75, 60]}}))
        db.execute(delete(AuditLog).where(AuditLog.id == second.id))
        db.commit()
    report = client.get("/api/audit/verify", headers=admin).json()
    assert report["ok"] is False and report["altered"] == [first.id] and report["missing"] == [2]


def test_concurrent_entries_get_distinct_numbers_without_gaps(client, login):
    import threading

    from app.audit import record, verify
    from app.db import new_session
    from app.models import Tenant, User
    from app.security import Principal

    barrier = threading.Barrier(6, timeout=10)

    def write():
        with new_session() as db:
            tenant = db.query(Tenant).filter_by(slug="harbour").one()
            admin = db.query(User).filter_by(email="admin@harbour.demo").one()
            barrier.wait()
            record(db, Principal(admin, tenant), "tenant.update", f"tenant:{tenant.id}", changes={})
            db.commit()

    threads = [threading.Thread(target=write) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with new_session() as db:
        tenant_id = db.query(Tenant).filter_by(slug="harbour").one().id
        report = verify(db, tenant_id)
    assert report["ok"] and report["last_seq"] == 6 and report["entries"] == 6


def test_an_unsigned_or_garbled_entry_fails_verification_instead_of_passing_or_crashing(client, login):
    # Code review: verify() ignored entries without seq/mac (a fabricated one passed as "written
    # before signing"), and a non-ASCII stored MAC made compare_digest raise, so /verify was a 500.
    from datetime import datetime, timezone

    from sqlalchemy import select, update

    from app.db import new_session
    from app.models import AuditLog, Tenant

    admin = login("admin@harbour.demo")
    _three_changes(client, admin)
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).where(Tenant.slug == "harbour")).scalar_one()
        db.add(AuditLog(tenant_id=tenant_id, actor_id=None, action="tenant.update", target="tenant:1",
                        detail={"changes": {}}, request_id="", created_at=datetime.now(timezone.utc)))
        db.commit()
    report = client.get("/api/audit/verify", headers=admin).json()
    assert report["ok"] is False and report["unsigned"] == 1
    with new_session() as db:
        first = db.execute(select(AuditLog).where(AuditLog.seq == 1)).scalar_one()
        db.execute(update(AuditLog).where(AuditLog.id == first.id).values(mac="é" * 64))
        db.commit()
    r = client.get("/api/audit/verify", headers=admin)
    assert r.status_code == 200 and first.id in r.json()["altered"]


def test_entries_from_before_signing_can_be_signed_once_by_an_operator(client, login):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db import new_session
    from app.models import AuditLog, Tenant
    from app.ops.audit_backfill import sign_unsigned

    admin = login("admin@harbour.demo")
    with new_session() as db:  # written by a version without signing
        tenant_id = db.execute(select(Tenant.id).where(Tenant.slug == "harbour")).scalar_one()
        db.add(AuditLog(tenant_id=tenant_id, actor_id=None, action="tenant.update", target="tenant:1",
                        detail={"changes": {"auto_refund_limit": [75, 80]}}, request_id="",
                        created_at=datetime.now(timezone.utc)))
        db.commit()
    _three_changes(client, admin)
    assert client.get("/api/audit/verify", headers=admin).json()["unsigned"] == 1
    with new_session() as db:
        assert sign_unsigned(db) == 1
    report = client.get("/api/audit/verify", headers=admin).json()
    assert report["ok"] and report["last_seq"] == 4 and report["unsigned"] == 0


def _capture_anchor_log():
    import io
    import logging

    from app.observability import JsonFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logging.getLogger("agentloop.audit").addHandler(handler)
    return stream, handler


def test_cutting_off_the_newest_entries_is_caught_by_the_log_anchors(client, login):
    # verify() cannot see entries deleted from the end together with a lowered counter. Every
    # committed entry's number and MAC also go to the log platform; comparing them catches it.
    import logging

    from sqlalchemy import delete, select, update

    from app.audit import check_anchors, verify
    from app.db import new_session
    from app.models import AuditLog, Tenant
    from app.ops.audit_anchor_check import read_anchors

    admin = login("admin@harbour.demo")
    stream, handler = _capture_anchor_log()
    try:
        _three_changes(client, admin)
    finally:
        logging.getLogger("agentloop.audit").removeHandler(handler)
    anchors = read_anchors(stream.getvalue().splitlines())
    assert [a["seq"] for a in anchors] == [1, 2, 3]
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).where(Tenant.slug == "harbour")).scalar_one()
        assert check_anchors(db, anchors)["ok"]
        db.execute(delete(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.seq == 3))
        db.execute(update(Tenant).where(Tenant.id == tenant_id).values(audit_seq=2))
        db.commit()
        assert verify(db, tenant_id)["ok"], "the blind spot the anchors are for"
        report = check_anchors(db, anchors)
    assert not report["ok"] and report["missing"] == [{"tenant_id": tenant_id, "seq": 3}]
    assert report["counter_behind"] == [tenant_id]


def test_an_entry_rolled_back_with_its_change_is_not_anchored(client, login):
    import logging

    from app.audit import record
    from app.db import new_session
    from app.models import Tenant, User
    from app.security import Principal

    stream, handler = _capture_anchor_log()
    try:
        with new_session() as db:
            tenant = db.query(Tenant).filter_by(slug="harbour").one()
            admin = db.query(User).filter_by(email="admin@harbour.demo").one()
            record(db, Principal(admin, tenant), "tenant.update", f"tenant:{tenant.id}", changes={})
            db.rollback()
            record(db, Principal(admin, tenant), "tenant.update", f"tenant:{tenant.id}", changes={})
            db.commit()
    finally:
        logging.getLogger("agentloop.audit").removeHandler(handler)
    from app.ops.audit_anchor_check import read_anchors
    assert [a["seq"] for a in read_anchors(stream.getvalue().splitlines())] == [1], \
        "the rolled-back number is reused, and only the committed entry is anchored"


# Review round 3, N-022: only administrators' configuration changes were audited. Staff deciding
# refunds, exporting customer data, replying as a person and closing tickets left no entry.

def test_refund_decisions_are_recorded_with_the_amount_and_the_reason(client, ask, login):
    customer, agent, admin = (login(e) for e in ("customer@harbour.demo", "agent@harbour.demo",
                                                 "admin@harbour.demo"))
    _, events = ask(customer, "I'd like a refund for SO20261012003")  # $268: an agent may approve it
    small = by_type(events, "approval_required")[0]["approval_id"]
    r = client.post(f"/api/approvals/{small}/decide",
                    json={"decision": "approve", "comment": "Checked against the photos"}, headers=agent)
    assert r.status_code == 200
    entry = _audit(client, admin, action="approval.approve")[0]
    assert entry["actor"]["email"] == "agent@harbour.demo" and entry["target"] == f"approval:{small}"
    assert entry["detail"]["amount"] == 268 and entry["detail"]["order_id"] == "SO20261012003"
    assert entry["detail"]["comment"] == "Checked against the photos" and entry["request_id"] == r.headers["X-Request-ID"]

    _, events = ask(customer, "I'd like a refund for SO20261008002")
    big = by_type(events, "approval_required")[0]["approval_id"]
    # A refused attempt (an agent approving above the threshold) changes nothing and is not a decision.
    assert client.post(f"/api/approvals/{big}/decide", json={"decision": "approve"}, headers=agent).status_code == 403
    assert len(_audit(client, admin, action="approval.approve")) == 1
    client.post(f"/api/approvals/{big}/decide", json={"decision": "reject", "comment": "The photos are unclear"},
                headers=agent)
    entry = _audit(client, admin, action="approval.reject")[0]
    assert entry["target"] == f"approval:{big}" and entry["detail"]["comment"] == "The photos are unclear"


def test_a_decision_that_failed_and_went_back_to_pending_says_so(client, ask, login, monkeypatch):
    from app.services import get_services

    _, events = ask(login("customer@harbour.demo"), "I'd like a refund for SO20261008002")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    admin = login("admin@harbour.demo")

    def broken(*args, **kwargs):
        raise RuntimeError("connection reset by upstream")

    monkeypatch.setattr(get_services().provider, "stream", broken)
    assert client.post(f"/api/approvals/{approval_id}/decide",
                       json={"decision": "reject", "comment": "Past the returns window"},
                       headers=admin).status_code == 503
    monkeypatch.undo()
    assert client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                       headers=admin).status_code == 200
    trail = [e["action"] for e in _audit(client, admin) if e["target"] == f"approval:{approval_id}"]
    assert trail == ["approval.approve", "approval.reopen", "approval.reject"], "newest first"


def test_exports_handoffs_and_tickets_are_recorded(client, ask, login):
    agent, admin = login("agent@harbour.demo"), login("admin@harbour.demo")
    ask(login("customer@harbour.demo"), "how long do refunds take")
    r = client.get("/api/export/runs.csv?days=7", headers=agent)
    assert r.status_code == 200
    export = _audit(client, admin, action="export.download")[0]
    assert export["actor"]["email"] == "agent@harbour.demo" and export["target"] == "export:runs"
    assert export["detail"] == {"days": 7, "rows": len(list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))) - 1}
    assert export["detail"]["rows"] == 1

    conversation_id, _ = ask(login("customer2@harbour.demo"), "put me through to a human")
    handoff_id = client.get("/api/handoffs", headers=agent).json()[0]["ticket_id"]
    message = client.post(f"/api/handoffs/{handoff_id}/reply", json={"content": "Hi, Mia here"},
                          headers=agent).json()
    client.post(f"/api/handoffs/{handoff_id}/close", headers=agent)
    reply, close = _audit(client, admin, action="handoff.reply")[0], _audit(client, admin, action="handoff.close")[0]
    assert reply["target"] == close["target"] == f"handoff:{handoff_id}"
    assert reply["detail"] == {"conversation_id": conversation_id, "message_id": message["message"]["id"]}
    assert "Hi, Mia" not in str(reply["detail"]), "what was said stays in the conversation, under retention"

    ask(login("customer@harbour.demo"), "my robot vacuum is faulty and won't turn on, please sort it out")
    ticket_id = client.get("/api/tickets", headers=agent).json()[0]["id"]
    client.post(f"/api/tickets/{ticket_id}/claim", headers=agent)
    client.post(f"/api/tickets/{ticket_id}/close", json={"resolution": "Booked in for a repair"}, headers=agent)
    actions = [e["action"] for e in _audit(client, admin) if e["target"] == f"ticket:{ticket_id}"]
    assert actions == ["ticket.close", "ticket.claim"]
    assert _audit(client, admin, action="ticket.close")[0]["detail"] == {"overdue": False}
    assert {e["action"] for e in _audit(client, admin, category="ticket")} == {"ticket.claim", "ticket.close"}
    assert _audit(client, admin, category="ticke") == [], "a whole category, not a prefix of one"
    assert client.get("/api/audit/verify", headers=admin).json()["ok"] is True
