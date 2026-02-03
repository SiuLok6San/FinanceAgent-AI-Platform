# tools.py
"""
tools.py
--------
Lowest-level external data utilities for B17.

Guarantees:
- NEVER raises errors to upper layers.
- If API key missing or network fails → return None / [] safely.
- Standard return format for web sources:
    {"title":..., "url":..., "snippet":..., "source":...}

Adds:
- FRED macro/credit data helpers (requests only, no extra deps)
"""

from __future__ import annotations
import os
import requests
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

FINNHUB_BASE = "https://finnhub.io/api/v1"
FRED_BASE = "https://api.stlouisfed.org/fred"


# ============================================================
# Finnhub Helpers
# ============================================================

def _get_finnhub_key() -> str:
    return os.getenv("FINNHUB_API_KEY", "").strip()

def _finnhub_get(endpoint: str, params: Dict[str, Any]) -> Optional[Any]:
    key = _get_finnhub_key()
    if not key:
        return None
    try:
        params = dict(params or {})
        params["token"] = key
        r = requests.get(f"{FINNHUB_BASE}/{endpoint}", params=params, timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def finnhub_profile(ticker: str) -> Optional[Dict[str, Any]]:
    if not ticker:
        return None
    return _finnhub_get("stock/profile2", {"symbol": ticker})

def finnhub_quote(ticker: str) -> Optional[Dict[str, Any]]:
    if not ticker:
        return None
    return _finnhub_get("quote", {"symbol": ticker})

def finnhub_recommendation_trends(ticker: str) -> Optional[List[Dict[str, Any]]]:
    if not ticker:
        return None
    return _finnhub_get("stock/recommendation", {"symbol": ticker})

def finnhub_company_financials(ticker: str) -> Optional[Dict[str, Any]]:
    if not ticker:
        return None
    return _finnhub_get("stock/metric", {"symbol": ticker, "metric": "all"})

def finnhub_company_news(ticker: str, days_back: int = 30) -> Optional[List[Dict[str, Any]]]:
    if not ticker:
        return None
    try:
        end = datetime.utcnow().date()
        start = end - timedelta(days=days_back)
        return _finnhub_get("company-news", {
            "symbol": ticker,
            "from": start.isoformat(),
            "to": end.isoformat(),
        })
    except Exception:
        return None


# ============================================================
# Tavily Finance Web Search
# ============================================================

def _get_tavily_key() -> str:
    return os.getenv("TAVILY_API_KEY", "").strip()

def search_finance_web(query: str, max_results: int = 6) -> List[Dict[str, Any]]:
    key = _get_tavily_key()
    if not key or not query or not query.strip():
        return []

    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query.strip(),
                "search_depth": "advanced",
                "max_results": int(max_results),
            },
            timeout=20
        )
        if r.status_code != 200:
            return []

        data = r.json() or {}
        results = data.get("results", []) or []

        cleaned = []
        for x in results:
            if not isinstance(x, dict):
                continue
            cleaned.append({
                "title": x.get("title") or "Web Source",
                "url": x.get("url") or "",
                "snippet": (x.get("content") or "")[:320].replace("\n", " "),
                "source": "web",
            })
        return cleaned

    except Exception:
        return []


# ============================================================
# FRED Helpers (Macro + Credit Real Data)
# ============================================================

def _get_fred_key() -> str:
    return os.getenv("FRED_API_KEY", "").strip()

def fred_series_observations(series_id: str, start_date: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    Returns list of observations:
      [{"date":"YYYY-MM-DD","value":"x.x"}, ...]

    If fails → None
    """
    key = _get_fred_key()
    if not key or not series_id:
        return None

    params = {
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
    }
    if start_date:
        params["observation_start"] = start_date

    try:
        r = requests.get(f"{FRED_BASE}/series/observations", params=params, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        return data.get("observations", None)
    except Exception:
        return None

def fred_latest_value(series_id: str) -> Optional[float]:
    obs = fred_series_observations(series_id)
    if not obs:
        return None
    # find latest numeric value
    for x in reversed(obs):
        v = x.get("value")
        try:
            if v not in (".", None):
                return float(v)
        except Exception:
            continue
    return None
