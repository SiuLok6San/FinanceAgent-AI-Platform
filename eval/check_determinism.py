"""
Check whether a fixed seed makes the LLM repeatable on this endpoint.

    python eval/check_determinism.py [--repeats 3]

Sends the Planner prompt for Q01 several times with the same seed and
temperature and records how many distinct outputs came back
(eval/results/determinism.json). The result is read by eval/score.py.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO, RESULTS_DIR, load_questions, write_json  # noqa: E402

sys.path.insert(0, str(REPO))
from llm import call_llm, llm_settings  # noqa: E402
from settings import load_config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    question = load_questions()[0]["question"]
    temperature = load_config("pipeline")["temperatures"]["planner"]
    system = "You are the Planner Agent. Produce a short step-by-step research plan."
    user = f"QUESTION:\n{question}\n\nReturn 4-6 bullets."
    outputs = [call_llm(system, user, temperature=temperature) for _ in range(args.repeats)]
    digests = [hashlib.sha256(o.encode()).hexdigest()[:12] for o in outputs]

    settings = llm_settings()
    result = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "model": settings["model"],
        "api_base": settings["api_base"],
        "seed": settings["seed"],
        "temperature": temperature,
        "repeats": args.repeats,
        "distinct_outputs": len(set(digests)),
        "output_hashes": digests,
    }
    write_json(RESULTS_DIR / "determinism.json", result)
    print(result)


if __name__ == "__main__":
    main()
