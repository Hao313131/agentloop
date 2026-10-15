from app.agent.rewrite import is_followup, needs_context, rule_rewrite
from tests.conftest import by_type

PREVIOUS = "what are the conditions for a change of mind return?"


def test_followup_detection_is_explicit():
    assert is_followup("and who pays the return postage?", PREVIOUS)
    assert is_followup("what about bank transfers?", PREVIOUS)
    assert not is_followup("how many points do I get?", PREVIOUS), "short but self-contained"
    assert not is_followup("and where is SO20261008002", PREVIOUS), "an order number is self-contained"
    assert not is_followup("and who pays the return postage?", None), "nothing to follow up on"


def test_rule_rewrite_keeps_the_previous_topic():
    assert rule_rewrite("and who pays the return postage?", PREVIOUS) == \
        "conditions for change of mind return, who pays the return postage?"


def test_followup_is_answered_in_context_and_traced(client, ask, login):
    customer = login("customer@harbour.demo")
    conversation_id, _ = ask(customer, PREVIOUS)
    _, events = ask(customer, "and who pays the return postage?", conversation_id=conversation_id)
    assert by_type(events, "rewrite")[0]["standalone"].startswith("conditions for change of mind return")
    assert "the customer pays return postage" in by_type(events, "final")[0]["text"]
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"]
    assert messages[-2]["content"] == "and who pays the return postage?", "the conversation keeps what the customer typed"
    run_id = by_type(events, "done")[0]["run_id"]
    spans = client.get(f"/api/runs/{run_id}", headers=login("admin@harbour.demo")).json()["spans"]
    rewrite = next(s for s in spans if s["kind"] == "rewrite")
    assert rewrite["output"]["method"] == "rule"


def test_an_additive_opener_with_a_complete_question_starts_a_new_topic():
    assert not needs_context("also, can I get a tax invoice?")
    assert rule_rewrite("also, can I get a tax invoice?", "and how long does that take?") == "can I get a tax invoice?"
    assert needs_context("what about points?"), "a bare subject still leans on the previous question"
    assert needs_context("and who pays the return postage?")


def test_a_bare_followup_swaps_the_subject_and_keeps_the_question():
    from app.agent.rewrite import frame_of, next_frame

    root = frame_of(PREVIOUS)
    second = next_frame(root, "and who pays the return postage?", rule_rewrite("and who pays the return postage?", root))
    assert (second.topic, second.predicate) == ("conditions for change of mind return", "pays return postage")
    assert rule_rewrite("what about a faulty item?", second) == "a faulty item, pays return postage"
    assert rule_rewrite("what about bank transfers?", frame_of("who pays the return postage?")) == \
        "bank transfers, pays return postage"


def test_chained_followups_build_on_the_previous_rewrite(ask, login):
    customer = login("customer@harbour.demo")
    conversation_id, _ = ask(customer, PREVIOUS)
    ask(customer, "and who pays the return postage?", conversation_id=conversation_id)
    _, events = ask(customer, "what about a faulty item?", conversation_id=conversation_id)
    assert by_type(events, "rewrite")[0]["standalone"] == "a faulty item, pays return postage", \
        "built on turn 2, not on what was typed"
    assert by_type(events, "final")[0]["status"] == "completed"


def test_a_new_question_after_a_followup_is_not_glued_to_it(ask, login):
    customer = login("customer@harbour.demo")
    conversation_id, _ = ask(customer, "what are your dispatch times?")
    ask(customer, "and how long does that take?", conversation_id=conversation_id)
    _, events = ask(customer, "also, can I get a tax invoice?", conversation_id=conversation_id)
    assert by_type(events, "rewrite")[0]["standalone"] == "can I get a tax invoice?"
    assert "tax invoice" in by_type(events, "final")[0]["text"]
