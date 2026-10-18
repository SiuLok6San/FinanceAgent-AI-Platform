# data_providers.py
"""
Unified data providers.

Goals:
1) Export the same function names that agents.py expects
   (fetch_finnhub_profile / quote / recommendations / financials / news
    fetch_fred_macro_timeseries
    fetch_general_web_news)
2) Never crash the pipeline on missing/unauthorized external APIs: every
   fetch returns a bundle, and its "status" list says what worked, what
   failed, and why (e.g. a missing API key).
3) Pick FRED series from the question with the rules in
   config/data_sources.toml.

Bundle format:
    {
        "tables":  [table, ...],
        "charts":  [{"title", "type", "x", "y", "y_label"}, ...],
        "sources": [{"title", "url", "source", "snippet"}, ...],
        "status":  [{"provider", "item", "ok", "detail"}, ...],
    }

Each table carries metadata that the validation layer relies on:
    {
        "title", "data" (DataFrame), "provider", "kind" ("timeseries" | "snapshot"),
        "series_id", "frequency", "units", "last_date" (YYYY-MM-DD or None),
        "topics", "source_url",
    }
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Union

import pandas as pd

import tools
from router import matches_any
from settings import load_config
from tools import DataSourceError, MissingKeyError

logger = logging.getLogger("financeagent.data")

_CFG = load_config("data_sources")
_FRED_SERIES = {s["id"]: s for s in _CFG["fred"]["series"]}


def _empty_bundle() -> Dict[str, Any]:
    return {"tables": [], "charts": [], "sources": [], "status": []}


def _status(provider: str, item: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"provider": provider, "item": item, "ok": ok, "detail": detail}


def _guarded(provider: str, item: str, build: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Run one fetch+build step; turn any failure into a status entry instead of an exception."""
    try:
        bundle = build()
        bundle.setdefault("status", []).append(_status(provider, item, True))
        return bundle
    except MissingKeyError as e:
        out = _empty_bundle()
        out["status"].append(_status(provider, item, False, str(e)))
        return out
    except DataSourceError as e:
        logger.warning("%s %s failed: %s", provider, item, e)
        out = _empty_bundle()
        out["status"].append(_status(provider, item, False, str(e)))
        return out
    except Exception as e:  # unexpected payload shape etc.
        logger.warning("%s %s failed", provider, item, exc_info=True)
        out = _empty_bundle()
        out["status"].append(_status(provider, item, False, f"{type(e).__name__}: {e}"))
        return out


def _merge(bundles: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = _empty_bundle()
    for b in bundles:
        for k in out:
            out[k].extend(b.get(k, []))
    return out


# =====================================================================
# 1) FRED
# =====================================================================

def resolve_fred_series(question: str) -> List[str]:
    """Map a natural language question to the FRED series IDs whose keywords it mentions."""
    hits = [s["id"] for s in _CFG["fred"]["series"] if matches_any(question, s["keywords"])]
    # if nothing matches, don't force a bad call
    return hits[: _CFG["fred"]["max_series"]]


def _fred_bundle(sid: str) -> Dict[str, Any]:
    meta = _FRED_SERIES.get(sid, {"title": sid, "units": None, "frequency": "daily", "topics": []})
    limit = _CFG["fred"]["observations"].get(meta["frequency"], 60)
    obs = tools.fred_series_observations(sid, limit=limit)

    df = pd.DataFrame(obs, columns=["date", "value"])
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # FRED uses "." for missing
    df = df.dropna().sort_values("date").reset_index(drop=True)
    if df.empty:
        raise DataSourceError(f"no observations for {sid}")

    url = f"https://fred.stlouisfed.org/series/{sid}"
    last = df.iloc[-1]
    last_date = last["date"].strftime("%Y-%m-%d")
    return {
        "tables": [{
            "title": f"FRED: {meta['title']} ({sid})",
            "data": df,
            "provider": "FRED",
            "kind": "timeseries",
            "series_id": sid,
            "frequency": meta["frequency"],
            "units": meta["units"],
            "last_date": last_date,
            "topics": list(meta["topics"]),
            "source_url": url,
        }],
        "charts": [{
            "title": f"{meta['title']} ({sid})",
            "type": "line",
            "x": df["date"].dt.strftime("%Y-%m-%d").tolist(),
            "y": df["value"].tolist(),
            "y_label": meta["units"] or meta["title"],
        }],
        "sources": [{
            "title": f"FRED series {sid}: {meta['title']}",
            "url": url,
            "source": "FRED",
            "snippet": f"{meta['frequency'].capitalize()} series in {meta['units']}; "
                       f"latest observation {last['value']:g} on {last_date}.",
        }],
    }


def fetch_fred_macro_timeseries(series_id: Union[str, List[str]]) -> Dict[str, Any]:
    """
    Expected by agents.py.
    series_id must be a real FRED id or list of ids.
    """
    series_ids = [series_id] if isinstance(series_id, str) else (series_id or [])
    return _merge([_guarded("FRED", sid, lambda sid=sid: _fred_bundle(sid)) for sid in series_ids])


# =====================================================================
# 2) Finnhub wrappers (export names agents expects)
# =====================================================================

def _snapshot_table(title: str, df: pd.DataFrame, ticker: str, url: str,
                    last_date: Optional[str] = None, frequency: Optional[str] = None) -> Dict[str, Any]:
    return {
        "title": title,
        "data": df.reset_index(drop=True),
        "provider": "Finnhub",
        "kind": "snapshot",
        "series_id": None,
        "frequency": frequency,  # how often it updates; used by the freshness check
        "units": None,
        "last_date": last_date,
        "topics": ["equity"],
        "source_url": url,
        "ticker": ticker,
    }


def _field_table(data: Dict[str, Any], fields: Dict[str, Any]) -> pd.DataFrame:
    """Long-format (field, value) table for the configured fields that are present.

    A field spec is either a label or [label, scale]; numbers are multiplied by
    scale so the table holds plain units (e.g. market cap in USD, not USD millions).
    """
    rows = []
    for key, spec in fields.items():
        label, scale = (spec, 1) if isinstance(spec, str) else spec
        value = data.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)) and scale != 1:
            value = value * scale
        rows.append({"field": label, "value": value})
    return pd.DataFrame(rows, columns=["field", "value"])


def _source(title: str, url: str, snippet: str = "") -> Dict[str, Any]:
    return {"title": title, "url": url, "source": "Finnhub", "snippet": snippet}


def fetch_finnhub_profile(ticker: str) -> Dict[str, Any]:
    def build():
        data = tools.finnhub_profile(ticker)
        if not data:
            raise DataSourceError(f"no profile for {ticker}")
        url = "https://finnhub.io/docs/api/company-profile2"
        return {
            "tables": [_snapshot_table(f"{ticker} Company Profile", _field_table(data, _CFG["finnhub"]["profile_fields"]), ticker, url)],
            "charts": [],
            "sources": [_source(f"Finnhub company profile: {ticker}", url)],
        }
    return _guarded("Finnhub", f"profile {ticker}", build)


def fetch_finnhub_quote(ticker: str) -> Dict[str, Any]:
    def build():
        data = tools.finnhub_quote(ticker)
        if not data or not data.get("c"):
            raise DataSourceError(f"no quote for {ticker}")
        as_of = datetime.fromtimestamp(data["t"], tz=timezone.utc).strftime("%Y-%m-%d") if data.get("t") else None
        url = "https://finnhub.io/docs/api/quote"
        return {
            "tables": [_snapshot_table(f"{ticker} Quote", _field_table(data, _CFG["finnhub"]["quote_fields"]), ticker, url, as_of, "daily")],
            "charts": [],
            "sources": [_source(f"Finnhub quote: {ticker}", url, f"Quote as of {as_of}." if as_of else "")],
        }
    return _guarded("Finnhub", f"quote {ticker}", build)


def fetch_finnhub_recommendations(ticker: str) -> Dict[str, Any]:
    def build():
        data = tools.finnhub_recommendation_trends(ticker)
        if not data:
            raise DataSourceError(f"no recommendation trends for {ticker}")
        cols = ["period", "strongBuy", "buy", "hold", "sell", "strongSell"]
        df = pd.DataFrame(data)
        df = df[[c for c in cols if c in df.columns]].head(_CFG["finnhub"]["recommendation_periods"])
        url = "https://finnhub.io/docs/api/recommendation-trends"
        latest = str(df["period"].iloc[0]) if "period" in df else None
        return {
            "tables": [_snapshot_table(f"{ticker} Analyst Recommendation Trends", df, ticker, url, latest, "monthly")],
            "charts": [],
            "sources": [_source(f"Finnhub recommendation trends: {ticker}", url)],
        }
    return _guarded("Finnhub", f"recommendations {ticker}", build)


def fetch_finnhub_financials(ticker: str) -> Dict[str, Any]:
    """Key financial metrics (P/E, EPS, growth, margins, 52-week range) from /stock/metric."""
    def build():
        data = tools.finnhub_basic_financials(ticker)
        df = _field_table(data.get("metric") or {}, _CFG["finnhub"]["metric_fields"])
        if df.empty:
            raise DataSourceError(f"no metrics for {ticker}")
        url = "https://finnhub.io/docs/api/company-basic-financials"
        return {
            "tables": [_snapshot_table(f"{ticker} Key Financial Metrics", df, ticker, url)],
            "charts": [],
            "sources": [_source(f"Finnhub basic financials: {ticker}", url)],
        }
    return _guarded("Finnhub", f"financials {ticker}", build)


def fetch_finnhub_news(ticker: str, days_back: Optional[int] = None) -> Dict[str, Any]:
    """Recent company news; each article becomes its own citable source."""
    def build():
        cfg = _CFG["finnhub"]
        data = tools.finnhub_company_news(ticker, days_back or cfg["news_days_back"])
        if not data:
            raise DataSourceError(f"no recent news for {ticker}")
        items = data[: cfg["news_max_items"]]
        sources, rows = [], []
        for n in items:
            day = datetime.fromtimestamp(n["datetime"], tz=timezone.utc).strftime("%Y-%m-%d") if n.get("datetime") else ""
            rows.append({"date": day, "headline": n.get("headline", ""), "publisher": n.get("source", ""), "url": n.get("url", "")})
            sources.append({
                "title": n.get("headline") or "Untitled",
                "url": n.get("url", ""),
                "source": f"Finnhub news ({n.get('source', 'unknown')})",
                "snippet": (f"{day}: " if day else "") + (n.get("summary") or "")[:240],
            })
        table = _snapshot_table(f"{ticker} Recent News", pd.DataFrame(rows), ticker, "", rows[0]["date"] or None)
        return {"tables": [table], "charts": [], "sources": sources}
    return _guarded("Finnhub", f"news {ticker}", build)


# =====================================================================
# 3) Tavily web search
# =====================================================================

def fetch_general_web_news(query: str, max_results: Optional[int] = None) -> Dict[str, Any]:
    """
    Expected by agents.py.
    Returns short summary + sources list.
    """
    cfg = _CFG["tavily"]

    def build():
        results = tools.tavily_search(query, max_results or cfg["max_results"], cfg["search_depth"])
        sources, bullets = [], []
        for r in results:
            title = r.get("title") or "Untitled"
            snippet = (r.get("content") or "").replace("\n", " ")[: cfg["snippet_chars"]]
            published = (r.get("published_date") or "")[:10]
            sources.append({
                "title": title,
                "url": r.get("url") or "",
                "source": "web (Tavily)",
                "snippet": (f"{published}: " if published else "") + snippet,
            })
            if snippet:
                bullets.append(f"- {title}: {snippet[:160]}")
        return {"tables": [], "charts": [], "sources": sources, "summary": "\n".join(bullets)}

    out = _guarded("Tavily", "web search", build)
    out.setdefault("summary", "")
    return out


# ---------------------------------------------------------------------
# Backwards-compatible aliases if any old code imports these names
# ---------------------------------------------------------------------
fetch_finnhub_company_profile = fetch_finnhub_profile
fetch_finnhub_company_quote = fetch_finnhub_quote
