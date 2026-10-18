"""
Run a pipeline on the evaluation questions and save every output.

    python eval/run_eval.py --pipeline v3 --set all
    python eval/run_eval.py --pipeline v2 --set heldout       # tag v2.0-validation, from git
    python eval/run_eval.py --pipeline original --set heldout # tag v1.0-course, from git
    python eval/run_eval.py --pipeline v3 --ids Q01,H03

Outputs go to eval/results/<pipeline>/<id>.json plus run_info.json (run
date, model, settings, git commit per session). Existing results are
skipped unless --force is given, so an interrupted run can be resumed.

Old versions run their own code, checked out with `git archive`. The
original also gets the current llm.py, because its own client cannot call
the hosted Ollama API; its agents still pass their own temperatures.
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
    PIPELINES, REPO, RESULTS_DIR, extract_code, git_commit, load_questions, table_to_json, write_json,
)

# The original code writes LLM failures into the report text instead of raising.
ERROR_MARKERS = ("[CRITICAL ERROR]", "[LLM ERROR]", "[FINAL WRITER ERROR]")


def load_pipeline(name: str):
    """Return (run_multiagent_pipeline, route_question, detect_tier2, code label) for a pipeline."""
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")  # old versions look for .env in their own checkout
    commit, inject_llm = PIPELINES[name]
    code_dir = extract_code(commit) if commit else REPO
    sys.path.insert(0, str(code_dir))
    if inject_llm:
        sys.path.insert(1, str(REPO))  # current llm.py needs current settings.py
        spec = importlib.util.spec_from_file_location("llm", REPO / "llm.py")
        llm = importlib.util.module_from_spec(spec)
        sys.modules["llm"] = llm
        spec.loader.exec_module(llm)
    import agents
    import router
    import router_tier2
    assert Path(agents.__file__).resolve().parent == code_dir.resolve()
    label = f"git {commit} ({name})" if commit else f"git {git_commit()}"
    return agents.run_multiagent_pipeline, router.route_question, router_tier2.detect_tier2_specialization, label


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline", choices=sorted(PIPELINES), required=True)
    ap.add_argument("--set", choices=["dev", "heldout", "all"], default="dev")
    ap.add_argument("--ids", help="comma-separated question ids (default: the whole set)")
    ap.add_argument("--force", action="store_true", help="re-run questions that already have results")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between questions (API rate limits)")
    args = ap.parse_args()

    run, route, tier2, code = load_pipeline(args.pipeline)
    from llm import llm_settings

    out_dir = RESULTS_DIR / args.pipeline
    questions = load_questions("all" if args.ids else args.set)
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in wanted]

    # one entry per session, so resumed runs keep their history
    info_path = out_dir / "run_info.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {"pipeline": args.pipeline, "sessions": []}
    session = {
        "code": code,
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
            **{k: q[k] for k in ("id", "set", "category", "question", "ticker", "expected_tags")},
            "pipeline": args.pipeline,
            "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_sec": round(elapsed, 1),
            "router_tags": route(q["question"]),
            "tags": result.get("tags"),
            "plan": result.get("plan"),
            "plan_raw": result.get("plan_raw"),
            "tier2": tier2(q["question"]),
            "error": bool(result.get("error")) or any(m in str(result.get("report_text", "")) for m in ERROR_MARKERS),
            "report_text": result.get("report_text_raw", result.get("report_text", "")),
            "report_template": result.get("report_template"),
            "draft_report_text": result.get("draft_report_text"),
            "draft_report_template": result.get("draft_report_template"),
            "repair": result.get("repair"),
            "sources": result.get("sources", []),
            "citations_used": result.get("citations_used", []),
            "tables": [table_to_json(t) for t in result.get("key_data_tables", [])],
            "charts": result.get("chart_specs", []),
            "data_status": result.get("data_status"),
            "agent_outputs": result.get("agent_outputs"),
            "validation": result.get("validation"),
            "run_meta": result.get("run_meta") or {"llm": llm_settings()},
        }
        write_json(path, record)
        print(f"{q['id']}: {'ERROR' if record['error'] else 'ok'} in {elapsed:.0f}s "
              f"({len(record['tables'])} tables, {len(record['sources'])} sources)", flush=True)
        time.sleep(args.pause)

    # re-read before writing: another run of the same pipeline may have added its session meanwhile
    info = json.loads(info_path.read_text())
    for s in info["sessions"]:
        if s["started_utc"] == session["started_utc"] and s["ids"] == session["ids"]:
            s["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json(info_path, info)


if __name__ == "__main__":
    main()
