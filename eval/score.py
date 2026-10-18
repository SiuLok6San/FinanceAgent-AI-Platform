"""
Score saved evaluation results and write eval/RESULTS.md.

    python eval/score.py

- Routing accuracy: the original and the current rule-based routers, and
  the v3 Planner's topics as used, against the expected tags (dev and
  held-out questions separately).
- Report metrics: the current validators.py is applied to the saved outputs
  of every pipeline (for the original: validation turned on, nothing else
  changed), using each record's run date for the freshness check. v3 reports
  are re-rendered from their placeholder templates, so each number's origin
  (fact, quote, assumption, typed) is known exactly.

Every number in RESULTS.md is computed here from the saved files; the
hand-written context comes from eval/NOTES.md.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    EVAL_DIR, PIPELINES, REPO, RESULTS_DIR, extract_original_code,
    load_questions, read_json, table_from_json, write_json,
)

sys.path.insert(0, str(REPO))
from fact_store import build_fact_store  # noqa: E402
from render import render  # noqa: E402
from router import route_question as route_current  # noqa: E402
from settings import load_config  # noqa: E402
from validators import mislabelled_assumption, validate_report  # noqa: E402

SETS = ["fresh", "heldout", "dev"]
SET_TITLES = {"fresh": "Fresh held-out questions (10, never used during development)",
              "heldout": "Held-out questions (10, frozen before v3 was built; H05, H10 once used in a Planner smoke test)",
              "dev": "Development questions (20, also used while building v2 and v3)"}
SET_SHORT = {"fresh": "Fresh", "heldout": "Held-out", "dev": "Dev"}
# report columns: v3 is shown twice, first draft and final (after the feedback loop)
COLUMNS = [("original", "report"), ("v2", "report"), ("v3", "draft"), ("v3", "report")]
COLUMN_LABELS = {("original", "report"): "Original (Dec 2025)", ("v2", "report"): "v2 (validation + feedback)",
                 ("v3", "draft"): "v3 first draft", ("v3", "report"): "v3 final"}

# Temperatures hard-wired in the original code (agents.py / answer_builder.py at 0468eea).
ORIGINAL_TEMPERATURES = {"planner": 0.1, "analyst": 0.15, "reasoner": 0.15, "writer": 0.2}

# Series the original code fetched that the current config no longer lists.
ORIGINAL_ONLY_SERIES = {"USRECM": {"title": "NBER Recession Indicator", "units": None, "frequency": "monthly", "topics": ["macro"]}}

# Source URLs the original Finnhub fetchers attached to each table (by title suffix).
ORIGINAL_FINNHUB_URLS = {
    " Company Profile": "https://finnhub.io/docs/api/company-profile2",
    " Quote": "https://finnhub.io/docs/api/quote",
    " Analyst Recommendation Trends": "https://finnhub.io/docs/api/stock-recommendation-trends",
    " Financials (Reported)": "https://finnhub.io/docs/api/financials-reported",
    " Recent News (Top 6)": "https://finnhub.io/docs/api/company-news",
}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _original_router():
    path = extract_original_code() / "router.py"
    spec = importlib.util.spec_from_file_location("router_original", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.route_question


def routing_metrics(questions, tags_for) -> Dict[str, Any]:
    """tags_for(question row) -> list of tags, or None if not available."""
    rows, tp, fp, fn = [], 0, 0, 0
    for q in questions:
        got = tags_for(q)
        if got is None:
            continue
        got, exp = set(got), set(q["expected_tags"])
        tp, fp, fn = tp + len(got & exp), fp + len(got - exp), fn + len(exp - got)
        rows.append({"id": q["id"], "expected": sorted(exp), "got": sorted(got),
                     "exact": got == exp, "primary": q["category"] in got})
    return {
        "n": len(rows),
        "exact": sum(r["exact"] for r in rows),
        "primary": sum(r["primary"] for r in rows),
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Validation of saved outputs
# ---------------------------------------------------------------------------

def _original_tables(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Give the original pipeline's data the metadata the validators need.

    The original FRED tables kept only the last 24 rows, but its charts hold
    every observation it fetched, so the full series is rebuilt from charts.
    """
    series_cfg = {s["id"]: s for s in load_config("data_sources")["fred"]["series"]} | ORIGINAL_ONLY_SERIES
    tables = []
    for ch in rec["charts"]:
        m = re.search(r"\(([A-Z0-9]+)\)\s*$", ch.get("title", ""))
        if not m or not ch.get("x"):
            continue
        sid = m.group(1)
        meta = series_cfg.get(sid, {})
        df = pd.DataFrame({"date": pd.to_datetime(ch["x"]), "value": ch["y"]})
        tables.append({
            "title": f"FRED: {ch['title']}", "data": df, "provider": "FRED", "kind": "timeseries",
            "series_id": sid, "frequency": meta.get("frequency"), "units": meta.get("units"),
            "last_date": df["date"].max().strftime("%Y-%m-%d"), "topics": meta.get("topics", []),
            "source_url": f"https://fred.stlouisfed.org/series/{sid}",
        })
    for t in rec["tables"]:
        if t["title"].startswith("FRED:"):
            continue  # rebuilt from charts above
        table = table_from_json(t)
        url = next((u for suffix, u in ORIGINAL_FINNHUB_URLS.items() if t["title"].endswith(suffix)), "")
        table.update({"provider": "Finnhub", "kind": "snapshot", "topics": ["equity"], "source_url": url,
                      "frequency": None, "last_date": None})
        tables.append(table)
    return tables


def _run_date(rec: Dict[str, Any]) -> date:
    return datetime.fromisoformat(rec["run_utc"]).astimezone(timezone.utc).date()


def record_set(rec: Dict[str, Any]) -> str:
    return rec.get("set") or {"H": "heldout", "F": "fresh"}.get(rec["id"][0], "dev")


def validate_record(rec: Dict[str, Any], pipeline: str, which: str = "report") -> Optional[Dict[str, Any]]:
    """Validate the final report (which="report") or the first draft (which="draft")."""
    tables = _original_tables(rec) if pipeline == "original" else [table_from_json(t) for t in rec["tables"]]
    tags = rec.get("tags") or rec["router_tags"]
    template = rec.get("report_template") if which == "report" else rec.get("draft_report_template")
    text = rec["report_text"] if which == "report" else rec.get("draft_report_text")
    refs = None
    if template:
        text, refs = render(template, build_fact_store(tables), rec["sources"])
    if text is None:
        return None
    v = validate_report(text, rec["question"], tables, rec["sources"], tags,
                        data_status=rec.get("data_status"), today=_run_date(rec), refs=refs)
    v["n_tables"] = len(tables)
    return v


def _untraceable(f: Dict[str, Any]) -> bool:
    """Not from the data, not found in any fetched source text, and not labelled an assumption."""
    return f["status"] == "unverified" and not f["assumption"] and not f["snippet_ids"]


def report_metrics(records: List[Dict[str, Any]], vals: List[Dict[str, Any]]) -> Dict[str, Any]:
    num = [f for v in vals for f in v["figures"] if f["unit"] != "date"]
    nd_topics = [t for v in vals for t in v["no_data"]["topics_without_data"]]
    grounded = [f for f in num if f["status"] != "unverified"]
    elapsed = [r["elapsed_sec"] for r in records]
    return {
        "questions": len(records),
        "reports": len(vals),
        "reports_with_data": sum(v["n_tables"] > 0 for v in vals),
        "numbers": len(num),
        "traceable": len(grounded),
        "in_source_text": sum(f["status"] == "unverified" and not f["assumption"] and bool(f["snippet_ids"]) for f in num),
        "assumptions": sum(f["status"] == "unverified" and f["assumption"] for f in num),
        "assumptions_from_cited_source": sum(bool(mislabelled_assumption(f)) for f in num),
        "untraceable": sum(_untraceable(f) for f in num),
        "from_facts": sum(f.get("origin") == "fact" for f in num),
        "typed": sum(f.get("origin") == "free" for f in num),
        "placeholder_mode": bool(vals) and all(v.get("placeholders") for v in vals),
        "grounded_low_precision": sum(f["tolerance"] >= 0.5 and f.get("origin") != "fact" for f in grounded),
        "direction_errors": sum(len(v.get("directions", [])) for v in vals),
        "comparison_errors": sum(len(v.get("comparisons", [])) for v in vals),
        "history_wording_errors": sum(len((v.get("wording") or {}).get("history", [])) for v in vals),
        "trend_wording_errors": sum(len((v.get("wording") or {}).get("trend", [])) for v in vals),
        "reports_with_direction_errors": sum(bool(v.get("directions")) for v in vals),
        "citation_markers": sum(v["citations"]["markers"] for v in vals),
        "dangling_ids": sum(len(v["citations"]["dangling"]) for v in vals),
        "uncited_paragraphs": sum(len(v["citations"]["uncited_paragraphs"]) for v in vals),
        "source_mismatches": sum(len(v["citations"]["source_mismatches"]) for v in vals),
        "stale_series": sum(r["status"] == "stale" for v in vals for r in v["freshness"]),
        "no_data_topics": len(nd_topics),
        "no_data_undisclosed": sum(not t["disclosed"] for t in nd_topics),
        "reports_with_no_data_issue": sum(v["no_data"]["issue"] for v in vals),
        "open_issues": sum(v["open_issues"] for v in vals),
        "mean_elapsed_sec": round(sum(elapsed) / len(elapsed), 1) if elapsed else None,
    }


def score_pipeline(pipeline: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Metrics per (question set, report|draft) plus per-question rows and loop / planner stats."""
    out: Dict[str, Any] = {"errors": [r["id"] for r in records if r["error"]], "sets": {}}
    for which_set in SETS:
        recs = [r for r in records if record_set(r) == which_set]
        if not recs:
            continue
        ok = [r for r in recs if not r["error"]]
        entry: Dict[str, Any] = {}
        for which in ("report", "draft"):
            vals = [validate_record(r, pipeline, which) for r in ok]
            if which == "draft" and any(v is None for v in vals):
                continue  # no drafts saved for this pipeline
            entry[which] = report_metrics(recs, vals)
            entry[which]["per_question"] = [{
                "id": r["id"], "category": r["category"], "tables": v["n_tables"],
                "numbers": sum(f["unit"] != "date" for f in v["figures"]),
                "traceable": sum(f["unit"] != "date" and f["status"] != "unverified" for f in v["figures"]),
                "untraceable": sum(f["unit"] != "date" and _untraceable(f) for f in v["figures"]),
                "directions": len(v.get("directions", [])), "uncited": len(v["citations"]["uncited_paragraphs"]),
                "no_data_issue": v["no_data"]["issue"], "open_issues": v["open_issues"],
            } for r, v in zip(ok, vals)]
        rounds = [r["repair"]["rounds"][0] for r in ok if (r.get("repair") or {}).get("rounds")]
        entry["loop"] = {"questions": len(ok), "attempted": len(rounds), "kept": sum(x["accepted"] for x in rounds),
                         "rejected": sum(not x["accepted"] and not x.get("error") for x in rounds),
                         "failed": sum(bool(x.get("error")) for x in rounds)}
        plans = [r.get("plan") for r in ok if r.get("plan")]
        entry["planner"] = {"runs": len(plans), "usable": sum(p.get("source") == "planner" for p in plans)}
        out["sets"][which_set] = entry
    return out


# ---------------------------------------------------------------------------
# RESULTS.md
# ---------------------------------------------------------------------------

def _pct(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.0%})" if b else "—"


def _run_info(pipeline: str) -> Optional[Dict[str, Any]]:
    path = RESULTS_DIR / pipeline / "run_info.json"
    return read_json(path) if path.exists() else None


def _producing_sessions(info: Dict[str, Any], recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each saved record, the latest session that started before it ran and included its id."""
    used = []
    for rec in recs:
        cands = [s for s in info["sessions"] if rec["id"] in s["ids"] and s["started_utc"] <= rec["run_utc"]]
        if cands:
            used.append(max(cands, key=lambda s: s["started_utc"]))
    return used


def _setup_table(infos: Dict[str, Any], records: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    cols = [p for p in PIPELINES if p in records]

    def cell(p, fn):
        info, recs = infos.get(p), records.get(p, [])
        return fn(_producing_sessions(info, recs), recs) if info and recs else "not run"

    def dates(sessions, recs):
        ds = sorted({r["run_utc"][:10] for r in recs})
        return ds[0] if len(ds) == 1 else f"{ds[0]} to {ds[-1]}"

    def temps(sessions, recs):
        seen = {tuple(r["run_meta"]["temperatures"].items()) for r in recs if r["run_meta"].get("temperatures")}
        return "; ".join(", ".join(f"{k} {v}" for k, v in t) for t in sorted(seen)) or (
            ", ".join(f"{k} {v}" for k, v in ORIGINAL_TEMPERATURES.items()) + " (hard-coded)")

    def loop(sessions, recs):
        seen = {(m["enabled"], m["max_rounds"]) for r in recs if (m := r["run_meta"].get("repair"))}
        return "; ".join(f"{n} revision, kept only if fewer open issues" if on else "off" for on, n in sorted(seen)) or "none"

    def counts(sessions, recs):
        return ", ".join(f"{sum(record_set(r) == s for r in recs)} {s}" for s in SETS
                         if any(record_set(r) == s for r in recs))

    rows = [
        ("Code", lambda s, r: "; ".join(sorted({x["code"] for x in s}))),
        ("Run dates (UTC)", dates),
        ("Model", lambda s, r: ", ".join(sorted({x["llm"]["model"] for x in s}))),
        ("Endpoint", lambda s, r: ", ".join(sorted({x["llm"]["api_base"] for x in s}))),
        ("Seed", lambda s, r: ", ".join(sorted({str(x["llm"]["seed"]) for x in s}))),
        ("Temperatures", temps),
        ("Feedback loop", loop),
        ("Questions run", counts),
    ]
    out = ["| | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for label, fn in rows:
        out.append(f"| {label} | " + " | ".join(cell(p, fn) for p in cols) + " |")
    return out


def _metrics_table(scores: Dict[str, Any], which_set: str) -> List[str]:
    cols = [(p, w) for p, w in COLUMNS if w in scores.get(p, {}).get("sets", {}).get(which_set, {})]
    if not cols:
        return ["No results for this set yet."]
    m = {c: scores[c[0]]["sets"][which_set][c[1]] for c in cols}
    lines = ["| Metric | " + " | ".join(COLUMN_LABELS[c] for c in cols) + " |", "|---" * (len(cols) + 1) + "|"]

    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(m[c]) for c in cols) + " |")

    row("Reports (pipeline errors)", lambda x: f"{x['reports']} ({x['questions'] - x['reports']})")
    row("Reports with any fetched data table", lambda x: _pct(x["reports_with_data"], x["reports"]))
    row("Numbers in the reports (dates excluded)", lambda x: str(x["numbers"]))
    row("… traceable to fetched data (exact or derived)", lambda x: _pct(x["traceable"], x["numbers"]))
    row("… found in a fetched web/news source text", lambda x: _pct(x["in_source_text"], x["numbers"]))
    row("… labelled as the writer's assumption", lambda x: _pct(x["assumptions"], x["numbers"]))
    row("… assumption labels on a number in the text of a source the line cites",
        lambda x: str(x["assumptions_from_cited_source"]))
    row("… **untraceable** (none of the above)", lambda x: _pct(x["untraceable"], x["numbers"]))
    row("Numbers inserted from the fact table / typed by the model",
        lambda x: f"{x['from_facts']} / {x['typed']}" if x["placeholder_mode"] else "— / all")
    row("Change figures described in the wrong direction (reports)",
        lambda x: f"{x['direction_errors']} ({x['reports_with_direction_errors']})")
    row("Comparisons contradicted by the two facts' values or units", lambda x: str(x["comparison_errors"]))
    row("History wording contradicted by the percentile (\"near lows\", \"tight\")",
        lambda x: str(x["history_wording_errors"]))
    row("Trend wording contradicted by the recent change (\"widening\")", lambda x: str(x["trend_wording_errors"]))
    row("Paragraphs with grounded figures but no citation", lambda x: str(x["uncited_paragraphs"]))
    row("Paragraphs citing a different source than the data", lambda x: str(x["source_mismatches"]))
    row("Dangling citation ids", lambda x: str(x["dangling_ids"]))
    row("Routed topics with no data (not disclosed)", lambda x: f"{x['no_data_topics']} ({x['no_data_undisclosed']})")
    row("Reports with a no-data honesty issue", lambda x: _pct(x["reports_with_no_data_issue"], x["reports"]))
    row("Stale data series", lambda x: str(x["stale_series"]))
    row("Mean time per question (s)", lambda x: str(x["mean_elapsed_sec"]))
    return lines


def write_results_md(routing, scores, infos, records, determinism) -> str:
    lines = [
        "# Evaluation results",
        "",
        f"Generated by `eval/score.py` on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from the saved "
        "outputs in `eval/results/`. All numbers are reported exactly as computed, including the bad ones.",
        "",
        "Pipelines: **original** = the Dec 2025 course code (tag `v1.0-course`), run unchanged except for the LLM "
        "client; **v2** = tag `v2.0-validation` (validator + one feedback round on free-text reports); **v3** = this "
        "version (the LLM writes fact placeholders; code inserts the numbers). The same current `validators.py` "
        "scores every report.",
        "",
        "## Setup",
        "",
        *_setup_table(infos, records),
    ]
    for which_set in SETS:
        lines += ["", f"## {SET_TITLES[which_set]}", "", *_metrics_table(scores, which_set)]
        v3 = scores.get("v3", {}).get("sets", {}).get(which_set)
        if v3:
            lp, pl = v3["loop"], v3["planner"]
            lines += ["", f"v3 feedback loop: revision requested for {lp['attempted']}/{lp['questions']} questions, "
                      f"kept {lp['kept']}, rejected {lp['rejected']}, failed {lp['failed']}. "
                      f"v3 Planner output usable (valid topics from the allowed list): {_pct(pl['usable'], pl['runs'])}; "
                      "otherwise the rule-based router and keyword series were used."]

    lines += ["", "## Routing accuracy", "",
              "| Router | " + " | ".join(f"{SET_SHORT[s]}: exact tag set" for s in SETS) + " | "
              + " | ".join(f"{SET_SHORT[s]}: precision / recall" for s in SETS) + " |",
              "|---" * (1 + 2 * len(SETS)) + "|"]
    for name, label in [("rules_original", "Rule-based, original keywords"),
                        ("rules_current", "Rule-based, current keywords"),
                        ("planner", "v3 Planner (LLM), topics as used ¹")]:
        cells = []
        for s in SETS:
            r = routing.get(name, {}).get(s)
            cells.append(_pct(r["exact"], r["n"]) if r and r["n"] else "—")
        for s in SETS:
            r = routing.get(name, {}).get(s)
            cells.append(f"{r['precision']:.2f} / {r['recall']:.2f}" if r and r["n"] else "—")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += ["", "¹ When the Planner's output was unusable, the topics come from the current rule-based router. "
              "The current keyword rules were fixed after seeing the development questions; the held-out questions "
              "were not used to change them."]

    for p in ("v3", "v2", "original"):
        for which_set in SETS:
            rep = scores.get(p, {}).get("sets", {}).get(which_set, {}).get("report")
            if not rep:
                continue
            lines += ["", f"### Per question: {p}, {which_set}", "",
                      "| Question | Tables | Numbers | Traceable | Untraceable | Wrong direction | Uncited | No-data issue |",
                      "|---|---|---|---|---|---|---|---|"]
            for q in rep["per_question"]:
                lines.append(f"| {q['id']} ({q['category']}) | {q['tables']} | {q['numbers']} | {q['traceable']} | "
                             f"{q['untraceable']} | {q['directions']} | {q['uncited']} | "
                             f"{'yes' if q['no_data_issue'] else 'no'} |")
        if scores.get(p, {}).get("errors"):
            lines.append(f"\nPipeline errors ({p}, excluded above): {', '.join(scores[p]['errors'])}")

    if determinism:
        lines += ["", "## Determinism check", "",
                  f"Same prompt sent {determinism['repeats']} times to {determinism['model']} with seed "
                  f"{determinism['seed']} and temperature {determinism['temperature']} on {determinism['date']}: "
                  f"{determinism['distinct_outputs']} distinct output(s)."]

    notes = EVAL_DIR / "NOTES.md"
    if notes.exists():
        lines += ["", "## Notes on these runs", "", notes.read_text().strip()]

    lines += [
        "",
        "## How to read these numbers",
        "",
        "- *Traceable* means the number equals a fetched value or a simple calculation on fetched values, within the "
        "rounding its digits imply. For free text (original, v2) that is a match by value, so coarse numbers can "
        "match by coincidence; for v3 fact placeholders the link is exact.",
        "- *Untraceable* numbers are neither in the data, nor in a fetched web/news text, nor labelled as the "
        "writer's assumption. They are the closest measure here of numbers the model made up.",
        "- *Wrong direction* compares words such as \"rose\" or \"narrowed\" next to a change figure with the sign of "
        "the change it matches. For free text the match is by value, so some flags can be coincidences.",
        "- In v3, a number typed by the model instead of a placeholder counts as an open issue even when it is right, "
        "so open-issue totals are not comparable across versions and are left out of the comparison tables.",
        "- 10 questions per held-out set are a small sample; live data and model output change from day to day.",
    ]
    text = "\n".join(lines) + "\n"
    (EVAL_DIR / "RESULTS.md").write_text(text)
    return text


def main() -> None:
    questions = load_questions("all")
    records, scores, infos = {}, {}, {}
    for p in PIPELINES:
        files = sorted((RESULTS_DIR / p).glob("[QHF][0-9]*.json"))
        if files:
            records[p] = [read_json(f) for f in files]
            scores[p] = score_pipeline(p, records[p])
            infos[p] = _run_info(p)

    by_id = {r["id"]: r for r in records.get("v3", []) if not r["error"]}
    orig_router = _original_router()
    routing: Dict[str, Dict[str, Any]] = {"rules_original": {}, "rules_current": {}, "planner": {}}
    for s in SETS:
        qs = [q for q in questions if q["set"] == s]
        routing["rules_original"][s] = routing_metrics(qs, lambda q: orig_router(q["question"]))
        routing["rules_current"][s] = routing_metrics(qs, lambda q: route_current(q["question"]))
        routing["planner"][s] = routing_metrics(qs, lambda q: by_id[q["id"]].get("tags") if q["id"] in by_id else None)

    det_path = RESULTS_DIR / "determinism.json"
    determinism = read_json(det_path) if det_path.exists() else None
    write_json(RESULTS_DIR / "metrics.json", {"routing": routing, "scores": scores})
    print(write_results_md(routing, scores, infos, records, determinism))


if __name__ == "__main__":
    main()
