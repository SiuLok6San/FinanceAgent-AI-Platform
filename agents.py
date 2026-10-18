# agents.py
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm import call_llm, LLMError, llm_settings
from settings import load_config
from router import route_question
from router_tier2 import detect_tier2_specialization
from answer_builder import build_answer, extract_citations_used, normalize_sources, revise_report
from facts import key_data_block, topics_with_data
from validators import annotate_report, feedback_for_writer, validate_report

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


_PIPELINE_CFG = load_config("pipeline")
TEMPERATURES = _PIPELINE_CFG["temperatures"]
MAX_PROMPT_CHARS = _PIPELINE_CFG["limits"]["max_prompt_chars"]
MAX_NOTE_CHARS = _PIPELINE_CFG["limits"]["max_note_chars"]
REPAIR = _PIPELINE_CFG["repair"]

# --------------------------------------------------------------------------
# Logging: tracebacks go to logs/pipeline.log, never into the report text
# --------------------------------------------------------------------------
LOG_FILE = Path(__file__).resolve().parent / "logs" / "pipeline.log"
logger = logging.getLogger("financeagent")


def _setup_logging() -> None:
    if logger.handlers:
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


_setup_logging()

LLM_UNAVAILABLE_MESSAGE = (
    "The language model could not be reached, so no report was generated. "
    "With a local model, check that Ollama is running and the model is pulled "
    "(`ollama pull llama3`). With the Ollama API, check `OLLAMA_API_KEY` and "
    "`LLM_MODEL_NAME` in `.env`. Then try again."
)
GENERIC_ERROR_MESSAGE = (
    "Something went wrong while generating the report. Please try again; "
    "if it keeps happening, check the log file for details."
)


# --------------------------------------------------------------------------
# LLM wrapper
# --------------------------------------------------------------------------
def ask_llm(system_prompt: str, user_prompt: str, temperature: float = 0.15) -> str:
    # trim to avoid Ollama slowdowns; LLMError propagates to run_multiagent_pipeline
    user_prompt = (user_prompt or "")[:MAX_PROMPT_CHARS]
    return call_llm(system_prompt, user_prompt, temperature=temperature)


def run_planner(question: str) -> str:
    sys = "You are the Planner Agent. Produce a short step-by-step research plan."
    user = f"QUESTION:\n{question}\n\nReturn 4-6 bullets."
    return ask_llm(sys, user, temperature=TEMPERATURES["planner"])


def run_researcher(question: str, ticker: Optional[str]) -> Dict[str, Any]:
    """
    Collect real data (FRED macro, Finnhub company data, Tavily web news).
    Never raises.
    """
    out: Dict[str, Any] = {"tables": [], "charts": [], "sources": [], "notes": [], "status": []}

    # 1) FRED
    try:
        series_ids = resolve_fred_series(question)
        if series_ids:
            fred_bundle = fetch_fred_macro_timeseries(series_ids)
            out["tables"].extend(fred_bundle.get("tables", []))
            out["charts"].extend(fred_bundle.get("charts", []))
            out["sources"].extend(fred_bundle.get("sources", []))
            out["status"].extend(fred_bundle.get("status", []))
    except Exception:
        logger.warning("FRED fetch failed", exc_info=True)

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
                    out["status"].extend(b.get("status", []))
            except Exception:
                logger.warning("Finnhub fetch %s failed for %s", fn.__name__, ticker, exc_info=True)
                continue

    # 3) Web news (Tavily)
    try:
        web = fetch_general_web_news(question)
        if web:
            out["sources"].extend(web.get("sources", []))
            out["status"].extend(web.get("status", []))
            if web.get("summary"):
                out["notes"].append(web["summary"])
    except Exception:
        logger.warning("Web news fetch failed", exc_info=True)

    # keep notes compact
    out["notes"] = [n[:MAX_NOTE_CHARS] for n in out["notes"]]
    return out


def _no_data_line(no_data_topics: List[str]) -> str:
    return "NO DATA FETCHED FOR: " + (", ".join(no_data_topics) if no_data_topics else "(none)")


def run_analyst(question: str, planner_steps: str, research_note: str,
                key_data: str = "", no_data_topics: Optional[List[str]] = None) -> str:
    sys = (
        "You are the Analyst Agent. Use the plan + evidence to write concise, "
        "institutional analysis. Use short bullets, avoid fluff. "
        "Highlight macro, credit (IG vs HY), and portfolio implications. "
        "Take every number from KEY DATA or the research notes and keep its [S#] "
        "citation; never invent figures. For topics under NO DATA FETCHED, say that "
        "no data was fetched and keep that part qualitative."
    )
    # KEY DATA goes before the notes so prompt trimming cuts notes first
    user = (
        f"QUESTION:\n{question}\n\n"
        f"PLANNER STEPS:\n{planner_steps}\n\n"
        f"{key_data or 'KEY DATA: (none fetched)'}\n\n"
        f"{_no_data_line(no_data_topics or [])}\n\n"
        f"RESEARCH NOTES (may be partial):\n{research_note}"
    )
    return ask_llm(sys, user, temperature=TEMPERATURES["analyst"])


def run_reasoner(question: str, analyst_text: str, key_data: str = "") -> str:
    sys = (
        "You are the Reasoner Agent. Remove weak claims and sharpen logic. "
        "Check every number in the draft against KEY DATA: keep it with its [S#] "
        "citation if it is there, otherwise remove it or rewrite the claim without it."
    )
    user = (
        f"QUESTION:\n{question}\n\n"
        f"{key_data or 'KEY DATA: (none fetched)'}\n\n"
        f"ANALYST DRAFT:\n{analyst_text}"
    )
    return ask_llm(sys, user, temperature=TEMPERATURES["reasoner"])


# --------------------------------------------------------------------------
# Main Pipeline (used by app.py)
# --------------------------------------------------------------------------
def _repair_loop(question, draft, validation, key_data, tables, sources, tags, status):
    """Send the validator's findings back to the Writer; keep a revision only if it is better.

    Returns (final report, its validation, list of rounds). The validator, not
    the LLM, decides: a revision is accepted only if it has strictly fewer open
    issues and is not much shorter than the text it replaces.
    """
    report, rounds = draft, []
    if not REPAIR["enabled"]:
        return report, validation, rounds
    for _ in range(REPAIR["max_rounds"]):
        feedback = feedback_for_writer(validation)
        if not feedback:
            break
        entry = {"feedback": feedback, "open_issues_before": validation["open_issues"],
                 "summary_before": validation["summary"]}
        try:
            revised = revise_report(question, report, feedback, key_data, sources)
        except LLMError:
            logger.warning("Revision pass failed; keeping the draft", exc_info=True)
            rounds.append({**entry, "accepted": False, "error": "LLM call failed"})
            break
        revised_validation = validate_report(revised, question, tables, sources, tags, data_status=status)
        accepted = (revised_validation["open_issues"] < validation["open_issues"]
                    and len(revised) >= REPAIR["min_length_ratio"] * len(report))
        rounds.append({**entry, "accepted": accepted, "open_issues_after": revised_validation["open_issues"],
                       "summary_after": revised_validation["summary"], "revised_report_text": revised})
        if not accepted:
            break
        report, validation = revised, revised_validation
    return report, validation, rounds


def _run_meta(run_id: str) -> Dict[str, Any]:
    """Model name and settings stored with every report (and every error)."""
    return {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **llm_settings(),
        "temperatures": dict(TEMPERATURES),
        "repair": dict(REPAIR),
    }


def _error_result(message: str, run_meta: Dict[str, Any]) -> Dict[str, Any]:
    text = f"{message}\n\nReference: `{run_meta['run_id']}` (see `logs/pipeline.log`)."
    return {
        "error": True,
        "answer": text,
        "report_text": text,
        "key_data_tables": [],
        "chart_specs": [],
        "sources": [],
        "citations_used": [],
        "run_meta": run_meta,
    }


def run_multiagent_pipeline(question: str, ticker: Optional[str] = None) -> Dict[str, Any]:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_meta = _run_meta(run_id)
    try:
        tags = route_question(question)
        tier2 = detect_tier2_specialization(question)

        planner = run_planner(question)
        research_bundle = run_researcher(question, ticker)
        tables = research_bundle.get("tables", [])
        # assign [S#] ids now so every agent cites the same ids
        sources = normalize_sources(research_bundle.get("sources", []))
        key_data = key_data_block(tables, sources)
        no_data_topics = [t for t in tags if t not in topics_with_data(tables)]

        analyst = run_analyst(
            question,
            planner_steps=planner,
            research_note="\n".join(research_bundle.get("notes", [])),
            key_data=key_data,
            no_data_topics=no_data_topics,
        )

        reasoner = run_reasoner(question, analyst, key_data=key_data)

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
            data_tables=tables,
            chart_specs=research_bundle.get("charts", []),
            sources=sources,
            key_data=key_data,
            no_data_topics=no_data_topics,
        )

        # deterministic checks on the draft; flagged content is kept, not deleted
        sources = final_answer["sources"]
        status = research_bundle.get("status", [])
        draft = final_answer["report_text"]
        draft_validation = validate_report(draft, question, tables, sources, tags, data_status=status)
        raw_report, validation, rounds = _repair_loop(
            question, draft, draft_validation, key_data, tables, sources, tags, status)

        final_answer["draft_report_text"] = draft
        final_answer["draft_validation"] = draft_validation
        final_answer["repair"] = {"enabled": REPAIR["enabled"], "rounds": rounds}
        final_answer["citations_used"] = extract_citations_used(raw_report, sources)
        final_answer["report_text_raw"] = raw_report
        final_answer["report_text"] = annotate_report(raw_report, validation)
        final_answer["validation"] = validation
        final_answer["tags"] = tags
        final_answer["tier2"] = tier2
        final_answer["data_status"] = research_bundle.get("status", [])

        # compatibility: some UI versions expect "answer"
        final_answer["answer"] = final_answer["report_text"]

        final_answer["error"] = False
        final_answer["run_meta"] = run_meta
        logger.info("Report generated [ref=%s] meta=%s validation=%s", run_id, run_meta, validation["summary"])
        return final_answer

    except LLMError:
        logger.exception("LLM call failed [ref=%s] question=%r ticker=%r", run_id, question, ticker)
        return _error_result(LLM_UNAVAILABLE_MESSAGE, run_meta)
    except Exception:
        logger.exception("Pipeline failed [ref=%s] question=%r ticker=%r", run_id, question, ticker)
        return _error_result(GENERIC_ERROR_MESSAGE, run_meta)
