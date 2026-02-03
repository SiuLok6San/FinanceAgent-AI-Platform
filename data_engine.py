"""
data_providers.py
-----------------
Unified real-data provider layer for:
- FRED macro data (charts + tables)
- Finnhub equity data (profile, quote, recommendations, financials)
- News (Tavily general finance)

Returns clean, structured objects:
{
    "tables": [...],
    "charts": [...],
    "sources": [...]
}
"""

from __future__ import annotations

import requests
import pandas as pd
from typing import Any, Dict, List, Optional

# -------------------------------------------------------------------
# SET YOUR KEYS HERE (or through .env)
# -------------------------------------------------------------------
import os
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")


# -------------------------------------------------------------------
# Helper
# -------------------------------------------------------------------
def _safe_get(url: str, params: dict) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _build_table(title: str, df: pd.DataFrame) -> Dict[str, Any]:
    return {
        "title": title,
        "columns": list(df.columns),
        "data": df,
    }


def _build_chart(title: str, df: pd.DataFrame, chart_type: str = "line") -> Dict[str, Any]:
    return {
        "title": title,
        "type": chart_type,
        "data": df,
    }


def _build_source(title: str, url: str, provider: str, snippet: str = "") -> Dict[str, Any]:
    return {
        "title": title,
        "url": url,
        "source": provider,
        "snippet": snippet,
    }


# -------------------------------------------------------------------
# 1. FRED Macro Data
# -------------------------------------------------------------------
def fetch_fred_macro_timeseries(question: str) -> Optional[Dict[str, Any]]:
    """
    Returns key macro indicators **based on user's question**.
    If the question mentions:
      - rates / yield curve / fed → return EFFR
      - recession / growth → return GDP
      - credit spreads → BAA vs AAA
    """
    # Map keywords → FRED series
    mapping = []

    q = question.lower()

    if any(k in q for k in ["rate", "fed", "yield", "curve", "ffr"]):
        mapping.append(("Federal Funds Rate", "DFF"))

    if any(k in q for k in ["gdp", "growth", "economy"]):
        mapping.append(("Real GDP", "GDPC1"))

    if any(k in q for k in ["credit", "spread", "ig", "hy"]):
        mapping.append(("AAA Corporate Bond Yield", "AAA"))
        mapping.append(("BAA Corporate Bond Yield", "BAA"))

    if not mapping:
        return None

    tables = []
    charts = []
    sources = []

    for title, fred_code in mapping:
        url = f"https://api.stlouisfed.org/fred/series/observations"

        params = {
            "api_key": FRED_API_KEY,
            "series_id": fred_code,
            "file_type": "json",
        }

        data = _safe_get(url, params)
        if not data or "observations" not in data:
            continue

        df = pd.DataFrame(data["observations"])
        df = df[df["value"] != "."]
        df["value"] = pd.to_numeric(df["value"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")[["value"]]

        tables.append(_build_table(title, df))
        charts.append(_build_chart(title, df))
        sources.append(_build_source(title, url, "FRED API"))

    return {
        "tables": tables,
        "charts": charts,
        "sources": sources,
    }


# -------------------------------------------------------------------
# 2. Finnhub Equity Data
# -------------------------------------------------------------------
def _finnhub_get(endpoint: str, params: dict) -> Optional[dict]:
    try:
        url = f"https://finnhub.io/api/v1/{endpoint}"
        params["token"] = FINNHUB_API_KEY
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None


def fetch_finnhub_profile(ticker: str) -> Optional[Dict[str, Any]]:
    data = _finnhub_get("stock/profile2", {"symbol": ticker})
    if not data:
        return None

    df = pd.DataFrame([data])
    return {
        "tables": [_build_table(f"{ticker} Profile", df)],
        "charts": [],
        "sources": [
            _build_source(f"{ticker} Profile", "https://finnhub.io/api/v1/stock/profile2", "Finnhub API")
        ],
    }


def fetch_finnhub_quote(ticker: str) -> Optional[Dict[str, Any]]:
    data = _finnhub_get("quote", {"symbol": ticker})
    if not data:
        return None

    df = pd.DataFrame([data])
    return {
        "tables": [_build_table(f"{ticker} Real-Time Quote", df)],
        "charts": [],
        "sources": [
            _build_source(f"{ticker} Quote", "https://finnhub.io/api/v1/quote", "Finnhub API")
        ],
    }


def fetch_finnhub_recommendations(ticker: str) -> Optional[Dict[str, Any]]:
    data = _finnhub_get("stock/recommendation", {"symbol": ticker})
    if not data:
        return None

    df = pd.DataFrame(data)
    return {
        "tables": [_build_table(f"{ticker} Analyst Recommendations", df)],
        "charts": [],
        "sources": [
            _build_source(f"{ticker} Recommendations", "https://finnhub.io/api/v1/stock/recommendation", "Finnhub API")
        ],
    }


def fetch_finnhub_financials(ticker: str) -> Optional[Dict[str, Any]]:
    data = _finnhub_get("stock/financials-reported", {"symbol": ticker})
    if not data or "data" not in data:
        return None

    df = pd.json_normalize(data["data"])
    return {
        "tables": [_build_table(f"{ticker} Financial Reports", df)],
        "charts": [],
        "sources": [
            _build_source(f"{ticker} Financials", "https://finnhub.io/api/v1/stock/financials-reported", "Finnhub API")
        ],
    }


def fetch_finnhub_news(ticker: str) -> Optional[Dict[str, Any]]:
    data = _finnhub_get("company-news", {"symbol": ticker, "from": "2024-01-01", "to": "2025-12-31"})
    if not data:
        return None

    # Only return 5 items
    sources = [
        _build_source(
            title=n.get("headline", ""),
            url=n.get("url", ""),
            provider="Finnhub News",
            snippet=n.get("summary", ""),
        )
        for n in data[:5]
    ]

    return {"tables": [], "charts": [], "sources": sources}


# -------------------------------------------------------------------
# 3. Tavily Web News Summary
# -------------------------------------------------------------------
def fetch_general_web_news(question: str) -> Optional[Dict[str, Any]]:
    if not TAVILY_API_KEY:
        return None

    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": question,
                "include_answer": True,
                "max_tokens": 2048,
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None

        data = r.json()

        # Build source list
        sources = []
        for i, entry in enumerate(data.get("results", [])[:5], start=1):
            sources.append(_build_source(
                title=entry.get("title", f"News {i}"),
                url=entry.get("url", ""),
                provider="Tavily",
                snippet=entry.get("snippet", "")
            ))

        return {
            "summary": data.get("answer", ""),
            "sources": sources,
        }

    except:
        return None
