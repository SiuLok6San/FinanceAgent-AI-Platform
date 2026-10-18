"""llm.py: total time limit, retries and error handling (HTTP mocked)."""

import json

import pytest

import llm


class FakeResponse:
    def __init__(self, status=200, body=None, chunks=None, clock=None, step=0.0):
        self.status_code = status
        self._body = json.dumps(body or {"message": {"content": "hello"}}).encode()
        self._chunks = chunks
        self._clock, self._step = clock, step
        self.text = self._body.decode()

    def iter_content(self, chunk_size=1):
        for c in (self._chunks or [self._body]):
            if self._clock is not None:
                self._clock[0] += self._step
            yield c

    @property
    def content(self):
        return getattr(self, "_content", self._body)

    def close(self):
        pass


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_returns_message_content(monkeypatch, no_sleep):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResponse())
    assert llm.call_llm("sys", "user") == "hello"


def test_trickling_response_is_abandoned_after_total_timeout(monkeypatch, no_sleep):
    clock = [0.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    calls = []

    def post(*a, **k):
        calls.append(1)
        # one byte every 60 s: each read is within the read timeout, but the total is not
        return FakeResponse(chunks=[b" "] * 100, clock=clock, step=60.0)

    monkeypatch.setattr(llm.requests, "post", post)
    with pytest.raises(llm.LLMError, match="no complete response within"):
        llm.call_llm("sys", "user", retries=1)
    assert len(calls) == 2  # retried once, then gave up


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
