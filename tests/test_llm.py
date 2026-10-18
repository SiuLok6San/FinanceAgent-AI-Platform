"""llm.py: total time limit, retries and error handling (HTTP mocked)."""

import json
import threading

import pytest

import llm


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.content = json.dumps(body or {"message": {"content": "hello"}}).encode()
        self.text = self.content.decode()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_returns_message_content(monkeypatch, no_sleep):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResponse())
    assert llm.call_llm("sys", "user") == "hello"


def test_stuck_request_is_abandoned_after_total_timeout(monkeypatch, no_sleep):
    # a request that never returns (like a server trickling bytes forever)
    release = threading.Event()
    calls = []

    def post(*a, **k):
        calls.append(1)
        release.wait(5)
        return FakeResponse()

    monkeypatch.setattr(llm, "LLM_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(llm.requests, "post", post)
    try:
        with pytest.raises(llm.LLMError, match="no complete response within"):
            llm.call_llm("sys", "user", retries=1)
        assert len(calls) == 2  # retried once, then gave up
    finally:
        release.set()


def test_client_errors_are_not_retried(monkeypatch, no_sleep):
    calls = []

    def post(*a, **k):
        calls.append(1)
        return FakeResponse(status=401, body={"error": "unauthorized"})

    monkeypatch.setattr(llm.requests, "post", post)
    with pytest.raises(llm.LLMError, match="HTTP 401"):
        llm.call_llm("sys", "user", retries=3)
    assert len(calls) == 1


def test_rate_limit_is_retried(monkeypatch, no_sleep):
    responses = [FakeResponse(status=429, body={"error": "slow down"}), FakeResponse()]
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: responses.pop(0))
    assert llm.call_llm("sys", "user", retries=2) == "hello"
