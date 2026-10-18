"""Tests for the data layer with every HTTP call mocked (no network)."""

from datetime import datetime, timezone

import pytest

import data_providers as dp
import tools


def _ts(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


# --- FRED series selection ---------------------------------------------------

@pytest.mark.parametrize(
    "question, must_include",
    [
        ("Are high-yield spreads tight?", {"BAMLH0A0HYM2"}),
        ("How have investment-grade corporate bond spreads moved?", {"BAMLC0A0CM"}),
        ("How does higher-for-longer affect IG vs HY credit spreads?", {"BAMLH0A0HYM2", "BAMLC0A0CM"}),
        ("What does the yield curve signal?", {"T10Y2Y", "DGS10", "DGS2"}),
        ("What is the outlook for the federal funds rate?", {"FEDFUNDS"}),
        ("Is inflation cooling?", {"CPIAUCSL"}),
        ("What does the unemployment rate say?", {"UNRATE"}),
    ],
)
def test_resolve_fred_series(question, must_include):
    assert must_include <= set(dp.resolve_fred_series(question))


def test_resolve_fred_series_respects_max_and_no_match():
    many = "Fed, yield curve, mortgage, high yield, inflation, unemployment, GDP and oil"
    assert len(dp.resolve_fred_series(many)) == dp._CFG["fred"]["max_series"]
    assert dp.resolve_fred_series("Tell me a story") == []


# --- FRED bundle -------------------------------------------------------------

def test_fred_bundle_sorts_drops_missing_and_sets_metadata(monkeypatch):
    obs = [  # FRED returns newest first; "." means missing
        {"date": "2026-09-01", "value": "4.3"},
        {"date": "2026-08-01", "value": "."},
        {"date": "2026-07-01", "value": "4.2"},
    ]
    monkeypatch.setattr(tools, "fred_series_observations", lambda sid, limit: obs)

    b = dp.fetch_fred_macro_timeseries(["UNRATE"])

    assert b["status"] == [{"provider": "FRED", "item": "UNRATE", "ok": True, "detail": ""}]
    t = b["tables"][0]
    assert list(t["data"]["value"]) == [4.2, 4.3]
    assert t["last_date"] == "2026-09-01"
    assert (t["kind"], t["series_id"], t["frequency"], t["units"]) == ("timeseries", "UNRATE", "monthly", "Percent")
    assert t["source_url"] == b["sources"][0]["url"] == "https://fred.stlouisfed.org/series/UNRATE"
    assert b["charts"][0]["x"] == ["2026-07-01", "2026-09-01"]


def test_missing_key_is_reported_not_raised(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(dp.tools, "env", lambda k, d="": "")
    b = dp.fetch_fred_macro_timeseries("UNRATE")
    assert b["tables"] == []
    assert b["status"][0]["ok"] is False
    assert "FRED_API_KEY is not set" in b["status"][0]["detail"]


def test_http_error_is_reported_not_raised(monkeypatch):
    def boom(sid, limit):
        raise tools.DataSourceError("HTTP 500 from FRED")
    monkeypatch.setattr(tools, "fred_series_observations", boom)
    b = dp.fetch_fred_macro_timeseries("UNRATE")
    assert b["tables"] == [] and b["status"][0]["detail"] == "HTTP 500 from FRED"


# --- Finnhub -----------------------------------------------------------------

def test_financials_table_is_built_from_metric_endpoint(monkeypatch):
    # Regression: the old code read data["data"] from /stock/metric and always got an empty table.
    monkeypatch.setattr(tools, "finnhub_basic_financials", lambda t: {"metric": {"peTTM": 14.2, "epsTTM": 18.5}, "series": {}})
    b = dp.fetch_finnhub_financials("JPM")
    df = b["tables"][0]["data"]
    assert dict(zip(df["field"], df["value"])) == {"P/E (TTM)": 14.2, "EPS (TTM)": 18.5}


def test_profile_converts_market_cap_to_usd(monkeypatch):
    monkeypatch.setattr(tools, "finnhub_profile", lambda t: {"name": "Apple Inc", "marketCapitalization": 3_500_000})
    df = dp.fetch_finnhub_profile("AAPL")["tables"][0]["data"]
    values = dict(zip(df["field"], df["value"]))
    assert values["Market capitalization (USD)"] == pytest.approx(3.5e12)
    assert values["Company"] == "Apple Inc"


def test_quote_uses_readable_labels_and_date(monkeypatch):
    monkeypatch.setattr(tools, "finnhub_quote", lambda t: {"c": 190.5, "dp": -1.2, "t": _ts("2026-10-16")})
    t = dp.fetch_finnhub_quote("AAPL")["tables"][0]
    assert set(t["data"]["field"]) == {"Current price", "Percent change (%)"}
    assert t["last_date"] == "2026-10-16"


def test_each_news_article_is_its_own_source(monkeypatch):
    news = [
        {"headline": "A", "url": "https://x/a", "source": "Reuters", "summary": "sa", "datetime": _ts("2026-10-16")},
        {"headline": "B", "url": "https://x/b", "source": "CNBC", "summary": "sb", "datetime": _ts("2026-10-15")},
    ]
    monkeypatch.setattr(tools, "finnhub_company_news", lambda t, d: news)
    b = dp.fetch_finnhub_news("AAPL")
    assert [s["url"] for s in b["sources"]] == ["https://x/a", "https://x/b"]
    assert b["sources"][0]["snippet"].startswith("2026-10-16: ")


# --- Tavily ------------------------------------------------------------------

def test_web_news_returns_sources_and_summary(monkeypatch):
    results = [{"title": "T", "url": "https://w/t", "content": "Spreads widened.", "published_date": "2026-10-15T12:00:00Z"}]
    monkeypatch.setattr(tools, "tavily_search", lambda q, n, depth: results)
    b = dp.fetch_general_web_news("credit spreads")
    assert b["sources"][0]["snippet"] == "2026-10-15: Spreads widened."
    assert "Spreads widened." in b["summary"]


def test_web_news_failure_gives_empty_summary(monkeypatch):
    def boom(q, n, depth):
        raise tools.DataSourceError("HTTP 401 from Tavily")
    monkeypatch.setattr(tools, "tavily_search", boom)
    b = dp.fetch_general_web_news("x")
    assert b["sources"] == [] and b["summary"] == "" and b["status"][0]["ok"] is False
