"""
Score saved evaluation results and write eval/RESULTS.md.

    python eval/score.py

- Routing accuracy: the original and the revised rule-based routers against
  the expected tags in eval/questions.csv (no LLM involved).
- Validation metrics: the current validators.py is applied to the saved
  outputs of BOTH pipelines ("validation turned on, nothing else changed"
  for the original), using each record's run date for the freshness check.

Every number in RESULTS.md is computed here from the saved files.
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
    EVAL_DIR, ORIGINAL_COMMIT, REPO, RESULTS_DIR, extract_original_code,
    load_questions, read_json, table_from_json, write_json,
)

sys.path.insert(0, str(REPO))
from router import route_question as route_revised  # noqa: E402
from settings import load_config  # noqa: E402
from validators import validate_report  # noqa: E402

PIPELINES = ["original", "revised"]
# columns in RESULTS.md; "revised_draft" is the revised pipeline's first draft, before the feedback loop
LABELS = {"original": "Original pipeline", "revised_draft": "Revised, first draft",
          "revised": "Revised, after feedback loop"}
TAGS = load_config("routing")["tag_order"]

# Temperatures hard-wired in the original code (agents.py / answer_builder.py at 0468eea).
ORIGINAL_TEMPERATURES = {"planner": 0.1, "analyst": 0.15, "reasoner": 0.15, "writer": 0.2}

# Series the original code fetched that the revised config no longer lists.
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


def routing_metrics(questions, route) -> Dict[str, Any]:
    rows, tp, fp, fn = [], 0, 0, 0
    for q in questions:
        got, exp = set(route(q["question"])), set(q["expected_tags"])
        tp, fp, fn = tp + len(got & exp), fp + len(got - exp), fn + len(exp - got)
        rows.append({"id": q["id"], "expected": sorted(exp), "got": sorted(got),
                     "exact": got == exp, "primary": q["category"] in got})
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "n": len(rows),
        "exact": sum(r["exact"] for r in rows),
        "primary": sum(r["primary"] for r in rows),
        "precision": precision,
        "recall": recall,
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


def validate_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    tables = _original_tables(rec) if rec["pipeline"] == "original" else [table_from_json(t) for t in rec["tables"]]
    v = validate_report(rec["report_text"], rec["question"], tables, rec["sources"], rec["router_tags"],
                        data_status=rec.get("data_status"), today=_run_date(rec))
    v["n_tables"] = len(tables)
    return v


def pipeline_metrics(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [r for r in records if not r["error"]]
    vals = [validate_record(r) for r in ok]
    figs = [f for v in vals for f in v["figures"]]
    num = [f for f in figs if f["unit"] != "date"]
    count = lambda status, fs=figs: sum(f["status"] == status for f in fs)  # noqa: E731
    nd_topics = [t for v in vals for t in v["no_data"]["topics_without_data"]]
    return {
        "questions": len(records),
        "errors": len(records) - len(ok),
        "error_ids": [r["id"] for r in records if r["error"]],
        "reports_with_data": sum(v["n_tables"] > 0 for v in vals),
        "tables": sum(v["n_tables"] for v in vals),
        "figures": len(figs),
        "verified": count("verified"),
        "derived": count("derived"),
        "unverified": count("unverified"),
        "numbers": len(num),
        "numbers_verified": count("verified", num),
        "numbers_derived": count("derived", num),
        "numbers_unverified": count("unverified", num),
        "dates": len(figs) - len(num),
        "dates_verified": count("verified", [f for f in figs if f["unit"] == "date"]),
        "grounded_same_topic": sum(f["status"] != "unverified" and f["topic_match"] is True for f in num),
        # written without decimals ("2%", "4"), so the match tolerance is ±0.5 or wider
        "grounded_low_precision": sum(f["status"] != "unverified" and f["tolerance"] >= 0.5 for f in num),
        "unverified_in_snippets": sum(f["status"] == "unverified" and bool(f["in_source_text"]) for f in num),
        "citation_markers": sum(v["citations"]["markers"] for v in vals),
        "dangling_ids": sum(len(v["citations"]["dangling"]) for v in vals),
        "reports_with_dangling": sum(bool(v["citations"]["dangling"]) for v in vals),
        "uncited_paragraphs": sum(len(v["citations"]["uncited_paragraphs"]) for v in vals),
        "source_mismatches": sum(len(v["citations"]["source_mismatches"]) for v in vals),
        "stale_series": sum(r["status"] == "stale" for v in vals for r in v["freshness"]),
        "reports_with_no_data_topics": sum(bool(v["no_data"]["topics_without_data"]) for v in vals),
        "no_data_topics": len(nd_topics),
        "no_data_undisclosed": sum(not t["disclosed"] for t in nd_topics),
        "no_data_unverified_figures": sum(len(t["unverified_figures"]) for t in nd_topics),
        "reports_with_no_data_issue": sum(v["no_data"]["issue"] for v in vals),
        "open_issues": sum(v["open_issues"] for v in vals),
        "assumptions": sum(f["status"] == "unverified" and f.get("assumption", False) for f in num),
        "mean_elapsed_sec": round(sum(r["elapsed_sec"] for r in records) / len(records), 1) if records else None,
        "per_question": [{
            "id": r["id"], "category": r["category"], "tables": v["n_tables"], **v["counts"],
            "dangling": len(v["citations"]["dangling"]), "uncited": len(v["citations"]["uncited_paragraphs"]),
            "no_data_issue": v["no_data"]["issue"], "summary": v["summary"],
        } for r, v in zip(ok, vals)],
    }


# ---------------------------------------------------------------------------
# RESULTS.md
# ---------------------------------------------------------------------------

def _pct(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.0%})" if b else "0/0"


def _run_info(pipeline: str) -> Optional[Dict[str, Any]]:
    path = RESULTS_DIR / pipeline / "run_info.json"
    return read_json(path) if path.exists() else None


def _setup_rows(infos: Dict[str, Any], records: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    def cell(p, fn):
        info = infos.get(p)
        if not info or not info["sessions"]:
            return "not run"
        return fn(info, records.get(p, []))

    def dates(info, recs):
        ds = sorted({r["run_utc"][:10] for r in recs})
        return (ds[0] if len(ds) == 1 else f"{ds[0]} to {ds[-1]}") if ds else "n/a"

    def temps(p):
        if p == "original":
            return ", ".join(f"{k} {v}" for k, v in ORIGINAL_TEMPERATURES.items()) + " (hard-coded in original code)"
        seen = {tuple(r["run_meta"]["temperatures"].items()) for r in records.get(p, []) if r["run_meta"].get("temperatures")}
        return "; ".join(", ".join(f"{k} {v}" for k, v in t) for t in sorted(seen)) or "n/a"

    def loop(p):
        if p == "original":
            return "none"
        seen = {(m["enabled"], m["max_rounds"]) for r in records.get(p, []) if (m := r["run_meta"].get("repair"))}
        if not seen:
            return "none"
        return "; ".join(f"validator feedback → {n} Writer revision(s), kept only if it has fewer open issues"
                         if on else "off" for on, n in sorted(seen))

    def producing_sessions(info, recs):
        """For each saved record, the latest session that started before it ran and included its id."""
        used = []
        for rec in recs:
            cands = [s for s in info["sessions"] if rec["id"] in s["ids"] and s["started_utc"] <= rec["run_utc"]]
            if cands:
                used.append(max(cands, key=lambda s: s["started_utc"]))
        return used

    rows = [
        ("Code", lambda i, r: "; ".join(sorted({s["code"] for s in producing_sessions(i, r)}))),
        ("Run date (UTC)", dates),
        ("Model", lambda i, r: ", ".join(sorted({s["llm"]["model"] for s in producing_sessions(i, r)}))),
        ("Endpoint", lambda i, r: ", ".join(sorted({f"{s['llm']['api_base']} ({s['llm'].get('provider', '')})" for s in producing_sessions(i, r)}))),
        ("Seed / context", lambda i, r: ", ".join(sorted({f"seed {s['llm']['seed']}, num_ctx {s['llm'].get('num_ctx', 'default')}" for s in producing_sessions(i, r)}))),
        ("Questions run", lambda i, r: str(len(r))),
    ]
    out = ["| | Original pipeline (before) | Revised pipeline (after) |", "|---|---|---|"]
    for label, fn in rows:
        out.append(f"| {label} | {cell('original', fn)} | {cell('revised', fn)} |")
    out.append(f"| Temperatures | {temps('original')} | {temps('revised')} |")
    out.append(f"| Feedback loop | {loop('original')} | {loop('revised')} |")
    return out


def write_results_md(routing: Dict[str, Any], metrics: Dict[str, Any], infos: Dict[str, Any],
                     records: Dict[str, List[Dict[str, Any]]], determinism: Optional[Dict[str, Any]],
                     repair: Optional[Dict[str, Any]] = None) -> str:
    o, r = routing["original"], routing["revised"]
    lines = [
        "# Evaluation results",
        "",
        f"Generated by `eval/score.py` on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from the saved "
        "outputs in `eval/results/`. All numbers are reported exactly as computed, including the bad ones.",
        "",
        "## Setup",
        "",
        *_setup_rows(infos, records),
        "",
        f"The *original* pipeline is the code at commit `{ORIGINAL_COMMIT}` (Dec 2025 course version), run unchanged except "
        "that it calls the LLM through the current `llm.py`, so both pipelines use the same model, endpoint and seed. "
        "The *revised* pipeline is this branch. The same `validators.py` is applied to the saved outputs of both "
        "(for the original: validation turned on, nothing else changed).",
        "",
        "## Routing accuracy (rule-based, no LLM)",
        "",
        "| | Original router | Revised router |",
        "|---|---|---|",
        f"| Exact match of the full tag set | {_pct(o['exact'], o['n'])} | {_pct(r['exact'], r['n'])} |",
        f"| Question's own category among the tags | {_pct(o['primary'], o['n'])} | {_pct(r['primary'], r['n'])} |",
        f"| Tag precision | {o['precision']:.2f} | {r['precision']:.2f} |",
        f"| Tag recall | {o['recall']:.2f} | {r['recall']:.2f} |",
        "",
        "Caveat: the revised router's fixes (whole-word matching, a few exclude phrases) were written after the 20 "
        "questions were drafted, so these questions were also the development set and the revised numbers are optimistic.",
        "",
        "| Question | Expected | Original router | Revised router |",
        "|---|---|---|---|",
    ]
    for a, b in zip(o["rows"], r["rows"]):
        mark = lambda row: ("" if row["exact"] else " ✗")  # noqa: E731
        lines.append(f"| {a['id']} | {', '.join(a['expected'])} | {', '.join(a['got'])}{mark(a)} | {', '.join(b['got'])}{mark(b)} |")

    lines += ["", "## Validation metrics", ""]
    if not metrics:
        lines.append("No pipeline outputs found yet; run `eval/run_eval.py` first.")
    else:
        cols = [p for p in ("original", "revised_draft", "revised") if p in metrics]
        labels = dict(LABELS, revised="Revised pipeline") if "revised_draft" not in metrics else LABELS
        header = "| Metric | " + " | ".join(labels[p] for p in cols) + " |"
        lines += [header, "|---" * (len(cols) + 1) + "|"]

        def row(label, fn):
            lines.append(f"| {label} | " + " | ".join(fn(metrics[p]) for p in cols) + " |")

        row("Questions run / pipeline errors", lambda m: f"{m['questions']} / {m['errors']}")
        row("Reports with any fetched data table", lambda m: _pct(m["reports_with_data"], m["questions"] - m["errors"]))
        row("Figures checked (numbers + dates)", lambda m: str(m["figures"]))
        row("Verified (exact match to fetched data)", lambda m: _pct(m["verified"], m["figures"]))
        row("Derived (arithmetic on fetched data)", lambda m: _pct(m["derived"], m["figures"]))
        row("Unverified", lambda m: _pct(m["unverified"], m["figures"]))
        row("Numbers only: verified + derived", lambda m: _pct(m["numbers_verified"] + m["numbers_derived"], m["numbers"]))
        row("Grounded numbers matched to a series on the sentence's topic",
            lambda m: _pct(m["grounded_same_topic"], m["numbers_verified"] + m["numbers_derived"]))
        row("Grounded numbers written without decimals (match tolerance ±0.5 or wider)",
            lambda m: _pct(m["grounded_low_precision"], m["numbers_verified"] + m["numbers_derived"]))
        row("Unverified numbers that appear in a web/news snippet", lambda m: str(m["unverified_in_snippets"]))
        row("Unverified numbers labelled as assumptions", lambda m: str(m.get("assumptions", 0)))
        row("Citation markers", lambda m: str(m["citation_markers"]))
        row("Dangling citation ids (reports affected)", lambda m: f"{m['dangling_ids']} ({m['reports_with_dangling']})")
        row("Paragraphs with grounded figures but no citation", lambda m: str(m["uncited_paragraphs"]))
        row("Paragraphs citing a different source than the data", lambda m: str(m["source_mismatches"]))
        row("Stale data series", lambda m: str(m["stale_series"]))
        row("Routed topics with no data", lambda m: str(m["no_data_topics"]))
        row("… not disclosed in the report", lambda m: str(m["no_data_undisclosed"]))
        row("… unverified figures on those topics", lambda m: str(m["no_data_unverified_figures"]))
        row("Reports with a no-data honesty issue", lambda m: _pct(m["reports_with_no_data_issue"], m["questions"] - m["errors"]))
        row("Open issues a writer could fix (validator count)", lambda m: str(m.get("open_issues", "")))
        row("Mean time per question (s)", lambda m: str(m["mean_elapsed_sec"]))

        if repair:
            lines += ["", "### Validator feedback loop (revised pipeline)", "",
                      f"Questions where the first draft had open issues and a revision was requested: {repair['attempted']}/{repair['questions']}. "
                      f"Revisions kept (strictly fewer open issues): {repair['accepted']}. "
                      f"Rejected: {repair['rejected']}. Revision calls that failed: {repair['failed']}. "
                      f"Open issues over all questions: {repair['issues_before']} in the first drafts, "
                      f"{repair['issues_after']} in the final reports."]

        for p in cols:
            lines += ["", f"### Per question: {labels[p]}", "",
                      "| Question | Tables | Figures | Verified | Derived | Unverified | Dangling | Uncited | No-data issue |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for q in metrics[p]["per_question"]:
                lines.append(f"| {q['id']} ({q['category']}) | {q['tables']} | {q['figures']} | {q['verified']} | "
                             f"{q['derived']} | {q['unverified']} | {q['dangling']} | {q['uncited']} | "
                             f"{'yes' if q['no_data_issue'] else 'no'} |")
            if metrics[p]["error_ids"]:
                lines.append(f"\nPipeline errors (excluded above): {', '.join(metrics[p]['error_ids'])}")

    if determinism:
        lines += ["", "## Determinism check", "",
                  f"Same prompt sent {determinism['repeats']} times to {determinism['model']} with seed "
                  f"{determinism['seed']} and temperature {determinism['temperature']} on {determinism['date']}: "
                  f"{determinism['distinct_outputs']} distinct output(s)."]

    notes = EVAL_DIR / "NOTES.md"
    if notes.exists():
        lines += ["", "## Notes on this run", "", notes.read_text().strip()]

    lines += [
        "",
        "## How to read these numbers",
        "",
        "- *Verified* means the number matches a value in the fetched tables within the rounding its digits imply "
        "(\"4%\" matches anything from 3.5 to 4.5), so coarse numbers can match by coincidence, e.g. \"the Fed's 2% "
        "target\" can match an inflation change that rounds to 2%. The \"same topic\" and \"without decimals\" rows "
        "show how much of the grounding is solid.",
        "- *Unverified* does not mean wrong: it means the number could not be traced to fetched data (it may come from "
        "a web snippet, general knowledge, or be invented).",
        "- 20 questions is a small sample, and live data and LLM outputs change from day to day.",
    ]
    text = "\n".join(lines) + "\n"
    (EVAL_DIR / "RESULTS.md").write_text(text)
    return text


def main() -> None:
    questions = load_questions()
    routing = {"original": routing_metrics(questions, _original_router()),
               "revised": routing_metrics(questions, route_revised)}

    records, metrics, infos = {}, {}, {}
    for p in PIPELINES:
        files = sorted((RESULTS_DIR / p).glob("Q*.json"))
        if files:
            records[p] = [read_json(f) for f in files]
            metrics[p] = pipeline_metrics(records[p])
            infos[p] = _run_info(p)

    # the revised pipeline's first drafts, scored the same way, isolate the feedback loop's effect
    repair = None
    revised = records.get("revised", [])
    if revised and all(r.get("draft_report_text") for r in revised if not r["error"]):
        drafts = [dict(r, report_text=r["draft_report_text"]) for r in revised]
        metrics["revised_draft"] = pipeline_metrics(drafts)
        rounds = [r["repair"]["rounds"][0] for r in revised if not r["error"] and r["repair"]["rounds"]]
        repair = {
            "questions": sum(not r["error"] for r in revised),
            "attempted": len(rounds),
            "accepted": sum(x["accepted"] for x in rounds),
            "rejected": sum(not x["accepted"] and not x.get("error") for x in rounds),
            "failed": sum(bool(x.get("error")) for x in rounds),
            "issues_before": metrics["revised_draft"]["open_issues"],
            "issues_after": metrics["revised"]["open_issues"],
        }
    det_path = RESULTS_DIR / "determinism.json"
    determinism = read_json(det_path) if det_path.exists() else None

    write_json(RESULTS_DIR / "metrics.json", {"routing": routing, "validation": metrics, "repair": repair})
    print(write_results_md(routing, metrics, infos, records, determinism, repair))


if __name__ == "__main__":
    main()
