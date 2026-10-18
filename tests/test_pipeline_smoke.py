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
            "tables": [{"title": "FRED: Unemployment Rate (UNRATE)", "data": table}],
            "charts": [],
            "sources": [{"title": "FRED series UNRATE", "url": "https://fred.stlouisfed.org/series/UNRATE", "source": "FRED"}],
        },
    )
    monkeypatch.setattr(agents, "fetch_general_web_news", lambda q: {})


def test_pipeline_returns_report_when_llm_works(monkeypatch, mocked_data):
    calls = []

    def fake_llm(system_prompt, user_prompt, temperature=0.2, **kwargs):
        calls.append(temperature)
        return "Unemployment was 4.3% in September [S1]."

    monkeypatch.setattr(agents, "call_llm", fake_llm)
    monkeypatch.setattr(answer_builder, "call_llm", fake_llm)

    result = agents.run_multiagent_pipeline("What is the unemployment outlook?")

    assert result["error"] is False
    assert "4.3%" in result["report_text"]
    assert result["citations_used"] == ["S1"]
    assert len(result["key_data_tables"]) == 1
    assert len(calls) == 4  # planner, analyst, reasoner, writer
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
