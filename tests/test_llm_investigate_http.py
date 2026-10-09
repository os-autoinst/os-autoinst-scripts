# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.http."""

from __future__ import annotations

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
