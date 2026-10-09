# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.http."""

from __future__ import annotations

import logging
import re

import httpx
import pytest

from openqa_llm_investigate.http import AnalyzerError, OpenQA


def _api(handler: httpx.MockTransport | None = None, status: int = 200, content: str = "{}") -> OpenQA:
    transport = handler or httpx.MockTransport(lambda _request: httpx.Response(status, text=content))
    return OpenQA(httpx.Client(transport=transport), "https://openqa.example/")


def test_text_joins_server_and_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="hello")

    assert _api(httpx.MockTransport(handler)).text("/tests/1/file/x.txt") == "hello"
    assert seen == ["https://openqa.example/tests/1/file/x.txt"]


def test_json_parses() -> None:
    assert _api(content='{"a": 1}').json("/api/v1/jobs/1") == {"a": 1}


@pytest.mark.parametrize("method", ["text", "json"])
def test_http_error_raises_analyzer_error(method: str) -> None:
    with pytest.raises(AnalyzerError, match=r"Error fetching https://openqa\.example/x"):
        getattr(_api(status=404), method)("/x")


def test_invalid_json_raises_analyzer_error() -> None:
    with pytest.raises(AnalyzerError, match="Error parsing JSON"):
        _api(content="<html>").json("/x")


def test_text_or_empty() -> None:
    assert _api(content="ok").text_or_empty("/x") == "ok"
    assert not _api(status=500).text_or_empty("/x")


def test_partial_output_is_kept() -> None:
    assert AnalyzerError("boom", partial_output="so far").partial_output == "so far"


def test_job_file_is_fetched_once() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, text="log")

    api = _api(httpx.MockTransport(handler))
    assert api.job_file("1", "a.txt") == "log"
    assert api.job_file("1", "a.txt") == "log"
    assert seen == ["/tests/1/file/a.txt"]


def test_job_file_unavailable_is_empty() -> None:
    assert not _api(status=404).job_file("1", "a.txt")


def test_text_is_fetched_once() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, text='{"a": 1}')

    api = _api(httpx.MockTransport(handler))
    assert api.json("/x") == api.json("/x") == {"a": 1}
    assert seen == ["/x"]


def test_job_file_keeps_head_and_tail_of_large_files() -> None:
    body = "".join(f"line {i}\n" for i in range(1000))
    api = _api(content=body)
    out = api.job_file("1", "big.txt", max_bytes=200)
    lines = out.split("\n")
    assert lines[0] == "line 0"
    assert lines[-2] == "line 999"
    assert any(re.fullmatch(r"\[\.\.\. \d+ bytes omitted from the middle of this file \.\.\.\]", ln) for ln in lines)
    assert len(out) < 300


def test_job_file_small_file_is_complete() -> None:
    assert _api(content="a\nb\n").job_file("1", "s.txt", max_bytes=200) == "a\nb\n"


def test_job_file_preserves_utf8_across_head_tail_boundary() -> None:
    body = "aé" * 7
    assert _api(content=body).job_file("1", "s.txt", max_bytes=22) == body


@pytest.mark.parametrize("method", ["text_or_empty", "job_file"])
@pytest.mark.parametrize("status", [404, 403, 500, None])
def test_optional_fetch_logs_failures(method: str, status: int | None, caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if status is None:
            msg = "timed out"
            raise httpx.ReadTimeout(msg, request=request)
        return httpx.Response(status)

    api = _api(httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="openqa_llm_investigate.http"):
        out = (
            api.text_or_empty("/x?token=secret-value")
            if method == "text_or_empty"
            else api.job_file("1", "x?token=secret-value")
        )
    assert not out
    records = [r for r in caplog.records if r.name == "openqa_llm_investigate.http"]
    assert len(records) == 1
    assert records[0].levelno == (logging.DEBUG if status == 404 else logging.WARNING)
    assert "https://openqa.example/" in records[0].getMessage()
    assert "secret-value" not in records[0].getMessage()
