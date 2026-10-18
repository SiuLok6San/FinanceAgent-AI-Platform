# tools.py
"""
tools.py
--------
Lowest-level external data clients for B17 (FRED, Finnhub, Tavily).

Each function makes one HTTP call and returns parsed JSON. Failures raise
DataSourceError (MissingKeyError when the API key is not configured), so
data_providers.py can record *why* data is missing instead of guessing.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

from settings import env, load_config

FINNHUB_BASE = "https://finnhub.io/api/v1"
FRED_BASE = "https://api.stlouisfed.org/fred"
TAVILY_URL = "https://api.tavily.com/search"

HTTP_TIMEOUT = float(env("HTTP_TIMEOUT", str(load_config("data_sources")["http_timeout_sec"])))


class DataSourceError(RuntimeError):
    pass


class MissingKeyError(DataSourceError):
    pass


def _key(name: str) -> str:
    key = env(name)
    if not key:
        raise MissingKeyError(f"{name} is not set")
    return key


def _get_json(url: str, params: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Any:
    try:
        r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        # "from None": the original message can contain the full URL incl. api_key
        raise DataSourceError(f"{type(e).__name__} calling {url}") from None
    if r.status_code != 200:
        raise DataSourceError(f"HTTP {r.status_code} from {url}")
    return r.json()


# ============================================================
# FRED
# ============================================================

def fred_series_observations(series_id: str, limit: int) -> List[Dict[str, str]]:
    """Most recent `limit` observations, newest first: [{"date": "YYYY-MM-DD", "value": "4.3"}, ...]."""
    js = _get_json(f"{FRED_BASE}/series/observations", {
        "series_id": series_id,
        "api_key": _key("FRED_API_KEY"),
        "file_type": "json",
        "sort_order": "desc",
        "limit": int(limit),
    })
    return js.get("observations") or []


# ============================================================
# Finnhub
# ============================================================

def _finnhub(endpoint: str, params: Dict[str, Any]) -> Any:
    # Finnhub accepts the key as a header, which keeps it out of URLs and logs.
    return _get_json(f"{FINNHUB_BASE}/{endpoint}", params, headers={"X-Finnhub-Token": _key("FINNHUB_API_KEY")})


def finnhub_profile(ticker: str) -> Dict[str, Any]:
    return _finnhub("stock/profile2", {"symbol": ticker}) or {}


def finnhub_quote(ticker: str) -> Dict[str, Any]:
    return _finnhub("quote", {"symbol": ticker}) or {}


def finnhub_recommendation_trends(ticker: str) -> List[Dict[str, Any]]:
    return _finnhub("stock/recommendation", {"symbol": ticker}) or []


def finnhub_basic_financials(ticker: str) -> Dict[str, Any]:
    """Key metrics (P/E, EPS, margins, 52-week range, ...) from /stock/metric."""
    return _finnhub("stock/metric", {"symbol": ticker, "metric": "all"}) or {}


def finnhub_company_news(ticker: str, days_back: int) -> List[Dict[str, Any]]:
    end = date.today()
    start = end - timedelta(days=days_back)
    return _finnhub("company-news", {"symbol": ticker, "from": start.isoformat(), "to": end.isoformat()}) or []


# ============================================================
# Tavily web search
# ============================================================

def tavily_search(query: str, max_results: int, search_depth: str) -> List[Dict[str, Any]]:
    """Raw Tavily results: [{"title", "url", "content", "published_date"?}, ...]."""
    key = _key("TAVILY_API_KEY")
    try:
        r = requests.post(
            TAVILY_URL,
            json={
                "query": query.strip(),
                "search_depth": search_depth,
                "max_results": int(max_results),
                "include_answer": False,
                "include_raw_content": False,
            },
            headers={"Authorization": f"Bearer {key}"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise DataSourceError(f"{type(e).__name__} calling Tavily") from None
    if r.status_code != 200:
        raise DataSourceError(f"HTTP {r.status_code} from Tavily")
    return (r.json() or {}).get("results") or []
