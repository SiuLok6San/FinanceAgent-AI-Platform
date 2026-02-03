# agents.py
from __future__ import annotations

import traceback
from typing import Any, Dict, Optional

from llm import call_llm, LLMError
from router import route_question
from router_tier2 import detect_tier2_specialization
from answer_builder import build_answer

from data_providers import (
    resolve_fred_series,
    fetch_fred_macro_timeseries,
    fetch_finnhub_profile,
    fetch_finnhub_quote,
    fetch_finnhub_recommendations,
    fetch_finnhub_financials,
    fetch_finnhub_news,
    fetch_general_web_news,
)


# --------------------------------------------------------------------------
# LLM wrapper
# --------------------------------------------------------------------------
def safe_llm(system_prompt: str, user_prompt: str, temperature: float = 0.15) -> str:
    # trim to avoid Ollama slowdowns
    user_prompt = (user_prompt or "")[:12000]
    try:
        return call_llm(system_prompt, user_prompt, temperature=temperature)
    except LLMError as e:
        return f"[LLM ERROR] {e}"


def run_planner(question: str) -> str:
    sys = "You are the Planner Agent. Produce a short step-by-step research plan."
    user = f"QUESTION:\n{question}\n\nReturn 4-6 bullets."
    return safe_llm(sys, user, temperature=0.1)


def run_researcher(question: str, ticker: Optional[str]) -> Dict[str, Any]:
    """
    Collect real data (FRED macro, Finnhub company data, Tavily web news).
    Never raises.
    """
    out: Dict[str, Any] = {"tables": [], "charts": [], "sources": [], "notes": []}

    # 1) FRED
    try:
        series_ids = resolve_fred_series(question)
        if series_ids:
            fred_bundle = fetch_fred_macro_timeseries(series_ids)
            out["tables"].extend(fred_bundle.get("tables", []))
            out["charts"].extend(fred_bundle.get("charts", []))
            out["sources"].extend(fred_bundle.get("sources", []))
    except Exception:
        pass

    # 2) Finnhub (only if ticker)
    if ticker:
        for fn in [
            fetch_finnhub_profile,
            fetch_finnhub_quote,
            fetch_finnhub_recommendations,
            fetch_finnhub_financials,
            fetch_finnhub_news,
        ]:
            try:
                b = fn(ticker)
                if b:
                    out["tables"].extend(b.get("tables", []))
                    out["charts"].extend(b.get("charts", []))
                    out["sources"].extend(b.get("sources", []))
            except Exception:
                continue

    # 3) Web news (Tavily)
    try:
        web = fetch_general_web_news(question)
        if web:
            out["sources"].extend(web.get("sources", []))
            if web.get("summary"):
                out["notes"].append(web["summary"])
    except Exception:
        pass

    # keep notes compact
    out["notes"] = [n[:2000] for n in out["notes"]]
    return out


def run_analyst(question: str, planner_steps: str, research_note: str) -> str:
    sys = (
        "You are the Analyst Agent. Use the plan + evidence to write concise, "
        "institutional analysis. Use short bullets, avoid fluff. "
        "Highlight macro, credit (IG vs HY), and portfolio implications."
    )
    user = (
        f"QUESTION:\n{question}\n\n"
        f"PLANNER STEPS:\n{planner_steps}\n\n"
        f"RESEARCH NOTES (may be partial):\n{research_note}"
    )
    return safe_llm(sys, user)


def run_reasoner(question: str, analyst_text: str) -> str:
    sys = "You are the Reasoner Agent. Remove weak claims and sharpen logic."
    user = f"QUESTION:\n{question}\n\nANALYST DRAFT:\n{analyst_text}"
    return safe_llm(sys, user)


# --------------------------------------------------------------------------
# Main Pipeline (used by app.py)
# --------------------------------------------------------------------------
def run_multiagent_pipeline(question: str, ticker: Optional[str] = None) -> Dict[str, Any]:
    try:
        tags = route_question(question)
        tier2 = detect_tier2_specialization(question)

        planner = run_planner(question)
        research_bundle = run_researcher(question, ticker)

        analyst = run_analyst(
            question,
            planner_steps=planner,
            research_note="\n".join(research_bundle.get("notes", [])),
        )

        reasoner = run_reasoner(question, analyst)

        final_answer = build_answer(
            question=question,
            agent_outputs={
                "planner": planner,
                "analyst": analyst,
                "reasoner": reasoner,
            },
            tags=tags,
            tier2_subtags=tier2,
            ctx_text="",
            data_tables=research_bundle.get("tables", []),
            chart_specs=research_bundle.get("charts", []),
            sources=research_bundle.get("sources", []),
        )

        # compatibility: some UI versions expect "answer"
        if "answer" not in final_answer and "report_text" in final_answer:
            final_answer["answer"] = final_answer["report_text"]

        return final_answer

    except Exception as e:
        return {
            "answer": f"[CRITICAL ERROR]\n{e}\n{traceback.format_exc()}",
            "report_text": f"[CRITICAL ERROR]\n{e}\n{traceback.format_exc()}",
            "key_data": {"tables": []},
            "charts": [],
            "sources": [],
            "citations_used": [],
        }
