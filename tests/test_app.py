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


def _run(question: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.text_area[0].input(question)
    at.button[0].click().run()
    return at


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
