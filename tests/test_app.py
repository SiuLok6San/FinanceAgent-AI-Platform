"""UI test: run app.py headlessly with the pipeline mocked (streamlit.testing)."""

from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import agents
import answer_builder

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def mocked_pipeline(monkeypatch):
    table = pd.DataFrame({"date": pd.to_datetime(["2026-08-01", "2026-09-01"]), "value": [4.2, 4.3]})
    monkeypatch.setattr(agents, "resolve_fred_series", lambda q: ["UNRATE"])
    monkeypatch.setattr(agents, "fetch_fred_macro_timeseries", lambda ids: {
        "tables": [{"title": "FRED: Unemployment Rate (UNRATE)", "data": table, "provider": "FRED",
                    "kind": "timeseries", "series_id": "UNRATE", "frequency": "monthly", "units": "Percent",
                    "last_date": "2026-09-01", "topics": ["macro"],
                    "source_url": "https://fred.stlouisfed.org/series/UNRATE"}],
        "charts": [{"title": "Unemployment Rate (UNRATE)", "type": "line",
                    "x": ["2026-08-01", "2026-09-01"], "y": [4.2, 4.3], "y_label": "Percent"}],
        "sources": [{"title": "FRED series UNRATE", "url": "https://fred.stlouisfed.org/series/UNRATE", "source": "FRED"}],
        "status": [],
    })
    monkeypatch.setattr(agents, "fetch_general_web_news", lambda q: {})
    fake = lambda *a, **k: "Unemployment is 4.3% [S1] and may reach 6.1% [S1]; it costs $5."  # noqa: E731
    monkeypatch.setattr(agents, "call_llm", fake)
    monkeypatch.setattr(answer_builder, "call_llm", fake)


LIVE = "Live analysis"


def _run(question: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.radio[0].set_value(LIVE).run()
    at.text_area[0].input(question)
    at.button[0].click().run()
    return at


@pytest.fixture
def no_api(monkeypatch):
    """Fail the test if anything tries to call the LLM or a data API."""
    def boom(*a, **k):
        raise AssertionError("example mode must not call any API")
    for mod, name in [(agents, "call_llm"), (answer_builder, "call_llm"), (agents, "fetch_general_web_news"),
                      (agents, "fetch_fred_macro_timeseries")]:
        monkeypatch.setattr(mod, name, boom)


def test_example_mode_is_default_and_calls_no_api(no_api, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.radio[0].value.startswith("Example runs")
    assert at.selectbox[0].value == "Q01"
    assert any("Saved run" in i.value for i in at.info)
    assert any("Validation:" in w.value for w in [*at.warning, *at.success])
    assert [s.value for s in at.subheader][:2] == ["Input", "Final Institutional Report"]

    at.selectbox[0].set_value("Q13").run()   # equity example with Finnhub tables
    assert not at.exception
    assert any("Key Data Tables" == s.value for s in at.subheader)


def test_live_mode_needs_the_password_when_one_is_set(mocked_pipeline, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "open-sesame")
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.radio[0].set_value(LIVE).run()
    assert at.button[0].disabled is True

    at.text_input[1].input("wrong").run()
    assert at.button[0].disabled is True and at.error[0].value == "Wrong password."

    at.text_input[1].input("open-sesame").run()
    assert at.button[0].disabled is False
    at.text_area[0].input("What is the unemployment rate?")
    at.button[0].click().run()
    assert any("Final Institutional Report" == s.value for s in at.subheader)


def test_report_validation_panel_and_highlight(mocked_pipeline):
    at = _run("What is the unemployment rate?")
    assert not at.exception
    assert any("Validation:" in w.value and "1 of 3 figures verified" in w.value for w in at.warning)
    report = next(m.value for m in at.markdown if "Unemployment is" in m.value)
    assert ":orange-background[6.1%]" in report     # unverified figure highlighted, not removed
    assert "\\$5" in report                          # "$" escaped so it is not rendered as LaTeX
    assert [m.label for m in at.metric] == ["Verified", "Derived", "Unverified", "Open issues", "Dangling citations"]
    assert [m.value for m in at.metric] == ["1", "0", "2", "2", "0"]
    # the fake writer repeats itself, so the revision is not kept and the UI says so
    assert any("revision was tried but not kept" in c.value for c in at.caption)
    assert any("First draft" in e.label for e in at.expander)
    assert [s.value for s in at.subheader][-4:] == ["Validation", "Key Data Tables", "Charts", "Sources"]


def test_pipeline_error_is_shown_as_message(monkeypatch):
    from llm import LLMError

    def broken(*a, **k):
        raise LLMError("connection refused")
    monkeypatch.setattr(agents, "call_llm", broken)
    at = _run("anything")
    assert not at.exception
    assert at.error and at.error[0].value.startswith("The language model could not be reached")
