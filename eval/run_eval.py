"""
Run the full pipeline on eval/questions.csv and save every output.

    python eval/run_eval.py --pipeline revised
    python eval/run_eval.py --pipeline original          # Dec 2025 code from git
    python eval/run_eval.py --pipeline revised --ids Q01,Q09

Outputs go to eval/results/<pipeline>/<id>.json plus run_info.json (run
date, model, settings, git commit). Existing results are skipped unless
--force is given, so an interrupted run can be resumed.

The "original" pipeline is the original commit's code, unchanged, except
that it calls the LLM through the current llm.py, so both pipelines use the
same model, endpoint, API key and seed. The original agents still pass their
own temperatures.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ORIGINAL_COMMIT, REPO, RESULTS_DIR, extract_original_code, git_commit,
    load_questions, table_to_json, write_json,
)


# The original code writes LLM failures into the report text instead of raising.
ERROR_MARKERS = ("[CRITICAL ERROR]", "[LLM ERROR]", "[FINAL WRITER ERROR]")


def load_pipeline(name: str):
    """Return (run_multiagent_pipeline, route_question, detect_tier2) for the chosen pipeline."""
    if name == "original":
        orig = extract_original_code()
        sys.path[:0] = [str(orig), str(REPO)]
        # same LLM transport for both pipelines: preload the current llm.py as "llm"
        spec = importlib.util.spec_from_file_location("llm", REPO / "llm.py")
        llm = importlib.util.module_from_spec(spec)
        sys.modules["llm"] = llm
        spec.loader.exec_module(llm)
    else:
        sys.path.insert(0, str(REPO))
    import agents
    import router
    import router_tier2
    assert Path(agents.__file__).resolve().parent == (extract_original_code() if name == "original" else REPO).resolve()
    return agents.run_multiagent_pipeline, router.route_question, router_tier2.detect_tier2_specialization


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline", choices=["revised", "original"], required=True)
    ap.add_argument("--ids", help="comma-separated question ids (default: all)")
    ap.add_argument("--force", action="store_true", help="re-run questions that already have results")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between questions (API rate limits)")
    args = ap.parse_args()

    run, route, tier2 = load_pipeline(args.pipeline)
    from llm import llm_settings

    out_dir = RESULTS_DIR / args.pipeline
    questions = load_questions()
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in wanted]

    # one entry per session, so resumed runs keep their history
    info_path = out_dir / "run_info.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {"pipeline": args.pipeline, "sessions": []}
    session = {
        "code": f"git {ORIGINAL_COMMIT} (original code)" if args.pipeline == "original" else f"git {git_commit()}",
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llm": llm_settings(),
        "python": sys.version.split()[0],
        "ids": [q["id"] for q in questions],
    }
    info["sessions"].append(session)
    write_json(info_path, info)

    for q in questions:
        path = out_dir / f"{q['id']}.json"
        if path.exists() and not args.force:
            print(f"{q['id']}: exists, skipping")
            continue
        t0 = time.time()
        result = run(q["question"], q["ticker"])
        elapsed = time.time() - t0
        record = {
            **{k: q[k] for k in ("id", "category", "question", "ticker", "expected_tags")},
            "pipeline": args.pipeline,
            "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_sec": round(elapsed, 1),
            "router_tags": route(q["question"]),
            "tier2": tier2(q["question"]),
            "error": bool(result.get("error")) or any(m in str(result.get("report_text", "")) for m in ERROR_MARKERS),
            "report_text": result.get("report_text_raw", result.get("report_text", "")),
            "report_text_annotated": result.get("report_text") if "report_text_raw" in result else None,
            "draft_report_text": result.get("draft_report_text"),
            "repair": result.get("repair"),
            "sources": result.get("sources", []),
            "citations_used": result.get("citations_used", []),
            "tables": [table_to_json(t) for t in result.get("key_data_tables", [])],
            "charts": result.get("chart_specs", []),
            "data_status": result.get("data_status"),
            "validation": result.get("validation"),
            "run_meta": result.get("run_meta") or {"llm": llm_settings()},
        }
        write_json(path, record)
        print(f"{q['id']}: {'ERROR' if record['error'] else 'ok'} in {elapsed:.0f}s "
              f"({len(record['tables'])} tables, {len(record['sources'])} sources)")
        time.sleep(args.pause)

    session["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json(info_path, info)


if __name__ == "__main__":
    main()
