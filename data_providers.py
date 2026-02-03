# data_providers.py
"""
Unified data providers.

Goals:
1) Export the same function names that agents.py expects
   (fetch_finnhub_profile / quote / recommendations / financials / news
    fetch_fred_macro_timeseries
    fetch_general_web_news)
2) Never crash the pipeline on missing/unauthorized external APIs:
   - FRED 4xx -> return empty bundle
   - Tavily 401/403 -> return empty bundle
3) Provide small FRED series resolver so agents don't pass question as series_id.
"""

from __future__ import annotations

import os
import re
import time
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

import requests
import pandas as pd

# ---------------------------------------------------------------------
# Env helper
# ---------------------------------------------------------------------
def _env(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return v if v not in (None, "") else default

FINNHUB_API_KEY = _env("FINNHUB_API_KEY", "")
TAVILY_API_KEY = _env("TAVILY_API_KEY", "")
FRED_API_KEY = _env("FRED_API_KEY", "")
HTTP_TIMEOUT = float(_env("HTTP_TIMEOUT", "25"))

# ---------------------------------------------------------------------
# Optional tools.py integration (if you already have it)
# We fall back to direct requests if tools.py not present.
# ---------------------------------------------------------------------
try:
    from tools import (
        finnhub_profile as _tools_finnhub_profile,
        finnhub_quote as _tools_finnhub_quote,
        finnhub_recommendation_trends as _tools_finnhub_reco,
        finnhub_company_financials as _tools_finnhub_fin,
        finnhub_company_news as _tools_finnhub_news,
        search_finance_web as _tools_search_finance_web,
    )
    _HAS_TOOLS = True
except Exception:
    _HAS_TOOLS = False


# =====================================================================
# 1) FRED helpers
# =====================================================================

# very small, robust keyword -> FRED series mapping
_FRED_KEYWORD_MAP = [
    (r"\bfed funds\b|\bpolicy rate\b|\bfed rate\b", "FEDFUNDS", "Effective Fed Funds Rate"),
    (r"\binflation\b|\bcpi\b", "CPIAUCSL", "CPI (All Urban Consumers)"),
    (r"\bpce\b", "PCEPI", "PCE Price Index"),
    (r"\bunemployment\b|\bjobless\b", "UNRATE", "Unemployment Rate"),
    (r"\bgdp\b|\bgrowth\b", "GDPC1", "Real GDP"),
    (r"\bhigh yield\b|\bhy spread\b|\bhy oas\b", "BAMLH0A0HYM2", "ICE BofA US HY OAS"),
    (r"\binvestment grade\b|\big spread\b|\big oas\b", "BAMLC0A0CM", "ICE BofA US Corp OAS"),
    (r"\brecession\b", "USRECM", "NBER Recession Indicator"),
]

def resolve_fred_series(question: str) -> List[str]:
    """Map a natural language question to a few likely FRED series IDs."""
    q = (question or "").lower()
    hits = []
    for pat, sid, _title in _FRED_KEYWORD_MAP:
        if re.search(pat, q):
            hits.append(sid)
    # if nothing matches, don't force a bad call
    return list(dict.fromkeys(hits))[:4]


def _fred_get_series_obs(series_id: str, limit: int = 180) -> pd.DataFrame:
    """Fetch observations for a single FRED series."""
    if not FRED_API_KEY or not series_id:
        return pd.DataFrame()

    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    js = r.json()
    obs = js.get("observations", [])
    if not obs:
        return pd.DataFrame()

    df = pd.DataFrame(obs)
    df = df[["date", "value"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna().sort_values("date")
    return df


def fetch_fred_macro_timeseries(
    series_id: Union[str, List[str]],
) -> Dict[str, Any]:
    """
    Expected by agents.py.
    series_id must be a real FRED id or list of ids.
    Returns bundle with tables/charts/sources.
    """
    out = {"tables": [], "charts": [], "sources": []}

    if isinstance(series_id, str):
        series_ids = [series_id]
    else:
        series_ids = series_id or []

    for sid in series_ids:
        try:
            df = _fred_get_series_obs(sid)
            if df.empty:
                continue

            title = next((t for _, s, t in _FRED_KEYWORD_MAP if s == sid), sid)

            out["tables"].append({
                "title": f"FRED: {title} ({sid})",
                "data": df.tail(24).reset_index(drop=True),
            })

            out["charts"].append({
                "title": f"{title} ({sid})",
                "type": "line",
                "x": df["date"].dt.strftime("%Y-%m-%d").tolist(),
                "y": df["value"].tolist(),
                "y_label": title,
            })

            out["sources"].append({
                "title": f"FRED series {sid}",
                "url": f"https://fred.stlouisfed.org/series/{sid}",
                "source": "FRED",
            })
        except Exception:
            # Never crash pipeline because of FRED
            continue

    return out


# =====================================================================
# 2) Finnhub wrappers (export names agents expects)
# =====================================================================

def _finnhub_get(endpoint: str, params: Dict[str, Any]) -> Any:
    if not FINNHUB_API_KEY:
        return None
    base = "https://finnhub.io/api/v1"
    params = dict(params)
    params["token"] = FINNHUB_API_KEY
    r = requests.get(f"{base}{endpoint}", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def fetch_finnhub_profile(ticker: str) -> Dict[str, Any]:
    try:
        if _HAS_TOOLS:
            data = _tools_finnhub_profile(ticker)
        else:
            data = _finnhub_get("/stock/profile2", {"symbol": ticker})
        if not data:
            return {}
        df = pd.DataFrame([data])
        return {
            "tables": [{"title": f"{ticker} Company Profile", "data": df}],
            "charts": [],
            "sources": [{
                "title": "Finnhub company profile",
                "url": "https://finnhub.io/docs/api/company-profile2",
                "source": "Finnhub",
            }],
        }
    except Exception:
        return {}

def fetch_finnhub_quote(ticker: str) -> Dict[str, Any]:
    try:
        if _HAS_TOOLS:
            data = _tools_finnhub_quote(ticker)
        else:
            data = _finnhub_get("/quote", {"symbol": ticker})
        if not data:
            return {}
        df = pd.DataFrame([data])
        return {
            "tables": [{"title": f"{ticker} Quote", "data": df}],
            "charts": [],
            "sources": [{
                "title": "Finnhub quote",
                "url": "https://finnhub.io/docs/api/quote",
                "source": "Finnhub",
            }],
        }
    except Exception:
        return {}

def fetch_finnhub_recommendations(ticker: str) -> Dict[str, Any]:
    try:
        if _HAS_TOOLS:
            data = _tools_finnhub_reco(ticker)
        else:
            data = _finnhub_get("/stock/recommendation", {"symbol": ticker})
        if not data:
            return {}
        df = pd.DataFrame(data)
        return {
            "tables": [{"title": f"{ticker} Analyst Recommendation Trends", "data": df}],
            "charts": [],
            "sources": [{
                "title": "Finnhub recommendation trends",
                "url": "https://finnhub.io/docs/api/stock-recommendation-trends",
                "source": "Finnhub",
            }],
        }
    except Exception:
        return {}

def fetch_finnhub_financials(ticker: str) -> Dict[str, Any]:
    try:
        if _HAS_TOOLS:
            data = _tools_finnhub_fin(ticker)
        else:
            data = _finnhub_get("/stock/financials-reported", {"symbol": ticker})
        if not data:
            return {}
        df = pd.DataFrame(data.get("data", [])[:8])
        return {
            "tables": [{"title": f"{ticker} Financials (Reported)", "data": df}],
            "charts": [],
            "sources": [{
                "title": "Finnhub financials reported",
                "url": "https://finnhub.io/docs/api/financials-reported",
                "source": "Finnhub",
            }],
        }
    except Exception:
        return {}

def fetch_finnhub_news(ticker: str, days_back: int = 30) -> Dict[str, Any]:
    try:
        if _HAS_TOOLS:
            data = _tools_finnhub_news(ticker, days_back)
        else:
            end = int(time.time())
            start = int((datetime.utcnow() - timedelta(days=days_back)).timestamp())
            data = _finnhub_get("/company-news", {"symbol": ticker, "from": start, "to": end})
        if not data:
            return {}
        # keep light: show top 6 headlines
        df = pd.DataFrame(data[:6])[["datetime", "headline", "source", "url"]]
        return {
            "tables": [{"title": f"{ticker} Recent News (Top 6)", "data": df}],
            "charts": [],
            "sources": [{
                "title": "Finnhub company news",
                "url": "https://finnhub.io/docs/api/company-news",
                "source": "Finnhub",
            }],
        }
    except Exception:
        return {}


# =====================================================================
# 3) Tavily / web news (robust auth + never crash)
# =====================================================================

def _tavily_search_direct(query: str, max_results: int = 6) -> List[Dict[str, Any]]:
    if not TAVILY_API_KEY:
        return []

    url = "https://api.tavily.com/search"
    payload = {
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
    }

    # Try legacy api_key body first (old SDKs)
    try:
        r = requests.post(
            url,
            json={**payload, "api_key": TAVILY_API_KEY},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code in (401, 403):
            raise RuntimeError("Legacy auth rejected")
        r.raise_for_status()
        return r.json().get("results", []) or []
    except Exception:
        pass

    # Retry with Bearer auth (current Tavily style)
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code in (401, 403):
            return []
        r.raise_for_status()
        return r.json().get("results", []) or []
    except Exception:
        return []


def fetch_general_web_news(query: str, max_results: int = 6) -> Dict[str, Any]:
    """
    Expected by agents.py.
    Returns short summary + sources list.
    """
    try:
        if _HAS_TOOLS:
            results = _tools_search_finance_web(query, max_results=max_results)
        else:
            results = _tavily_search_direct(query, max_results=max_results)

        sources = []
        bullets = []
        for r in results[:max_results]:
            title = r.get("title") or "Untitled"
            url = r.get("url") or ""
            snippet = r.get("content") or r.get("snippet") or ""
            sources.append({
                "title": title,
                "url": url,
                "source": r.get("source", "web"),
                "snippet": snippet[:240],
            })
            if snippet:
                bullets.append(f"- {title}: {snippet[:160]}")

        summary = "\n".join(bullets)
        return {"summary": summary, "sources": sources}
    except Exception:
        return {}


# ---------------------------------------------------------------------
# Backwards-compatible aliases if any old code imports these names
# ---------------------------------------------------------------------
fetch_finnhub_company_profile = fetch_finnhub_profile
fetch_finnhub_company_quote = fetch_finnhub_quote
