# Copyright SUSE LLC
"""Unit tests for the request-parameter injection proxy."""

from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from typing import Any, Self
from unittest.mock import MagicMock, patch

import pytest

from scripts.benchmarking.params_proxy import (
    ProxyServer,
    forward,
    merge_params,
    parse_params,
    split_listen,
    truncate_messages,
    usage_of,
)

UPSTREAM = "http://w26:8082/v1/chat/completions"
ANSWER = (
    b'{"choices":[{"message":{"content":"**LLM Investigation summary:**\\nProduct bug"}}],'
    b'"usage":{"prompt_tokens":8123}}'
)
REQUEST = {"model": "Inkling-2-Mini", "messages": [{"role": "user", "content": "prompt 1"}],
           "temperature": 0.3, "top_p": 0.95}
URLOPEN = "scripts.benchmarking.params_proxy.urllib.request.urlopen"
FORWARD = "scripts.benchmarking.params_proxy.forward"


def response_stub(payload: bytes, status: int = 200) -> MagicMock:
    handle = MagicMock()
    handle.read.return_value = payload
    handle.status = status
    handle.__enter__.return_value = handle
    handle.__exit__.return_value = False
    return handle


class ProxyRunner:
    """The proxy served on an ephemeral port, with `forward` stubbed to record every forwarded body."""

    def __init__(self, params: list[str], response: tuple[int, bytes]) -> None:
        self.params = params
        self.response = response
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.server: ProxyServer | None = None
        self.thread: threading.Thread | None = None
        self.patcher: patch | None = None

    def __enter__(self) -> Self:
        """Start the stubbed proxy and return it."""

        def fake_forward(upstream: str, body: dict[str, Any], timeout: int = 600) -> tuple[int, bytes]:
            self.calls.append((upstream, body, timeout))
            return self.response

        self.patcher = patch(FORWARD, side_effect=fake_forward)
        self.patcher.start()
        self.server = ProxyServer(("127.0.0.1", 0), UPSTREAM, parse_params(self.params))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        """Stop the proxy and the stub."""
        assert self.server
        assert self.thread
        assert self.patcher
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.patcher.stop()
        return False

    def post(self, body: dict[str, Any]) -> tuple[int, bytes]:
        """Send one chat-completion request through the proxy."""
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            # The proxy address is the loopback port this fixture opened.
            with urllib.request.urlopen(request, timeout=30) as response:  # ruff: ignore[suspicious-url-open-usage]
                return response.status, response.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()


class TestParseParams(unittest.TestCase):
    """Injectable keys are typed and unusable input is rejected."""

    def test_accepted_values(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "effort stays a string", "pairs": ["reasoning_effort=low"],
             "expected": {"reasoning_effort": "low"}},
            {"desc": "sampling values become floats", "pairs": ["top_p=1.0", "min_p=0.0"],
             "expected": {"top_p": 1.0, "min_p": 0.0}},
            {"desc": "count keys become ints", "pairs": ["top_k=0", "num_ctx=4096"],
             "expected": {"top_k": 0, "num_ctx": 4096}},
            {"desc": "several pairs merge in order", "pairs": ["reasoning_effort=high", "top_p=1.0"],
             "expected": {"reasoning_effort": "high", "top_p": 1.0}},
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                assert parse_params(case["pairs"]) == case["expected"]

    def test_rejected_values(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "a pair without a value is rejected", "pair": "reasoning_effort", "message": "needs key=value"},
            {"desc": "temperature is not injectable", "pair": "temperature=0.3", "message": "not injectable"},
            {"desc": "a non-numeric top_p is rejected", "pair": "top_p=high", "message": "needs a number"},
            {"desc": "a non-numeric num_ctx is rejected", "pair": "num_ctx=four", "message": "needs a number"},
        ]
        for case in cases:
            with self.subTest(case["desc"]), pytest.raises(ValueError, match=case["message"]):
                parse_params([case["pair"]])


class TestMergeParams(unittest.TestCase):
    """Injected parameters are applied over the body, the body is not mutated."""

    @staticmethod
    def test_injections_override_the_body() -> None:
        merged = merge_params(REQUEST, {"top_p": 1.0, "top_k": 0, "reasoning_effort": "low"})
        assert merged["top_p"] == pytest.approx(1.0)
        assert merged["top_k"] == 0
        assert merged["reasoning_effort"] == "low"
        assert merged["temperature"] == pytest.approx(0.3)
        assert merged["messages"] == REQUEST["messages"]
        assert merged["model"] == "Inkling-2-Mini"

    @staticmethod
    def test_body_is_not_mutated() -> None:
        body = dict(REQUEST)
        merge_params(body, {"top_p": 1.0})
        assert body["top_p"] == pytest.approx(0.95)

    @staticmethod
    def test_no_params_keeps_the_body() -> None:
        assert merge_params(REQUEST, {}) == REQUEST


class TestTruncateMessages(unittest.TestCase):
    """A context-limited deployment keeps the head of each message and drops the tail."""

    def test_cases(self) -> None:
        long = "x" * 50
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": long}, {"role": "assistant", "content": "short"}],
        }
        cases: list[dict[str, Any]] = [
            {"desc": "zero keeps every message whole", "max_chars": 0, "merged": body, "lengths": [50, 5]},
            {"desc": "a limit cuts the long message and keeps the short one",
             "max_chars": 10, "merged": body, "lengths": [10, 5]},
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                result = truncate_messages(body, case["max_chars"])
                assert [len(m["content"]) for m in result["messages"]] == case["lengths"]
                assert result["model"] == "m"
                assert result["messages"][0]["role"] == "user"

    @staticmethod
    def test_body_without_messages_is_returned_unchanged() -> None:
        body = {"model": "m", "prompt": "x" * 50}
        assert truncate_messages(body, 10) == body

    @staticmethod
    def test_body_is_not_mutated() -> None:
        body = {"messages": [{"content": "x" * 50}]}
        truncate_messages(body, 10)
        assert len(body["messages"][0]["content"]) == 50


class TestUsageOf(unittest.TestCase):
    """Token counters come from the usage block; a non-JSON body yields none."""

    def test_usage(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "usage counters are read from the completion payload", "payload": ANSWER,
             "expected": {"prompt_tokens": 8123}},
            {"desc": "an upstream error page yields no counters", "payload": b"<html>502</html>", "expected": {}},
            {"desc": "a payload without usage yields no counters", "payload": b'{"choices": []}', "expected": {}},
            {"desc": "a non-object payload yields no counters", "payload": b"[1, 2]", "expected": {}},
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                assert usage_of(case["payload"]) == case["expected"]


class TestSplitListen(unittest.TestCase):
    """The listen address splits into host and port."""

    def test_accepted_addresses(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "the default loopback address", "value": "127.0.0.1:8089", "expected": ("127.0.0.1", 8089)},
            {"desc": "a named loopback address", "value": "localhost:8089", "expected": ("localhost", 8089)},
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                assert split_listen(case["value"]) == case["expected"]

    def test_rejected_addresses(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "a missing port is rejected", "value": "127.0.0.1", "message": "needs host:port"},
            {"desc": "a non-numeric port is rejected", "value": "127.0.0.1:http", "message": "needs host:port"},
        ]
        for case in cases:
            with self.subTest(case["desc"]), pytest.raises(ValueError, match=case["message"]):
                split_listen(case["value"])


class TestForward(unittest.TestCase):
    """Upstream statuses and bytes come back as they are, errors included."""

    @staticmethod
    def test_success_returns_status_and_bytes() -> None:
        with patch(URLOPEN, return_value=response_stub(ANSWER)):
            assert forward(UPSTREAM, {"model": "Inkling-2-Mini"}) == (200, ANSWER)

    @staticmethod
    def test_upstream_error_status_and_body_are_returned() -> None:
        busy = b'{"error": "server busy, please try again"}'
        error = urllib.error.HTTPError(UPSTREAM, 503, "Busy", Message(), io.BytesIO(busy))
        with patch(URLOPEN, side_effect=error):
            assert forward(UPSTREAM, {"model": "Inkling-2-Mini"}) == (503, busy)

    @staticmethod
    def test_refused_upstream_becomes_a_502_response() -> None:
        with patch(URLOPEN, side_effect=urllib.error.URLError(ConnectionRefusedError("refused"))):
            status, payload = forward(UPSTREAM, {"model": "Inkling-2-Mini"})
        assert status == 502
        assert "upstream unreachable" in payload.decode()


class TestProxyServing(unittest.TestCase):
    """The served request carries the injected parameters, the response is relayed unchanged and logged."""

    @staticmethod
    def test_injected_params_reach_the_upstream() -> None:
        with ProxyRunner(["reasoning_effort=low", "num_ctx=4096", "top_p=1.0"], (200, ANSWER)) as proxy:
            status, payload = proxy.post(REQUEST)
        assert status == 200
        assert payload == ANSWER
        upstream, sent, timeout = proxy.calls[0]
        assert upstream == UPSTREAM
        assert timeout == 600
        assert sent["reasoning_effort"] == "low"
        assert sent["num_ctx"] == 4096
        assert sent["top_p"] == pytest.approx(1.0)
        assert sent["temperature"] == pytest.approx(0.3)
        assert sent["messages"] == REQUEST["messages"]

    @staticmethod
    def test_no_params_forwards_the_body_unchanged() -> None:
        with ProxyRunner([], (200, ANSWER)) as proxy:
            proxy.post(REQUEST)
        assert proxy.calls[0][1] == REQUEST

    @staticmethod
    def test_upstream_error_status_is_relayed_to_the_client() -> None:
        busy = b'{"error": "server busy, please try again"}'
        with ProxyRunner(["reasoning_effort=high"], (503, busy)) as proxy:
            status, payload = proxy.post(REQUEST)
        assert status == 503
        assert payload == busy

    def test_request_log_names_model_tokens_and_wall_time(self) -> None:
        with (
            self.assertLogs("params_proxy", level="INFO") as captured,
            ProxyRunner(["reasoning_effort=low"], (200, ANSWER)) as proxy,
        ):
            proxy.post(REQUEST)
        lines = [record.getMessage() for record in captured.records]
        request_line = next(line for line in lines if "prompt_tokens=" in line)
        for needle in ("model=Inkling-2-Mini", "status=200", "prompt_tokens=8123", "wall_sec="):
            assert needle in request_line
        assert any("POST /v1/chat/completions" in line for line in lines)


if __name__ == "__main__":
    unittest.main()
