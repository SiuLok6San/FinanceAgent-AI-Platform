"""Smoke tests for run_multiagent_pipeline with the LLM and data APIs mocked."""

import logging

import pandas as pd
import pytest

import agents
import answer_builder
from llm import LLMError


@pytest.fixture
def mocked_data(monkeypatch):
    """Replace every external data call with a small fixed bundle (no network)."""
    table = pd.DataFrame({"date": pd.to_datetime(["2026-08-01", "2026-09-01"]), "value": [4.2, 4.3]})
    monkeypatch.setattr(agents, "resolve_fred_series", lambda q: ["UNRATE"])
    monkeypatch.setattr(
        agents,
        "fetch_fred_macro_timeseries",
        lambda ids: {
            "tables": [{
                "title": "FRED: Unemployment Rate (UNRATE)", "data": table, "provider": "FRED",
                "kind": "timeseries", "series_id": "UNRATE", "frequency": "monthly", "units": "Percent",
                "last_date": "2026-09-01", "topics": ["macro"],
                "source_url": "https://fred.stlouisfed.org/series/UNRATE",
            }],
            "charts": [],
            "sources": [{"title": "FRED series UNRATE", "url": "https://fred.stlouisfed.org/series/UNRATE", "source": "FRED"}],
            "status": [{"provider": "FRED", "item": "UNRATE", "ok": True, "detail": ""}],
        },
    )
    monkeypatch.setattr(agents, "fetch_general_web_news", lambda q: {})


def test_pipeline_returns_report_when_llm_works(monkeypatch, mocked_data):
    calls = []

    def fake_llm(system_prompt, user_prompt, temperature=0.2, **kwargs):
        calls.append(user_prompt)
        return "Unemployment was 4.3% in September 2026 [S1], and will reach 6.1% [S1]."

    monkeypatch.setattr(agents, "call_llm", fake_llm)
    monkeypatch.setattr(answer_builder, "call_llm", fake_llm)

    result = agents.run_multiagent_pipeline("What is the unemployment outlook?")

    assert result["error"] is False
    assert "4.3%" in result["report_text"]
    assert result["citations_used"] == ["S1"]
    assert len(result["key_data_tables"]) == 1
    assert len(calls) == 5  # planner, analyst, reasoner, writer, writer revision
    # analyst, reasoner and writer see the fetched numbers with their source id
    assert all("[S1] FRED: Unemployment Rate (UNRATE)" in p and "latest 4.3 (2026-09-01)" in p for p in calls[1:])
    # the revision pass gets the validator's findings
    assert "VALIDATION FEEDBACK" in calls[4] and '"6.1%"' in calls[4]
    # the fake writer returns the same text, so the revision is not better and the draft stays
    assert result["repair"]["rounds"][0]["accepted"] is False

    v = result["validation"]
    assert v["counts"] == {"figures": 3, "verified": 2, "derived": 0, "unverified": 1}
    assert result["report_text"].startswith("> **Validation:** 2 of 3 figures verified")
    assert result["report_text_raw"] in result["report_text"]  # flagged text is kept
    assert result["tags"] == ["macro"]
    meta = result["run_meta"]
    assert meta["seed"] == agents.llm_settings()["seed"]
    assert set(meta["temperatures"]) == {"planner", "analyst", "reasoner", "writer"}


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
    # The traceback goes to the log instead.
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


def _writer_sequence(monkeypatch, outputs):
    """LLM fake: planner/analyst/reasoner get "notes"; writer calls return the given outputs in order."""
    writer_calls = []

    def fake(system_prompt, user_prompt, temperature=0.2, **kwargs):
        if "Final Writer" in system_prompt:
            writer_calls.append(user_prompt)
            return outputs[len(writer_calls) - 1]
        return "notes"

    monkeypatch.setattr(agents, "call_llm", fake)
    monkeypatch.setattr(answer_builder, "call_llm", fake)
    return writer_calls


def test_feedback_loop_accepts_a_better_revision(monkeypatch, mocked_data):
    draft = "- Unemployment was 4.3% in September 2026.\n- It will reach 6.1% next year [S1].\n"
    fixed = "- Unemployment was 4.3% in September 2026 [S1].\n- It could reach 6.1% (assumption) next year.\n"
    _writer_sequence(monkeypatch, [draft, fixed])

    result = agents.run_multiagent_pipeline("What is the unemployment outlook?")

    rnd = result["repair"]["rounds"][0]
    assert rnd["accepted"] is True and rnd["open_issues_after"] < rnd["open_issues_before"]
    assert result["report_text_raw"] == fixed
    assert result["draft_report_text"] == draft            # the draft is kept, not discarded
    assert result["draft_validation"]["open_issues"] == rnd["open_issues_before"]
    assert result["validation"]["open_issues"] == 0


def test_feedback_loop_rejects_a_worse_revision(monkeypatch, mocked_data):
    draft = "- Unemployment was 4.3% [S1], and may reach 6.1%.\n"
    worse = "- Unemployment was 4.3%, may reach 6.1%, then 7.2% [S9].\n"
    _writer_sequence(monkeypatch, [draft, worse])

    result = agents.run_multiagent_pipeline("What is the unemployment outlook?")

    assert result["repair"]["rounds"][0]["accepted"] is False
    assert result["report_text_raw"] == draft
    assert result["repair"]["rounds"][0]["revised_report_text"] == worse  # kept for inspection
