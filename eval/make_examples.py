"""
Turn saved revised-pipeline runs into readable examples/ pages.

    python eval/make_examples.py                 # Q01 (macro), Q09 (credit), Q13 (equity)
    python eval/make_examples.py --ids Q02,Q10,Q15

Each page shows the question, the run settings, the validation summary,
the report exactly as the model wrote it (unverified figures wrapped in
<mark>), the figure-by-figure check and the sources. Nothing is edited.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO, RESULTS_DIR, read_json  # noqa: E402
from score import validate_record  # noqa: E402

EXAMPLES_DIR = REPO / "examples"


def _mark_unverified(report: str, figures) -> str:
    for f in sorted((f for f in figures if f["status"] == "unverified"), key=lambda f: f["start"], reverse=True):
        report = report[:f["start"]] + "<mark>" + report[f["start"]:f["end"]] + "</mark>" + report[f["end"]:]
    return report


def _cell(text) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _repair_lines(rec) -> list:
    rounds = (rec.get("repair") or {}).get("rounds") or []
    if not rounds:
        return []
    r = rounds[0]
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
        rec["draft_report_text"],
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


def render(rec) -> str:
    v = validate_record(rec)
    meta = rec["run_meta"]
    lines = [
        f"# {rec['question']}",
        "",
        f"- **Category:** {rec['category']}" + (f" · **Ticker:** {rec['ticker']}" if rec["ticker"] else ""),
        f"- **Routed topics (rule-based):** {', '.join(rec['router_tags'])} · **Tier 2:** "
        + ", ".join(f"{k}: {val}" for k, val in rec["tier2"].items()),
        f"- **Run:** {rec['run_utc']} · model `{meta['model']}` via {meta['api_base']} · seed {meta['seed']} · "
        f"{rec['elapsed_sec']} s",
        f"- **Data fetched:** {len(rec['tables'])} table(s): " + ("; ".join(t["title"] for t in rec["tables"]) or "none"),
        "",
        f"> **Validation:** {v['summary']}",
        "",
        *_repair_lines(rec),
        "## Report (unedited model output; <mark>highlighted</mark> figures could not be matched to fetched data)",
        "",
        _mark_unverified(rec["report_text"], v["figures"]),
        "",
        "## Figure-by-figure check",
        "",
        "| Figure | Status | Matched data | Source | Same topic |",
        "|---|---|---|---|---|",
    ]
    for f in v["figures"]:
        src = f["source_id"] or (f"(in snippet {f['in_source_text']})" if f["in_source_text"] else "")
        topic = "" if f["topic_match"] is None else ("yes" if f["topic_match"] else "no")
        lines.append(f"| {_cell(f['text'])} | {f['status']} | {_cell(f['matched'])} | {src} | {topic} |")
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
        rec = read_json(RESULTS_DIR / "revised" / f"{qid}.json")
        if rec["error"]:
            print(f"{qid}: pipeline error in saved run, skipped")
            continue
        path = EXAMPLES_DIR / f"{qid}-{rec['category']}.md"
        path.write_text(render(rec))
        print(f"wrote {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
