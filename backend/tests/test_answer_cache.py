from app.agent.answer_cache import normalize_question
from app.db import new_session
from app.models import Run
from tests.conftest import by_type


def _done(events):
    """Routing and usage of the run, from its record: a customer's events do not carry them."""
    with new_session() as db:
        run = db.get(Run, by_type(events, "done")[0]["run_id"])
        return {"run_id": run.id, "route_tier": run.route_tier, "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens}


def test_question_normalisation():
    assert normalize_question("How long do refunds take?") == normalize_question("  how long  do refunds take ?")
    # Full-width letters and digits, as they arrive when text is copied out of a PDF or a
    # spreadsheet cell: the cache key must fold them to the same question as plain ASCII.
    assert normalize_question("\uff33\uff2f\uff12\uff10\uff12\uff16") == "so2026"


def test_repeated_faq_is_served_from_cache_with_its_evidence(client, ask, login):
    customer = login("customer@harbour.demo")
    _, first = ask(customer, "how long do refunds take?")
    assert _done(first)["route_tier"] != "cache"
    _, second = ask(customer, "how long do refunds take")  # different punctuation, same question
    done = _done(second)
    assert done["route_tier"] == "cache" and done["prompt_tokens"] == 0 and done["completion_tokens"] == 0
    assert not by_type(second, "tool_call"), "no retrieval or model call on a hit"
    assert by_type(second, "final")[0]["text"] == by_type(first, "final")[0]["text"]
    assert by_type(second, "verification")[0]["verdict"] == "pass"
    meta = by_type(second, "final")[0]["meta"]
    assert meta["cached"] is True and meta["citations"][0]["title"] == "Returns and refunds"
    trace = client.get(f"/api/runs/{done['run_id']}", headers=login("admin@harbour.demo")).json()
    assert [s["kind"] for s in trace["spans"]] == ["guardrail", "cache"]


def test_knowledge_change_invalidates_cache(client, ask, login):
    customer = login("customer@harbour.demo")
    ask(customer, "how do I use my points?")
    client.post("/api/kb/documents/text",
                json={"title": "Points supplement", "content": "## Points\n- Points cannot be exchanged for cash."},
                headers=login("admin@harbour.demo"))
    _, again = ask(customer, "how do I use my points?")
    assert _done(again)["route_tier"] != "cache", "a new knowledge version must not replay old answers"


def test_cache_is_scoped_by_visibility(ask, login):
    question = "what is the most a frontline agent can offer as a goodwill voucher?"
    _, staff = ask(login("agent@harbour.demo"), question)
    assert "$20" in by_type(staff, "final")[0]["text"]
    _, customer = ask(login("customer@harbour.demo"), question)
    assert _done(customer)["route_tier"] != "cache"
    assert "$20" not in by_type(customer, "final")[0]["text"], "internal answers never leak through the cache"


def test_tenants_do_not_share_cache(ask, login):
    ask(login("customer@harbour.demo"), "how long do refunds take?")
    _, other = ask(login("customer@rostr.demo"), "how long do refunds take?")
    assert _done(other)["route_tier"] != "cache"


def test_not_cached_follow_ups_orders_or_pii(ask, login):
    customer = login("customer@harbour.demo")
    conversation_id, _ = ask(customer, "how long does dispatch take?")
    _, follow_up = ask(customer, "how long does dispatch take?", conversation_id=conversation_id)
    assert _done(follow_up)["route_tier"] != "cache", "turns with history depend on context"
    ask(customer, "where is my order SO20261008002?")
    _, orders = ask(customer, "where is my order SO20261008002?")
    assert _done(orders)["route_tier"] != "cache", "answers built from live business data are never cached"
    ask(customer, "my mobile is 0412 345 678, how do I get a tax invoice?")
    _, pii = ask(customer, "my mobile is 0412 345 678, how do I get a tax invoice?")
    assert _done(pii)["route_tier"] != "cache"


def test_cache_can_be_disabled(settings, ask, login):
    settings.answer_cache_ttl_hours = 0
    customer = login("customer@harbour.demo")
    ask(customer, "how long do refunds take?")
    _, again = ask(customer, "how long do refunds take?")
    assert _done(again)["route_tier"] != "cache"


def test_overview_reports_cache_hit_rate(client, ask, login):
    customer = login("customer@harbour.demo")
    ask(customer, "how long do refunds take?")
    ask(customer, "how long do refunds take?")
    overview = client.get("/api/metrics/overview", headers=login("admin@harbour.demo")).json()
    assert overview["cache_hits"] == 1 and overview["cache_hit_rate"] == 0.5


def test_an_answer_given_after_a_failed_search_is_not_cached(client, ask, login, monkeypatch):
    # Code review: when search_knowledge failed (an exception in retrieval), the agent answered
    # "I couldn't find anything in the knowledge base…", the run passed verification and the
    # refusal was cached for a day.
    from app.agent import business_tools

    def broken(*args, **kwargs):
        raise ValueError("matmul: dimension mismatch")

    customer = login("customer@harbour.demo")
    monkeypatch.setattr(business_tools, "hybrid_search", broken)
    ask(customer, "how long do refunds take?")
    monkeypatch.undo()
    _, again = ask(customer, "how long do refunds take?")
    assert _done(again)["route_tier"] != "cache", "the refusal from a failed search was replayed"
    assert "1-3 business days" in by_type(again, "final")[0]["text"]


def test_a_different_embedding_model_does_not_reuse_cached_answers(settings):
    from app.agent.answer_cache import config_fingerprint

    before = config_fingerprint(settings)
    settings.embedding_dim = 1024
    assert config_fingerprint(settings) != before
