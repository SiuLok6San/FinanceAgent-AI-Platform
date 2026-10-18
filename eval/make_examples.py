"""
Turn saved v3 runs into readable examples/ pages.

    python eval/make_examples.py                 # Q01 (macro), Q09 (credit), Q13 (equity)
    python eval/make_examples.py --ids Q02,Q10,Q15

Each page shows the question, the run settings, the validation summary,
the report as rendered from the model's template (numbers the model typed
that are not in the data are wrapped in <mark>), where every number came
from, and the sources. Nothing is edited.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO, RESULTS_DIR, read_json  # noqa: E402
from fact_store import build_fact_store  # noqa: E402
from render import render  # noqa: E402
from score import table_from_json, validate_record  # noqa: E402

EXAMPLES_DIR = REPO / "examples"


PIPELINE = "v3"


def _mark_unverified(report: str, figures) -> str:
    flagged = (f for f in figures if f["status"] == "unverified" and not f["assumption"] and f.get("origin") != "quote")
    for f in sorted(flagged, key=lambda f: f["start"], reverse=True):
        report = report[:f["start"]] + "<mark>" + report[f["start"]:f["end"]] + "</mark>" + report[f["end"]:]
    return report


def _cell(text) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _repair_lines(rec) -> list:
    rounds = (rec.get("repair") or {}).get("rounds") or []
    if not rounds:
        return []
    r = rounds[0]
    draft = rec.get("draft_report_text")
    if rec.get("draft_report_template"):
        tables = [table_from_json(t) for t in rec["tables"]]
        draft = render(rec["draft_report_template"], build_fact_store(tables), rec["sources"])[0]
    if r.get("error"):
        outcome = "the revision call failed, so the first draft is shown"
    elif r["accepted"]:
        outcome = f"the revision was kept (open issues {r['open_issues_before']} → {r['open_issues_after']})"
    else:
        outcome = (f"the revision was not kept (open issues {r['open_issues_before']} → {r['open_issues_after']}), "
                   "so the first draft is shown")
    return [
        f"**Validator feedback loop:** the first draft was checked and the findings were sent back to the Writer; {outcome}.",
        "",
        "<details><summary>First draft, its validation and the feedback it received</summary>",
        "",
        f"**First draft validation:** {r['summary_before']}",
        "",
        draft,
        "",
        "**Feedback sent to the Writer:**",
        "",
        "```",
        r["feedback"],
        "```",
        "",
        "</details>",
        "",
    ]


def render_page(rec) -> str:
    v = validate_record(rec, PIPELINE)
    text = rec["report_text"]
    if rec.get("report_template"):
        text = render(rec["report_template"], build_fact_store([table_from_json(t) for t in rec["tables"]]),
                      rec["sources"])[0]
    meta = rec["run_meta"]
    lines = [
        f"# {rec['question']}",
        "",
        f"- **Category:** {rec['category']}" + (f" · **Ticker:** {rec['ticker']}" if rec["ticker"] else ""),
        f"- **Planner:** {(rec.get('plan') or {}).get('source', 'n/a')} · topics "
        f"{', '.join(rec.get('tags') or rec['router_tags'])} · FRED series "
        f"{', '.join((rec.get('plan') or {}).get('series') or []) or 'none'}",
        f"- **Run:** {rec['run_utc']} · model `{meta['model']}` via {meta['api_base']} · seed {meta['seed']} · "
        f"{rec['elapsed_sec']} s",
        f"- **Data fetched:** {len(rec['tables'])} table(s): " + ("; ".join(t["title"] for t in rec["tables"]) or "none"),
        "",
        f"> **Validation:** {v['summary']}",
        "",
        *_repair_lines(rec),
        "## Report (the model's template, rendered; <mark>highlighted</mark> numbers were typed by the model and are not in the data)",
        "",
        _mark_unverified(text, v["figures"]),
        "",
        "## Figure-by-figure check",
        "",
        "| Figure | Origin | Fact / match | Source | Status |",
        "|---|---|---|---|---|",
    ]
    for f in v["figures"]:
        src = ", ".join(f["source_ids"]) or (f"(in {f['in_source_text']})" if f["in_source_text"] else "")
        origin = {"fact": "fact table", "quote": "quoted", "assumption": "assumption", "free": "typed"}.get(f.get("origin"), "")
        what = f.get("fact_id") or f["matched"] or ""
        lines.append(f"| {_cell(f['text'])} | {origin} | {_cell(what)} | {src} | {f['status']} |")
    cit = v["citations"]
    lines += [
        "",
        f"**Citations:** {cit['markers']} marker(s); dangling: {', '.join(cit['dangling']) or 'none'}; "
        f"paragraphs with figures but no citation: {len(cit['uncited_paragraphs'])}.",
        "",
        "**Freshness:** " + ("; ".join(f"{r['table']}: last {r['last_date']} ({r['status']})" for r in v["freshness"]) or "n/a"),
        "",
        "## Sources",
        "",
    ]
    for s in rec["sources"]:
        title = _cell(s["title"])
        lines.append(f"- **[{s['id']}]** " + (f"[{title}]({s['url']})" if s.get("url") else title) + f" — {s['source']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="Q01,Q09,Q13")
    args = ap.parse_args()
    EXAMPLES_DIR.mkdir(exist_ok=True)
    for qid in args.ids.split(","):
        rec = read_json(RESULTS_DIR / PIPELINE / f"{qid}.json")
        if rec["error"]:
            print(f"{qid}: pipeline error in saved run, skipped")
            continue
        path = EXAMPLES_DIR / f"{qid}-{rec['category']}.md"
        path.write_text(render_page(rec))
        print(f"wrote {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
