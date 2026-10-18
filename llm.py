"""
llm.py — Native Ollama Chat Caller

Works with a local Ollama server (http://localhost:11434) and with the
hosted Ollama API (https://ollama.com), which uses the same /api/chat
format plus a Bearer API key (OLLAMA_API_KEY).
"""

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
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(LLM_BACKOFF_SEC * 2 ** (attempt - 1))
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=LLM_TIMEOUT_SEC)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            continue

        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if _is_retryable(r.status_code):
                continue
            break  # 401 / 404 etc. will not fix themselves

        msg = (r.json().get("message") or {}).get("content", "")
        if not msg.strip():
            last_err = "empty message from model"
            continue
        return msg.strip()

    raise LLMError(
        f"LLM call failed (model={model}, base={LLM_API_BASE}). Detail: {last_err}"
    )
