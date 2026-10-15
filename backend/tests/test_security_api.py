import logging

import pytest

from app.config import Settings
from app.main import create_app
from app.seed import DEMO_PASSWORD
from tests.conftest import _reset_database, by_type


def test_login_failure_and_missing_token(client):
    assert client.post("/api/auth/login", json={"email": "admin@harbour.demo", "password": "wrong"}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_customer_cannot_use_staff_endpoints(client, login):
    customer = login("customer@harbour.demo")
    assert client.get("/api/kb/documents", headers=customer).status_code == 403
    assert client.get("/api/approvals", headers=customer).status_code == 403
    assert client.get("/api/runs", headers=customer).status_code == 403


def test_agent_cannot_write_knowledge_base(client, login):
    r = client.post("/api/kb/documents/text", json={"title": "x", "content": "y"}, headers=login("agent@harbour.demo"))
    assert r.status_code == 403


def test_tenants_are_isolated(client, ask, login):
    harbour_admin = login("admin@harbour.demo")
    rostr_admin = login("admin@rostr.demo")
    _, events = ask(login("customer@harbour.demo"), "I'd like a refund for SO20261008002")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    run_id = by_type(events, "done")[0]["run_id"]

    assert client.get(f"/api/approvals/{approval_id}", headers=rostr_admin).status_code == 404
    assert client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                       headers=rostr_admin).status_code == 404
    assert client.get(f"/api/runs/{run_id}", headers=rostr_admin).status_code == 404
    harbour_docs = {d["id"] for d in client.get("/api/kb/documents", headers=harbour_admin).json()}
    rostr_docs = {d["id"] for d in client.get("/api/kb/documents", headers=rostr_admin).json()}
    assert harbour_docs.isdisjoint(rostr_docs)
    some_doc = min(harbour_docs)
    assert client.get(f"/api/kb/documents/{some_doc}", headers=rostr_admin).status_code == 404

    _, rostr_events = ask(login("customer@rostr.demo"), "What's your change-of-mind returns policy?")
    assert "couldn't find anything in the knowledge base" in by_type(rostr_events, "final")[0]["text"]


def test_customer_cannot_read_other_customers_conversation(client, ask, login):
    conversation_id, _ = ask(login("customer@harbour.demo"), "How do loyalty points work?")
    other = login("customer2@harbour.demo")
    assert client.get(f"/api/conversations/{conversation_id}/messages", headers=other).status_code == 404


def test_rate_limit(settings, client, login):
    settings.rate_limit_per_minute = 2
    headers = login("customer@harbour.demo")
    codes = [client.post("/api/chat", json={"content": "How do loyalty points work?"}, headers=headers).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_login_is_limited_per_client_across_accounts(settings, client):
    settings.login_attempts_per_ip = 5
    codes = [client.post("/api/auth/login", json={"email": f"user{i}@harbour.demo", "password": "guess"}).status_code
             for i in range(6)]
    assert codes == [401] * 5 + [429], "a new email per attempt must not reset the budget"


def test_login_limit_per_account_ignores_case_and_whitespace(client):
    attempts = [" " * (i % 3) + ("Admin@Harbour.Demo" if i % 2 else "admin@harbour.demo") for i in range(11)]
    codes = [client.post("/api/auth/login", json={"email": e, "password": "guess"}).status_code for e in attempts]
    assert codes == [401] * 10 + [429]


def test_successful_logins_do_not_use_up_the_guessing_budget(client):
    good = [client.post("/api/auth/login", json={"email": "customer@harbour.demo", "password": DEMO_PASSWORD}).status_code
            for _ in range(15)]
    assert good == [200] * 15
    bad = [client.post("/api/auth/login", json={"email": "customer@harbour.demo", "password": "guess"}).status_code
           for _ in range(11)]
    assert bad == [401] * 10 + [429], "failures still count"


def test_an_unknown_email_costs_the_same_password_work_as_a_wrong_password(client, monkeypatch):
    # If an unknown e-mail is refused before any hashing, the response time tells which
    # accounts exist. Counted rather than timed, so the test cannot flake.
    import hashlib

    real, work = hashlib.pbkdf2_hmac, []

    def counted(name, password, salt, iterations, *rest):
        work.append((len(salt), iterations))
        return real(name, password, salt, iterations, *rest)

    monkeypatch.setattr(hashlib, "pbkdf2_hmac", counted)

    def failed_login(email: str) -> list:
        work.clear()
        assert client.post("/api/auth/login", json={"email": email, "password": "guess"}).status_code == 401
        return list(work)

    wrong_password, unknown_email = failed_login("customer@harbour.demo"), failed_login("nobody@harbour.demo")
    assert len(wrong_password) == 1, "a wrong password is checked with one PBKDF2 run"
    assert unknown_email == wrong_password, "an unknown e-mail must do the same work (salt length, iterations)"


def test_rate_limiter_holds_under_a_parallel_burst():
    import threading

    from fastapi import HTTPException

    from app.security import RateLimiter

    limiter, allowed = RateLimiter(), []

    def attempt():
        try:
            limiter.check("login:burst", 10)
            allowed.append(1)
        except HTTPException:
            pass

    threads = [threading.Thread(target=attempt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 10


@pytest.mark.parametrize("secret", [Settings().jwt_secret, "change-me-to-a-long-random-string-before-deploying",
                                    "short-secret"])
def test_production_config_refuses_a_public_or_short_jwt_secret(settings, secret):
    settings.seed_demo, settings.jwt_secret = False, secret
    with pytest.raises(RuntimeError, match="LOOPDESK_JWT_SECRET"):
        create_app(settings)


def test_demo_config_starts_but_warns_about_the_default_jwt_secret(settings, caplog):
    _reset_database(settings.database_url)
    settings.jwt_secret = Settings().jwt_secret
    with caplog.at_level(logging.WARNING, logger="app.main"):
        create_app(settings)
    assert "LOOPDESK_JWT_SECRET" in caplog.text


def test_production_config_starts_with_a_strong_secret(settings):
    _reset_database(settings.database_url)
    settings.seed_demo = False  # the fixture's secret is 36 random-looking bytes
    settings.audit_key = "another-independent-audit-key-of-32-bytes!"
    assert create_app(settings).title == "LoopDesk API"


@pytest.mark.parametrize("key", [None, "short", "test-secret-with-at-least-32-bytes!!"])
def test_production_config_refuses_a_missing_weak_or_shared_audit_key(settings, key):
    # Code review: derived from the JWT secret, rotating that secret made every past audit entry
    # fail verification under a "the database may have been changed" banner.
    settings.seed_demo, settings.audit_key = False, key
    with pytest.raises(RuntimeError, match="LOOPDESK_AUDIT_KEY"):
        create_app(settings)


def test_upload_validation(client, login):
    admin = login("admin@harbour.demo")
    bad = client.post("/api/kb/documents/upload", files={"file": ("a.exe", b"MZ", "application/octet-stream")},
                      headers=admin)
    assert bad.status_code == 415
    ok = client.post("/api/kb/documents/upload", files={"file": ("after-sales.md", "## Trade-in\n- Trade-in is limited to the phone range.".encode())},
                     data={"visibility": "public"}, headers=admin)
    assert ok.status_code == 200 and ok.json()["chunk_count"] == 1


def test_formatted_phone_numbers_never_reach_storage_in_plain_text(ask, login):
    from sqlalchemy import select

    from app.db import new_session
    from app.models import Message, Run, Span, Ticket

    ask(login("customer@harbour.demo"), "The robot vacuum is broken and won't start, call 0412 345 678 urgently")
    with new_session() as db:
        stored = [m.content for m in db.execute(select(Message)).scalars()]
        stored += [r.input_text for r in db.execute(select(Run)).scalars()]
        stored += [t.description for t in db.execute(select(Ticket)).scalars()]
        stored += [str(s.input) + str(s.output) for s in db.execute(select(Span)).scalars()]
    assert any("41 *** 678" in s for s in stored), "the masked form is what gets stored"
    assert not any("0412 345 678" in s or "0412345678" in s for s in stored)


# ---------------------------------------------------------------- permission matrix
# Reviewed on purpose. Any route not listed here must demand a login, and anything a
# customer may call is listed below; a new endpoint cannot become public by accident.
PUBLIC_ROUTES = {
    ("GET", "/api/health"), ("GET", "/api/ready"),
    ("GET", "/metrics"),  # aggregate counters only, no tenant data; restrict at the network layer
    ("POST", "/api/auth/login"), ("GET", "/api/auth/demo-accounts"),
    ("GET", "/api/channels/whatsapp/{tenant_slug}/events"),  # Meta's subscription challenge
    ("POST", "/api/channels/whatsapp/{tenant_slug}/events"),  # authenticated by the tenant's app signature
}
CUSTOMER_ROUTES = {
    ("GET", "/api/auth/me"), ("GET", "/api/conversations"), ("POST", "/api/conversations"),
    ("GET", "/api/conversations/{conversation_id}/messages"),
    ("POST", "/api/conversations/{conversation_id}/stream"), ("POST", "/api/chat"),
    ("POST", "/api/messages/{message_id}/feedback"),
}


def test_every_route_is_public_on_purpose_or_requires_the_right_role(client, login):
    import re

    customer = login("customer@harbour.demo")
    seen = set()
    for path, operations in client.app.openapi()["paths"].items():
        for method in operations:
            route = (method.upper(), path)
            seen.add(route)
            url = re.sub(r"\{[^}]+\}", "1", path)
            if route not in PUBLIC_ROUTES:
                assert client.request(route[0], url, json={}).status_code == 401, f"{route} answers without a login"
            if route not in PUBLIC_ROUTES | CUSTOMER_ROUTES:
                code = client.request(route[0], url, json={}, headers=customer).status_code
                assert code == 403, f"{route} answers a customer with {code}"
    assert PUBLIC_ROUTES | CUSTOMER_ROUTES <= seen, "the lists name routes that no longer exist"


def test_whatsapp_webhook_fails_closed_outside_the_demo(settings, client):
    from app.config import WhatsAppApp

    settings.seed_demo = False
    r = client.post("/api/channels/whatsapp/harbour/events", json={"entry": []})
    assert r.status_code == 503
    # Review round 4: a tenant without its own app stays closed even when another has one.
    settings.whatsapp_apps = {"harbour": WhatsAppApp("phone_harbour", "access-token", "verify-token-harbour")}
    r = client.post("/api/channels/whatsapp/rostr/events",
                    json={"entry": []})
    assert r.status_code == 503


def test_login_limits_are_shared_across_worker_processes(settings, client, monkeypatch):
    # Review round 2, N-017: with 4 workers the in-memory limiter let 21 of 40 guesses reach
    # the password check. The database counter is shared; clearing this process's memory (as
    # another worker would see it) does not reset the budget.
    from app.security import rate_limiter, shared_rate_limiter

    settings.rate_limit_backend = "database"
    monkeypatch.setattr(shared_rate_limiter, "clock", lambda: 60_000.0)  # no minute boundary mid-test

    def guess():
        return client.post("/api/auth/login", json={"email": "admin@harbour.demo", "password": "guess"}).status_code

    codes = [guess() for _ in range(6)]
    rate_limiter._hits.clear()  # another process knows nothing about these attempts
    codes += [guess() for _ in range(5)]
    assert codes == [401] * 10 + [429]
    ok = client.post("/api/auth/login", json={"email": "customer@harbour.demo", "password": DEMO_PASSWORD})
    assert ok.status_code == 200


def test_database_limiter_gives_out_exactly_the_limit_under_concurrency(client, monkeypatch):
    # Separate sessions (separate connections, as separate workers would have) race for the
    # same key; the conditional upsert lets exactly `limit` of them through. On PostgreSQL
    # in CI these really run in parallel.
    import threading

    from fastapi import HTTPException
    from sqlalchemy import select

    from app.db import new_session
    from app.models import RateCounter
    from app.security import shared_rate_limiter

    monkeypatch.setattr(shared_rate_limiter, "clock", lambda: 60_000.0)
    barrier, results = threading.Barrier(12, timeout=10), []

    def attempt():
        db = new_session()
        try:
            barrier.wait()
            results.append(shared_rate_limiter.check(db, "login:race@harbour.demo", 5) is not None)
        except HTTPException as exc:
            results.append(exc.status_code)
        finally:
            db.close()

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results, key=str) == [429] * 7 + [True] * 5
    with new_session() as db:
        rows = db.execute(select(RateCounter.key, RateCounter.count)).all()
    assert [count for _, count in rows] == [5]
    assert "race@harbour.demo" not in rows[0][0]  # stored hashed, not as the e-mail address


def test_database_limiter_drops_expired_windows(client, monkeypatch):
    from sqlalchemy import select

    from app.db import new_session
    from app.models import RateCounter
    from app.security import DatabaseRateLimiter

    now = [60_000.0]
    limiter = DatabaseRateLimiter(clock=lambda: now[0])
    with new_session() as db:
        limiter.check(db, "login-ip:203.0.113.7", 30)
        now[0] += 180  # three minutes later, a different client
        limiter.check(db, "login-ip:198.51.100.2", 30)
        minutes = db.execute(select(RateCounter.minute)).scalars().all()
    assert minutes == [int(now[0] // 60)]


def test_rate_limit_backend_is_validated(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("LOOPDESK_RATE_LIMIT_BACKEND", "redis")
    with pytest.raises(ValueError, match="RATE_LIMIT_BACKEND"):
        Settings.from_env()
