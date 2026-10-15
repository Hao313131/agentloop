from app.agent.prompts import PROMPT_VERSIONS, choose_prompt_version
from app.db import new_session
from app.evals import cli
from app.models import Run
from tests.conftest import by_type


def test_every_prompt_version_keeps_the_placeholders_and_safety_rules():
    for version, template in PROMPT_VERSIONS.items():
        for placeholder in ("{tenant}", "{role}", "{today}"):
            assert placeholder in template, version
        for rule in ("search_knowledge", "request_refund", "couldn't find anything in the knowledge base",
                     "ignore your instructions"):
            assert rule in template, (version, rule)
        assert template.rstrip().splitlines()[-1].startswith("Current session"), "variable facts stay in the last line"


def test_canary_split_is_stable_and_roughly_proportional():
    picks = [choose_prompt_version("v1", "v2", 30, f"conversation:{i}") for i in range(1000)]
    share = picks.count("v2") / len(picks)
    assert 0.25 < share < 0.35
    assert all(choose_prompt_version("v1", "v2", 30, f"conversation:{i}") == picks[i] for i in range(50))
    assert choose_prompt_version("v1", "v9", 50, "x") == "v1", "unknown canary falls back to the default"
    assert choose_prompt_version("v1", "v2", 0, "x") == "v1"


def test_runs_record_the_prompt_version_and_cache_is_versioned(settings, client, ask, login):
    customer = login("customer@harbour.demo")
    _, first = ask(customer, "how long do refunds take?")
    settings.prompt_canary_version, settings.prompt_canary_percent = "v2", 100
    _, second = ask(customer, "how long do refunds take?")
    done = by_type(second, "done")[0]
    with new_session() as db:
        assert db.get(Run, done["run_id"]).route_tier != "cache", "an answer cached under v1 is not replayed for v2"
    trace = client.get(f"/api/runs/{done['run_id']}", headers=login("admin@harbour.demo")).json()
    router = next(s for s in trace["spans"] if s["kind"] == "router")
    assert router["output"]["prompt_version"] == "v2"
    overview = client.get("/api/metrics/overview", headers=login("admin@harbour.demo")).json()
    assert overview["prompt_versions"]["v1"]["runs"] >= 1 and overview["prompt_versions"]["v2"]["runs"] == 1


def test_eval_api_accepts_a_prompt_version(client, login):
    admin = login("admin@harbour.demo")
    run = client.post("/api/evals/runs", json={"prompt_version": "v2"}, headers=admin).json()
    assert run["metrics"]["prompt_version"] == "v2" and run["metrics"]["pass_rate"] == 1.0
    assert client.post("/api/evals/runs", json={"prompt_version": "v9"}, headers=admin).status_code == 400
    prompts = client.get("/api/evals/prompts", headers=admin).json()
    assert {v["version"] for v in prompts["versions"]} == set(PROMPT_VERSIONS)


def test_offline_ab_compare_passes_and_reports_token_change(capsys):
    assert cli.main(["--fresh", "--compare", "v1,v2", "--gate", "0.95"]) == 0
    out = capsys.readouterr().out
    assert "regressions: none" in out and "token change v2 vs v1" in out
