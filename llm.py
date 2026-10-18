"""
llm.py — Native Ollama Chat Caller

Works with a local Ollama server (http://localhost:11434) and with the
hosted Ollama API (https://ollama.com), which uses the same /api/chat
format plus a Bearer API key (OLLAMA_API_KEY).
"""

import json
import threading
import time
import requests
from typing import Any, Dict, Optional, Union

from settings import env, load_config

_CFG = load_config("pipeline")["llm"]

LLM_API_BASE = env("LLM_API_BASE", _CFG["api_base"]).rstrip("/")
LLM_MODEL_NAME = env("LLM_MODEL_NAME", _CFG["model"])
LLM_TIMEOUT_SEC = float(env("LLM_TIMEOUT_SEC", str(_CFG["timeout_sec"])))
# Fixed seed so repeated runs with the same inputs give the same output
# (as far as the server honours it).
LLM_SEED = int(env("LLM_SEED", str(_CFG["seed"])))
LLM_RETRIES = int(_CFG["retries"])
LLM_BACKOFF_SEC = float(_CFG["backoff_sec"])
LLM_RATE_LIMIT_WAIT_SEC = float(_CFG.get("rate_limit_wait_sec", 20.0))
LLM_NUM_CTX = int(_CFG["num_ctx"])
OLLAMA_API_KEY = env("OLLAMA_API_KEY")


def llm_settings() -> Dict[str, Any]:
    """Settings that are logged with every report for reproducibility (no secrets)."""
    return {
        "model": LLM_MODEL_NAME,
        "api_base": LLM_API_BASE,
        "provider": "ollama-api" if OLLAMA_API_KEY else "ollama-local",
        "seed": LLM_SEED,
        "num_ctx": LLM_NUM_CTX,
        "timeout_sec": LLM_TIMEOUT_SEC,
    }


class LLMError(RuntimeError):
    pass


def _is_retryable(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


class _Deadline(Exception):
    pass


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> requests.Response:
    """POST with a hard total time limit.

    requests' timeout only limits the wait for each socket read, not the whole
    call. The request runs in a daemon thread and the caller stops waiting
    once LLM_TIMEOUT_SEC has passed; a stuck thread is abandoned, not joined.
    (Background: eval runs looked stuck for over an hour. The real cause was
    the laptop sleeping mid-run, which froze the process and left stale
    connections behind; the total limit is kept as a safeguard.)
    """
    box: Dict[str, Any] = {}
    done = threading.Event()

    def worker() -> None:
        try:
            box["response"] = requests.post(url, json=payload, headers=headers, timeout=(15, LLM_TIMEOUT_SEC))
        except BaseException as e:  # handed to the caller below
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True, name="llm-request").start()
    if not done.wait(LLM_TIMEOUT_SEC):
        raise _Deadline(f"no complete response within {LLM_TIMEOUT_SEC:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["response"]


def call_llm(
    system_prompt: Union[str, None],
    user_prompt: Optional[str] = None,
    *,
    temperature: float = 0.15,
    model: Optional[str] = None,
    retries: Optional[int] = None,
) -> str:
    """
    Call Ollama's native /api/chat endpoint.
    Supports:
    - call_llm(prompt)
    - call_llm(system_prompt, user_prompt)
    """

    # Backward compatibility: call_llm(prompt)
    if user_prompt is None:
        user_prompt = system_prompt
        system_prompt = "You are a professional institutional finance analyst."

    model = model or LLM_MODEL_NAME
    retries = LLM_RETRIES if retries is None else retries

    url = f"{LLM_API_BASE}/api/chat"
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "temperature": temperature,
            "seed": LLM_SEED,
            "num_ctx": LLM_NUM_CTX,
        },
        "stream": False
    }

    last_err: Optional[str] = None
    retry_after: Optional[float] = None
    for attempt in range(retries + 1):
        if attempt:
            # a rate limit (429) needs a real pause: the server's Retry-After if given, else
            # rate_limit_wait_sec doubling; other errors retry quickly
            if retry_after is not None:
                time.sleep(max(retry_after, LLM_RATE_LIMIT_WAIT_SEC * 2 ** (attempt - 1)))
            else:
                time.sleep(LLM_BACKOFF_SEC * 2 ** (attempt - 1))
        retry_after = None
        try:
            r = _post_json(url, payload, headers)
        except (requests.RequestException, _Deadline) as e:
            last_err = f"{type(e).__name__}: {e}"
            continue

        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code == 429:
                try:
                    retry_after = float((getattr(r, "headers", None) or {}).get("retry-after", 0))
                except ValueError:
                    retry_after = 0.0
            if _is_retryable(r.status_code):
                continue
            break  # 401 / 404 etc. will not fix themselves

        try:
            msg = (json.loads(r.content).get("message") or {}).get("content", "")
        except ValueError:
            last_err = "response was not valid JSON"
            continue
        if not msg.strip():
            last_err = "empty message from model"
            continue
        return msg.strip()

    raise LLMError(
        f"LLM call failed (model={model}, base={LLM_API_BASE}). Detail: {last_err}"
    )
