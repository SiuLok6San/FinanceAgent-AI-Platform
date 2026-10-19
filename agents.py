# agents.py
"""
Multi-agent pipeline (v3): numbers come from the data, words from the LLM.

    Planner    (LLM)   picks topics and data series from a catalog (JSON); falls back to the
                       rule-based router and keyword series if its output is unusable
    Researcher (code)  fetches FRED / Finnhub / Tavily and builds the fact store (fact_store.py)
    Analyst    (LLM)   findings written with {{FACT_ID}} placeholders, never typed numbers
    Reasoner   (LLM)   checks the findings against the fact table, removes weak claims
    Writer     (LLM)   the report, still as a placeholder template
    Renderer   (code)  inserts exact values and [S#] citations (render.py)
    Validator  (code)  checks the rendered report; its findings go back to the Writer once,
                       and the revision is kept only if it has fewer open issues
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from llm import call_llm, LLMError, llm_settings
from settings import load_config
from router import route_question
from router_tier2 import detect_tier2_specialization
from answer_builder import (
    PLACEHOLDER_RULES, build_sources_block, extract_citations_used, normalize_sources, revise_template,
    write_template,
)
from fact_store import build_fact_store, fact_table_block
from facts import topics_with_data
from render import render
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
TAGS = load_config("routing")["tag_order"]
_SERIES = load_config("data_sources")["fred"]["series"]
MAX_SERIES = load_config("data_sources")["fred"]["max_series"]

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


# --------------------------------------------------------------------------
# Planner: structured plan with a rule-based fallback
# --------------------------------------------------------------------------
def _catalog() -> str:
    return "\n".join(f"- {s['alias']}: {s['title']} ({s['frequency']}; topics: {', '.join(s['topics'])})"
                     for s in _SERIES)


def parse_plan(text: str) -> Optional[Dict[str, Any]]:
    """Keep only topics and series that exist; None if nothing usable came back."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        raw = json.loads(m.group(0))
    except ValueError:
        return None
    by_alias = {s["alias"].upper(): s["id"] for s in _SERIES} | {s["id"].upper(): s["id"] for s in _SERIES}
    topics = [t for t in dict.fromkeys(str(x).lower() for x in raw.get("topics") or []) if t in TAGS]
    series = [by_alias[str(x).upper()] for x in raw.get("series") or [] if str(x).upper() in by_alias]
    series = list(dict.fromkeys(series))[:MAX_SERIES]
    steps = [str(x) for x in raw.get("steps") or []][:6]
    if not topics:
        return None
    return {"topics": topics, "series": series, "steps": steps}


def run_planner(question: str) -> Dict[str, Any]:
    sys = (
        "You are the Planner Agent of a financial research assistant. Reply with ONE JSON object and nothing "
        'else, exactly in this shape: {"topics": [...], "series": [...], "steps": [...]}.\n'
        '- "topics": one or more of exactly these strings: ' + ", ".join(f'"{t}"' for t in TAGS) + ".\n"
        f'- "series": up to {MAX_SERIES} IDs copied exactly from the DATA CATALOG, only data that helps answer.\n'
        '- "steps": 3-5 short research steps.'
    )
    raw = ask_llm(sys, f"DATA CATALOG:\n{_catalog()}\n\nQUESTION:\n{question}", temperature=TEMPERATURES["planner"])
    plan = parse_plan(raw)
    if plan is None:
        logger.warning("Planner output unusable, falling back to rules: %r", (raw or "")[:300])
        return {"topics": route_question(question), "series": resolve_fred_series(question), "steps": [],
                "source": "rules", "raw": raw}
    return {**plan, "source": "planner", "raw": raw}


# --------------------------------------------------------------------------
# Researcher (no LLM)
# --------------------------------------------------------------------------
def run_researcher(question: str, ticker: Optional[str], series_ids: List[str]) -> Dict[str, Any]:
    """
    Collect real data (FRED macro, Finnhub company data, Tavily web news).
    Never raises.
    """
    out: Dict[str, Any] = {"tables": [], "charts": [], "sources": [], "notes": [], "status": []}

    def add(bundle: Dict[str, Any]) -> None:
        for k in ("tables", "charts", "sources", "status"):
            out[k].extend(bundle.get(k, []))

    # 1) FRED
    try:
        if series_ids:
            add(fetch_fred_macro_timeseries(series_ids))
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
                add(fn(ticker) or {})
            except Exception:
                logger.warning("Finnhub fetch %s failed for %s", fn.__name__, ticker, exc_info=True)

    # 3) Web news (Tavily)
    try:
        web = fetch_general_web_news(question)
        if web:
            add(web)
            if web.get("summary"):
                out["notes"].append(web["summary"])
    except Exception:
        logger.warning("Web news fetch failed", exc_info=True)

    # keep notes compact
    out["notes"] = [n[:MAX_NOTE_CHARS] for n in out["notes"]]
    return out


# --------------------------------------------------------------------------
# Analyst and Reasoner (placeholders only)
# --------------------------------------------------------------------------
def _no_data_line(no_data_topics: List[str]) -> str:
    return "NO DATA FETCHED FOR: " + (", ".join(no_data_topics) if no_data_topics else "(none)")


def run_analyst(question: str, plan: Dict[str, Any], fact_table: str, sources_block: str,
                no_data_topics: List[str]) -> str:
    sys = (
        "You are the Analyst Agent. Write concise, institutional analysis as short bullets, covering each topic "
        "in the plan. Highlight macro, credit (IG vs HY) and portfolio implications where relevant. For topics "
        "under NO DATA FETCHED, say that no data was fetched and keep that part qualitative.\n\n" + PLACEHOLDER_RULES
    )
    steps = "\n".join(f"- {s}" for s in plan.get("steps") or []) or "(none)"
    user = (
        f"QUESTION:\n{question}\n\nPLAN TOPICS: {', '.join(plan['topics'])}\nPLAN STEPS:\n{steps}\n\n"
        f"{fact_table}\n\n{_no_data_line(no_data_topics)}\n\n{sources_block}"
    )
    return ask_llm(sys, user, temperature=TEMPERATURES["analyst"])


def run_reasoner(question: str, analyst_text: str, fact_table: str) -> str:
    sys = (
        "You are the Reasoner Agent. Review the analyst draft against the FACT TABLE: remove claims the facts do "
        "not support, fix any word that describes a change in the wrong direction, and make sure every number "
        "is a placeholder. Return the corrected bullets only.\n\n" + PLACEHOLDER_RULES
    )
    user = f"QUESTION:\n{question}\n\n{fact_table}\n\nANALYST DRAFT:\n{analyst_text}"
    return ask_llm(sys, user, temperature=TEMPERATURES["reasoner"])


# --------------------------------------------------------------------------
# Main Pipeline (used by app.py)
# --------------------------------------------------------------------------
def _repair_loop(question, template, validation, check, fact_table, sources):
    """Send the validator's findings back to the Writer; keep a revision only if it is better.

    check(template) -> (rendered text, refs, validation). Returns (final template, its
    rendered text, refs, validation, list of rounds). The validator, not the LLM,
    decides: a revision is kept only if it has strictly fewer open issues and is not
    much shorter than the template it replaces.
    """
    text, refs = None, None
    rounds = []
    if not REPAIR["enabled"]:
        return template, text, refs, validation, rounds
    for _ in range(REPAIR["max_rounds"]):
        feedback = feedback_for_writer(validation)
        if not feedback:
            break
        entry = {"feedback": feedback, "open_issues_before": validation["open_issues"],
                 "summary_before": validation["summary"]}
        try:
            revised = revise_template(question, template, feedback, fact_table, sources)
        except LLMError:
            logger.warning("Revision pass failed; keeping the draft", exc_info=True)
            rounds.append({**entry, "accepted": False, "error": "LLM call failed"})
            break
        r_text, r_refs, r_validation = check(revised)
        accepted = (r_validation["open_issues"] < validation["open_issues"]
                    and len(revised) >= REPAIR["min_length_ratio"] * len(template))
        rounds.append({**entry, "accepted": accepted, "open_issues_after": r_validation["open_issues"],
                       "summary_after": r_validation["summary"], "revised_report_text": revised})
        if not accepted:
            break
        template, text, refs, validation = revised, r_text, r_refs, r_validation
    return template, text, refs, validation, rounds


def _run_meta(run_id: str) -> Dict[str, Any]:
    """Model name and settings stored with every report (and every error)."""
    return {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **llm_settings(),
        "temperatures": dict(TEMPERATURES),
        "repair": dict(REPAIR),
        "pipeline": "v3",
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


def _refs_json(refs) -> List[Dict[str, Any]]:
    return [{"start": r.start, "end": r.end, "kind": r.kind, "fact_id": r.fact_id, "source_id": r.source_id}
            for r in refs or []]


def run_multiagent_pipeline(question: str, ticker: Optional[str] = None) -> Dict[str, Any]:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_meta = _run_meta(run_id)
    try:
        rule_tags = route_question(question)
        tier2 = detect_tier2_specialization(question)

        plan = run_planner(question)
        tags = plan["topics"]
        research = run_researcher(question, ticker, plan["series"])
        tables = research["tables"]
        # assign [S#] ids now so every agent cites the same ids
        sources = normalize_sources(research["sources"])
        store = build_fact_store(tables)
        fact_table = fact_table_block(store, sources)
        sources_block = build_sources_block(sources)
        no_data_topics = [t for t in tags if t not in topics_with_data(tables)]

        analyst = run_analyst(question, plan, fact_table, sources_block, no_data_topics)
        reasoner = run_reasoner(question, analyst, fact_table)
        template = write_template(question, analyst, reasoner, tags, tier2, fact_table, no_data_topics,
                                  sources, tables, research.get("charts", []))

        status = research.get("status", [])

        def check(tpl: str) -> Tuple[str, Any, Dict[str, Any]]:
            text, refs = render(tpl, store, sources)
            return text, refs, validate_report(text, question, tables, sources, tags, data_status=status, refs=refs)

        draft_text, draft_refs, draft_validation = check(template)
        final_tpl, final_text, final_refs, validation, rounds = _repair_loop(
            question, template, draft_validation, check, fact_table, sources)
        if final_text is None:  # no revision kept
            final_tpl, final_text, final_refs, validation = template, draft_text, draft_refs, draft_validation

        result = {
            "error": False,
            "report_template": final_tpl,
            "report_text_raw": final_text,
            "report_text": annotate_report(final_text, validation),
            "refs": _refs_json(final_refs),
            "validation": validation,
            "draft_report_template": template,
            "draft_report_text": draft_text,
            "draft_validation": draft_validation,
            "draft_refs": _refs_json(draft_refs),
            "repair": {"enabled": REPAIR["enabled"], "rounds": rounds},
            "plan": {k: plan[k] for k in ("topics", "series", "steps", "source")},
            "plan_raw": plan.get("raw", ""),
            "rule_tags": rule_tags,
            "tags": tags,
            "tier2": tier2,
            "facts": {k: {"display": e.display, "label": e.label, "kind": e.kind} for k, e in store.items()},
            "key_data_tables": tables,
            "chart_specs": research.get("charts", []),
            "sources": sources,
            "citations_used": extract_citations_used(final_text, sources),
            "data_status": status,
            "agent_outputs": {"analyst": analyst, "reasoner": reasoner},
            "run_meta": run_meta,
        }
        # compatibility: some UI versions expect "answer"
        result["answer"] = result["report_text"]
        logger.info("Report generated [ref=%s] meta=%s validation=%s", run_id, run_meta, validation["summary"])
        return result

    except LLMError:
        logger.exception("LLM call failed [ref=%s] question=%r ticker=%r", run_id, question, ticker)
        return _error_result(LLM_UNAVAILABLE_MESSAGE, run_meta)
    except Exception:
        logger.exception("Pipeline failed [ref=%s] question=%r ticker=%r", run_id, question, ticker)
        return _error_result(GENERIC_ERROR_MESSAGE, run_meta)
