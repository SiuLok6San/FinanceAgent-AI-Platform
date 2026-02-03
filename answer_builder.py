"""
answer_builder.py
-----------------
Final institutional report builder.

Responsibilities:
- Take multi-agent outputs + data tables + charts + sources
- Produce a clean, readable institutional-style report text
- Insert proper citation markers [S1], [S2], …
- Return a structured dict consumed directly by app.py:
        {
            "report_text": ...,
            "key_data_tables": [...],
            "chart_specs": [...],
            "sources": [...],
            "citations_used": [...]
        }
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any, Dict, List, Optional

from llm import call_llm


# ---------------------------------------------------------------------
# Source Normalization
# ---------------------------------------------------------------------

def normalize_sources(raw: Any) -> List[Dict[str, Any]]:
    """
    Normalize sources into:
      [
        {
           "id": "S1",
           "title": "...",
           "url": "...",
           "source": "...",
           "snippet": "..."
        }, ...
      ]
    """
    if not raw:
        return []

    # If string, attempt JSON
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = [{"title": raw, "url": "", "source": "unknown"}]

    sources = []
    if isinstance(raw, list):
        for i, s in enumerate(raw, start=1):
            if isinstance(s, str):
                entry = {"title": s, "url": "", "source": "unknown", "snippet": ""}
            else:
                entry = {
                    "title": s.get("title") or s.get("name") or f"Source {i}",
                    "url": s.get("url") or s.get("link") or "",
                    "source": s.get("source") or "web",
                    "snippet": s.get("snippet") or s.get("summary") or "",
                }
            entry["id"] = f"S{i}"
            sources.append(entry)

    return sources


def build_sources_block(sources: List[Dict[str, Any]]) -> str:
    """
    For the LLM: show all sources with their IDs.
    """
    if not sources:
        return "SOURCES: (none)"

    lines = ["SOURCES (use citation tags like [S1], [S2] when referencing):"]
    for s in sources:
        snippet = (s.get("snippet") or "").replace("\n", " ")[:200]
        line = f"[{s['id']}] {s.get('title','')} | {s.get('url','')} | {snippet}"
        lines.append(line)
    return "\n".join(lines)


def extract_citations_used(text: str, sources: List[Dict[str, Any]]) -> List[str]:
    ids = {s["id"] for s in sources}
    found = set(re.findall(r"\[(S\d+)\]", text))
    return sorted(id_ for id_ in found if id_ in ids)


# ---------------------------------------------------------------------
# Main Builder
# ---------------------------------------------------------------------

def build_institutional_report(
    question: str,
    agent_outputs: Dict[str, str],
    tags: List[str],
    tier2_subtags: Dict[str, str],
    ctx_text: str,
    data_tables: List[Dict[str, Any]],
    chart_specs: List[Dict[str, Any]],
    raw_sources: Any,
) -> Dict[str, Any]:
    """
    Produce final structured report.
    """
    # Normalize sources
    sources = normalize_sources(raw_sources)
    sources_block = build_sources_block(sources)

    # LLM system prompt
    system_prompt = textwrap.dedent("""
        You are the Final Writer for B17 Institutional Research.

        Objectives:
        - Produce a crisp, structured, professional finance report.
        - Avoid long paragraphs. Prefer bullets + concise sections.
        - Use ONLY provided sources and cite them as [S1], [S2], ...
        - If a claim cannot be sourced → phrase as scenario (e.g., “likely”, “probable”).

        Required Sections:
        1. Executive Summary (3–5 bullets)
        2. Core Analysis (macro / sector / credit / portfolio)
        3. Scenarios (base / upside / downside)
        4. Key Risks & Monitoring
        5. Portfolio / Decision Implications
        6. Conclusion

        Additional rules:
        - DO NOT include tables or charts inside the text. Mention them by name only:
            “See Table 1 for yield-curve shift data.”
        - DO NOT fabricate numbers or dates.
        - Professional tone. No filler language.
    """).strip()

    # Compose user prompt
    agent_block = "\n\n".join(
        [f"--- {k.upper()} ---\n{v}" for k, v in agent_outputs.items() if v]
    )

    # List available tables & charts
    tables_block = "AVAILABLE TABLES:\n"
    if data_tables:
        for i, t in enumerate(data_tables, start=1):
            tables_block += f"- Table {i}: {t.get('title','Untitled')} (cols={t.get('columns',[])})\n"
    else:
        tables_block += "- (none)\n"

    charts_block = "AVAILABLE CHARTS:\n"
    if chart_specs:
        for i, c in enumerate(chart_specs, start=1):
            charts_block += f"- Chart {i}: {c.get('title','Untitled')} ({c.get('type','line')})\n"
    else:
        charts_block += "- (none)\n"

    user_prompt = textwrap.dedent(f"""
        USER QUESTION:
        {question}

        TAGS:
        {tags}

        TIER-2 SPECIALIZATION:
        {tier2_subtags}

        CONTEXT:
        {ctx_text or "(none)"}

        {agent_block}

        {tables_block}
        {charts_block}

        {sources_block}

        Now write the final institutional report.
    """).strip()

    # Call LLM
    try:
        report = call_llm(system_prompt, user_prompt, temperature=0.2)
    except Exception as e:
        report = f"[FINAL WRITER ERROR] {e}"

    # Extract citations actually used
    used = extract_citations_used(report, sources)

    # Return in app.py format
    return {
        "report_text": report,
        "key_data_tables": data_tables,
        "chart_specs": chart_specs,
        "sources": sources,
        "citations_used": used,
    }


# ---------------------------------------------------------------------
# Backward-compatible wrapper
# ---------------------------------------------------------------------

def build_answer(
    question: str,
    agent_outputs: Dict[str, str],
    tags: Optional[List[str]] = None,
    tier2_subtags: Optional[Dict[str, str]] = None,
    ctx_text: str = "",
    data_tables: Optional[List[Dict[str, Any]]] = None,
    chart_specs: Optional[List[Dict[str, Any]]] = None,
    sources: Any = None,
) -> Dict[str, Any]:
    return build_institutional_report(
        question=question,
        agent_outputs=agent_outputs or {},
        tags=tags or [],
        tier2_subtags=tier2_subtags or {},
        ctx_text=ctx_text or "",
        data_tables=data_tables or [],
        chart_specs=chart_specs or [],
        raw_sources=sources,
    )
