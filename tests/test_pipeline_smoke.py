"""Smoke tests for the v3 pipeline with the LLM and data APIs mocked."""

import json
import logging

import pandas as pd
import pytest

import agents
import answer_builder
from llm import LLMError

PLAN = json.dumps({"topics": ["macro"], "series": ["UNRATE"], "steps": ["check the unemployment trend"]})


@pytest.fixture
def mocked_data(monkeypatch):
    """Replace every external data call with a small fixed bundle (no network)."""
    table = pd.DataFrame({"date": pd.to_datetime(["2026-08-01", "2026-09-01"]), "value": [4.2, 4.3]})
    fetched = []

    def fred(ids):
        fetched.append(list(ids))
        return {
            "tables": [{
                "title": "FRED: Unemployment Rate (UNRATE)", "data": table, "provider": "FRED",
                "kind": "timeseries", "series_id": "UNRATE", "frequency": "monthly", "units": "Percent",
                "last_date": "2026-09-01", "topics": ["macro"],
                "source_url": "https://fred.stlouisfed.org/series/UNRATE",
            }],
            "charts": [],
            "sources": [{"title": "FRED series UNRATE", "url": "https://fred.stlouisfed.org/series/UNRATE", "source": "FRED"}],
            "status": [{"provider": "FRED", "item": "UNRATE", "ok": True, "detail": ""}],
        }

    monkeypatch.setattr(agents, "fetch_fred_macro_timeseries", fred)
    monkeypatch.setattr(agents, "fetch_general_web_news", lambda q: {})
    return fetched


def fake_llm(monkeypatch, plan=PLAN, writer=("- Unemployment is {{UNRATE.latest}}.",)):
    """Planner -> plan; Analyst / Reasoner -> notes; Writer calls -> the given templates in order."""
    calls = {"planner": [], "analyst": [], "reasoner": [], "writer": []}

    def fake(system_prompt, user_prompt, temperature=0.2, **kwargs):
        if "Planner Agent" in system_prompt:
            calls["planner"].append(user_prompt)
            return plan
        if "Analyst Agent" in system_prompt:
            calls["analyst"].append(user_prompt)
            return "- unemployment {{UNRATE.latest}}"
        if "Reasoner Agent" in system_prompt:
            calls["reasoner"].append(user_prompt)
            return "- unemployment {{UNRATE.latest}}"
        calls["writer"].append(user_prompt)
        return writer[min(len(calls["writer"]), len(writer)) - 1]

    monkeypatch.setattr(agents, "call_llm", fake)
    monkeypatch.setattr(answer_builder, "call_llm", fake)
    return calls


def test_pipeline_renders_placeholders_with_values_and_citations(monkeypatch, mocked_data):
    calls = fake_llm(monkeypatch, writer=(
        "- Unemployment was {{UNRATE.latest}} in {{UNRATE.latest_date}}, {{UNRATE.chg_1m}} over a month.\n"
        "- It could reach {{assume: 6.1%}} next year.\n",))

    result = agents.run_multiagent_pipeline("What is the unemployment outlook?")

    assert result["error"] is False
    assert result["plan"]["source"] == "planner" and mocked_data == [["UNRATE"]]
    assert result["report_text_raw"] == (
        "- Unemployment was 4.3% [S1] in September 2026, +0.1 pp over a month.\n"
        "- It could reach 6.1% (assumption) next year.\n")
    v = result["validation"]
    assert v["open_issues"] == 0 and result["repair"]["rounds"] == []
    assert {f["origin"] for f in v["figures"]} == {"fact", "assumption"}
    # every agent after the planner saw the fact table
    assert all("{{UNRATE.latest}} = 4.3%" in p for p in calls["analyst"] + calls["reasoner"] + calls["writer"])
    assert result["report_text"].startswith("> **Validation:**")
    assert result["facts"]["UNRATE.latest"]["display"] == "4.3%"


def test_unusable_plan_falls_back_to_rules(monkeypatch, mocked_data):
    fake_llm(monkeypatch, plan="I think we should look at jobs data.")
    result = agents.run_multiagent_pipeline("What does the unemployment rate say about the economy?")
    assert result["plan"]["source"] == "rules"
    assert result["tags"] == result["rule_tags"] == ["macro"]
    assert mocked_data == [["UNRATE", "GDPC1"]]  # keyword fallback: "unemployment", "economy"


def test_feedback_loop_keeps_a_better_revision(monkeypatch, mocked_data):
    draft = "- Unemployment fell {{UNRATE.chg_1m}} to 4.3%.\n"            # wrong direction + typed number
    fixed = "- Unemployment rose {{UNRATE.chg_1m}} to {{UNRATE.latest}}.\n"
    calls = fake_llm(monkeypatch, writer=(draft, fixed))

    result = agents.run_multiagent_pipeline("What is the unemployment outlook?")

    rnd = result["repair"]["rounds"][0]
    assert rnd["accepted"] is True and rnd["open_issues_before"] > rnd["open_issues_after"] == 0
    assert "fix the wording" in calls["writer"][1] and "typed directly" in calls["writer"][1]
    assert result["report_template"] == fixed and result["draft_report_template"] == draft
    assert result["report_text_raw"] == "- Unemployment rose +0.1 pp [S1] to 4.3%.\n"
    assert result["draft_validation"]["directions"][0]["word"] == "fell"


def test_feedback_loop_rejects_a_worse_revision(monkeypatch, mocked_data):
    draft = "- Unemployment is {{UNRATE.latest}}, maybe 5%.\n"
    worse = "- Unemployment is 4.3%, maybe 5%, or 6%, {{UNRATE.nope}}.\n"
    fake_llm(monkeypatch, writer=(draft, worse))

    result = agents.run_multiagent_pipeline("What is the unemployment outlook?")

    assert result["repair"]["rounds"][0]["accepted"] is False
    assert result["report_template"] == draft
    assert result["repair"]["rounds"][0]["revised_report_text"] == worse  # kept for inspection


def test_llm_failure_gives_friendly_message_and_logs_traceback(monkeypatch, mocked_data, caplog):
    def broken_llm(*args, **kwargs):
        raise LLMError("connection refused")

    monkeypatch.setattr(agents, "call_llm", broken_llm)
    monkeypatch.setattr(answer_builder, "call_llm", broken_llm)

    with caplog.at_level(logging.ERROR, logger="financeagent"):
        result = agents.run_multiagent_pipeline("What is the unemployment outlook?")

    assert result["error"] is True
    assert result["report_text"].startswith(agents.LLM_UNAVAILABLE_MESSAGE)
    assert "Traceback" not in result["report_text"]
    assert "connection refused" not in result["report_text"]
    assert result["run_meta"]["run_id"] in result["report_text"]
    assert any(r.exc_info for r in caplog.records)


def test_unexpected_failure_gives_generic_message(monkeypatch, mocked_data, caplog):
    def broken_router(question):
        raise ValueError("boom")

    monkeypatch.setattr(agents, "route_question", broken_router)

    with caplog.at_level(logging.ERROR, logger="financeagent"):
        result = agents.run_multiagent_pipeline("anything")

    assert result["error"] is True
    assert result["report_text"].startswith(agents.GENERIC_ERROR_MESSAGE)
    assert "boom" not in result["report_text"]
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)


def test_parse_plan_keeps_only_known_topics_and_series():
    plan = agents.parse_plan('Sure! {"topics": ["Credit", "spreads"], "series": ["hy_oas", "BAMLC0A0CM", "SPX"], "steps": ["a"]}')
    assert plan == {"topics": ["credit"], "series": ["BAMLH0A0HYM2", "BAMLC0A0CM"], "steps": ["a"]}
    assert agents.parse_plan('{"topics": ["stuff"], "series": ["HY_OAS"]}') is None
    assert agents.parse_plan("no json here") is None
