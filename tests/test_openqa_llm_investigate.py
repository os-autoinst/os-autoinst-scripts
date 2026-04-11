# Copyright SUSE LLC
"""Unit tests for openqa-llm-investigate."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import pathlib
import sys
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

# Load the script as module "llm_investigate" (the file is named `openqa-llm-investigate`)
rootpath = pathlib.Path(__file__).parent.parent.resolve()
loader = importlib.machinery.SourceFileLoader("llm_investigate", f"{rootpath}/openqa-llm-investigate")
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
llm_investigate = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = llm_investigate
loader.exec_module(llm_investigate)


class TestFetchJson:
    def test_fetch_json_success(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        mock_response = Mock()
        mock_response.json.return_value = {"foo": "bar"}
        mock_client.get.return_value = mock_response

        res = llm_investigate.fetch_json(mock_client, "http://example.com")
        assert res == {"foo": "bar"}
        mock_client.get.assert_called_once_with("http://example.com")
        mock_response.raise_for_status.assert_called_once()

    def test_fetch_json_failure(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=Mock(), response=Mock())
        mock_client.get.return_value = mock_response

        # Default failure returns {}
        res = llm_investigate.fetch_json(mock_client, "http://example.com/api/v1/jobs/123")
        assert res == {}

        # Comments failure returns []
        res = llm_investigate.fetch_json(mock_client, "http://example.com/api/v1/jobs/123/comments")
        assert res == []


class TestFetchText:
    def test_fetch_text_success(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        mock_response = Mock()
        # Create 300 lines of text
        mock_response.text = "\n".join(f"line {i}" for i in range(300))
        mock_client.get.return_value = mock_response

        res = llm_investigate.fetch_text(mock_client, "http://example.com", max_lines=200)
        lines = res.splitlines()
        assert len(lines) == 200
        assert lines[-1] == "line 299"

    def test_fetch_text_failure(self) -> None:
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = httpx.RequestError("error")

        res = llm_investigate.fetch_text(mock_client, "http://example.com")
        assert res == ""


@patch("llm_investigate.subprocess.run")
def test_post_comment(mock_run: MagicMock) -> None:
    llm_investigate.post_comment("http://base", "123", "test comment")
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "openqa-cli" in args
    assert "jobs/123/comments" in args
    assert "text=test comment" in args


@patch("llm_investigate.httpx.Client")
@patch("llm_investigate.post_comment")
@patch("builtins.print")
def test_investigate_cmd(mock_print: MagicMock, mock_post: MagicMock, mock_client_class: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    def mock_get(url: str, *args: Any, **kwargs: Any) -> Mock:
        _ = args, kwargs
        resp = Mock()
        if "comments" in url:
            resp.json.return_value = []
        elif "api/v1/jobs" in url and ("build=" in url or "test=" in url):
            resp.json.return_value = {"jobs": [{"id": 123}, {"id": 124}]}
        elif "api/v1/jobs" in url:
            resp.json.return_value = {
                "job": {
                    "id": 123,
                    "result": "failed",
                    "test": "my_test",
                    "settings": {
                        "BUILD": "1.0",
                        "DISTRI": "opensuse",
                        "VERSION": "Tumbleweed",
                        "ARCH": "x86_64",
                        "FLAVOR": "DVD",
                    },
                }
            }
        elif "investigation_ajax" in url:
            resp.json.return_value = {"diff_to_last_good": {}}
        elif "autoinst-log.txt" in url:
            resp.text = "failed log"
        else:
            resp.json.return_value = {}
        return resp

    mock_client.get.side_effect = mock_get

    def mock_post_call(url: str, json: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> Mock:
        _ = url, json, args, kwargs
        resp = Mock()
        resp.json.return_value = {"choices": [{"message": {"content": "INVESTIGATE: YES. It is broken."}}]}
        return resp

    mock_client.post.side_effect = mock_post_call

    llm_investigate.investigate("123")

    mock_print.assert_called_once_with("https://openqa.opensuse.org/tests/123")
    mock_post.assert_called_once()
    assert "INVESTIGATE: YES" in mock_post.call_args[0][2]


@patch("llm_investigate.httpx.Client")
@patch("builtins.print")
def test_investigate_cmd_passed_job(mock_print: MagicMock, mock_client_class: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    def mock_get(url: str, *args: Any, **kwargs: Any) -> Mock:
        _ = args, kwargs
        resp = Mock()
        if "comments" in url:
            resp.json.return_value = []
        elif "api/v1/jobs" in url:
            resp.json.return_value = {"job": {"id": 123, "result": "passed"}}
        return resp

    mock_client.get.side_effect = mock_get

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 0
    mock_print.assert_not_called()


@patch("llm_investigate.httpx.Client")
@patch("builtins.print")
def test_investigate_cmd_softfailed_job(mock_print: MagicMock, mock_client_class: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    def mock_get(url: str, *args: Any, **kwargs: Any) -> Mock:
        _ = args, kwargs
        resp = Mock()
        if "comments" in url:
            resp.json.return_value = []
        elif "api/v1/jobs" in url:
            resp.json.return_value = {"job": {"id": 123, "result": "softfailed"}}
        return resp

    mock_client.get.side_effect = mock_get

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 0
    mock_print.assert_not_called()


@patch("llm_investigate.httpx.Client")
@patch("llm_investigate.log")
def test_investigate_cmd_already_commented(mock_log: MagicMock, mock_client_class: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    def mock_get(url: str, *args: Any, **kwargs: Any) -> Mock:
        _ = args, kwargs
        resp = Mock()
        if "comments" in url:
            resp.json.return_value = [{"text": "**LLM Investigation summary:** already done"}]
        else:
            resp.json.return_value = {}
        return resp

    mock_client.get.side_effect = mock_get

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 0
    mock_log.info.assert_called_once()
    assert "already has an LLM investigation summary" in mock_log.info.call_args[0][0]


@patch("llm_investigate.httpx.Client")
@patch("llm_investigate.post_comment")
@patch("builtins.print")
def test_investigate_cmd_dry_run(mock_print: MagicMock, mock_post: MagicMock, mock_client_class: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    def mock_get(url: str, *args: Any, **kwargs: Any) -> Mock:
        _ = args, kwargs
        resp = Mock()
        if "comments" in url:
            resp.json.return_value = []
        elif "api/v1/jobs" in url and "build=" not in url:
            resp.json.return_value = {
                "job": {"id": 123, "result": "failed", "test": "my_test", "settings": {"BUILD": "1.0"}}
            }
        elif "investigation_ajax" in url:
            resp.json.return_value = {"diff_to_last_good": {}}
        elif "api/v1/jobs?build" in url:
            resp.json.return_value = {"jobs": []}
        elif "autoinst-log.txt" in url:
            resp.text = "failed log"
        else:
            resp.json.return_value = {}
        return resp

    mock_client.get.side_effect = mock_get

    def mock_post_call(url: str, json: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> Mock:
        _ = url, json, args, kwargs
        resp = Mock()
        resp.json.return_value = {"choices": [{"message": {"content": "INVESTIGATE: NO. Already known."}}]}
        return resp

    mock_client.post.side_effect = mock_post_call

    llm_investigate.investigate("123", dry=True)

    # In dry run, it should print the summary instead of posting
    mock_post.assert_not_called()
    mock_print.assert_any_call("**LLM Investigation summary:**\n\nINVESTIGATE: NO. Already known.")


@patch("llm_investigate.httpx.Client")
@patch("llm_investigate.log")
def test_investigate_cmd_connection_error(mock_log: MagicMock, mock_client_class: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    def mock_get(url: str, *args: Any, **kwargs: Any) -> Mock:
        _ = args, kwargs
        resp = Mock()
        if "comments" in url:
            resp.json.return_value = []
        elif "api/v1/jobs" in url and "build=" not in url:
            resp.json.return_value = {"job": {"id": 123, "result": "failed", "test": "my_test"}}
        else:
            resp.json.return_value = {}
        return resp

    mock_client.get.side_effect = mock_get
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 1
    mock_log.error.assert_called_once()
    assert "Could not connect to LLM server" in mock_log.error.call_args[0][0]


@patch("llm_investigate.logging.basicConfig")
def test_investigate_logging_levels(mock_basic_config: MagicMock) -> None:
    # We need to mock httpx.Client to avoid real network calls during investigate() call
    with patch("llm_investigate.httpx.Client"), patch("llm_investigate.fetch_json") as mock_fetch:

        def mock_fetch_side_effect(_client: Any, url: str) -> Any:
            if "comments" in url:
                return []
            return {"job": {"id": 123, "result": "passed"}}

        mock_fetch.side_effect = mock_fetch_side_effect

        # Test default: warning
        with pytest.raises(SystemExit):
            llm_investigate.investigate("123", verbose=0, quiet=False)
        mock_basic_config.assert_called_with(
            level=logging.WARNING, format="%(levelname)s: %(message)s", stream=sys.stderr, force=True
        )

        # Test verbose 1: info
        with pytest.raises(SystemExit):
            llm_investigate.investigate("123", verbose=1, quiet=False)
        mock_basic_config.assert_called_with(
            level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr, force=True
        )

        # Test verbose 2: debug
        with pytest.raises(SystemExit):
            llm_investigate.investigate("123", verbose=2, quiet=False)
        mock_basic_config.assert_called_with(
            level=logging.DEBUG, format="%(levelname)s: %(message)s", stream=sys.stderr, force=True
        )

        # Test quiet: error
        with pytest.raises(SystemExit):
            llm_investigate.investigate("123", verbose=0, quiet=True)
        mock_basic_config.assert_called_with(
            level=logging.ERROR, format="%(levelname)s: %(message)s", stream=sys.stderr, force=True
        )
