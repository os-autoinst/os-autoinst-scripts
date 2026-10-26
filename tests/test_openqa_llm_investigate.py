# Copyright SUSE LLC
"""Unit tests for openqa-llm-investigate."""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.machinery
import importlib.util
import json
import logging
import pathlib
import string
import subprocess
import sys
import urllib.parse
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, Mock, call

import httpx
import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# Load the script as module "llm_investigate" (the file is named `openqa-llm-investigate`)
rootpath = pathlib.Path(__file__).parent.parent.resolve()
loader = importlib.machinery.SourceFileLoader("llm_investigate", f"{rootpath}/openqa-llm-investigate")
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
llm_investigate = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = llm_investigate
loader.exec_module(llm_investigate)


def test_fetch_json_success() -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = Mock()
    mock_response.json.return_value = {"foo": "bar"}
    mock_client.get.return_value = mock_response

    res = llm_investigate.fetch_json(mock_client, "http://example.com")
    assert res == {"foo": "bar"}
    mock_client.get.assert_called_once_with("http://example.com", params=None)
    mock_response.raise_for_status.assert_called_once()

    # With explicit timeout
    mock_client.get.reset_mock()
    res_timeout = llm_investigate.fetch_json(mock_client, "http://example.com", timeout=900.0)
    assert res_timeout == {"foo": "bar"}
    mock_client.get.assert_called_once_with("http://example.com", params=None, timeout=900.0)


@pytest.mark.parametrize(
    "error",
    [
        httpx.HTTPStatusError("error", request=Mock(), response=Mock(status_code=500)),
        httpx.RequestError("network error", request=Mock()),
        ValueError("invalid json"),
    ],
)
def test_fetch_json_failure(error: Exception) -> None:
    mock_client = MagicMock(spec=httpx.Client)
    mock_response = Mock()
    if isinstance(error, ValueError):
        mock_response.json.side_effect = error
    elif isinstance(error, httpx.RequestError):
        mock_client.get.side_effect = error
    else:
        mock_response.raise_for_status.side_effect = error
    mock_client.get.return_value = mock_response

    # Default failure returns {}
    res = llm_investigate.fetch_json(mock_client, "http://example.com/api/v1/jobs/123")
    assert res == {}

    # Passing explicit default
    res = llm_investigate.fetch_json(mock_client, "http://example.com/api/v1/jobs/123/comments", default=[])
    assert res == []


def test_fetch_json_404(caplog: pytest.LogCaptureFixture) -> None:
    mock_client = MagicMock(spec=httpx.Client)
    resp_404 = httpx.Response(404, request=httpx.Request("GET", "http://example.com/api/v1/jobs/123"))
    mock_client.get.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=resp_404.request, response=resp_404
    )

    with caplog.at_level(logging.WARNING):
        res = llm_investigate.fetch_json(mock_client, "http://example.com/api/v1/jobs/123")
    assert res == {"error_status": 404}
    assert "Failed to fetch JSON" not in caplog.text


def test_post_comment(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("llm_investigate.subprocess.run")
    llm_investigate.post_comment("http://base", "123", "test comment")
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "openqa-cli" in args
    assert "jobs/123/comments" in args
    assert "text=test comment" in args


def test_post_comment_404(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    err = subprocess.CalledProcessError(1, ["openqa-cli"], stderr="404 Not Found\n")
    mocker.patch("llm_investigate.subprocess.run", side_effect=err)
    with caplog.at_level(logging.INFO):
        llm_investigate.post_comment("http://base", "123", "test comment")
    assert "Could not post comment as job 123 has been deleted." in caplog.text
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_investigate_deleted_job(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    mocker.patch("llm_investigate.analyze_job")
    setup_mock_client(mocker, overrides={"api/v1/jobs": {"error_status": 404}})
    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")
    assert exc.value.code == 0
    mock_log.info.assert_called_once_with("Skipping investigation as job %s has been deleted.", "123")
    mock_log.error.assert_not_called()
    mock_log.warning.assert_not_called()


CHAT_REPLIES = {
    1: "* isosize: the test died with boom",
    3: "The `isosize` module died.",
    5: "Classification: Product bug\nBISECT: YES \u2014 isolated\nRestart: NO \u2014 product error",
}


def setup_mock_client(mocker: MockerFixture, overrides: dict[str, Any] | None = None) -> MagicMock:
    mock_client_class = mocker.patch("llm_investigate.httpx.Client")
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    responses = {
        "comments": [],
        "api/v1/jobs?build": {
            "jobs": [
                {"id": 123, "modules": [{"name": "isosize", "result": "failed"}]},
                {"id": 124, "modules": [{"name": "other_mod", "result": "failed"}]},
            ]
        },
        "test=": {
            "jobs": [
                {"id": 120, "state": "done", "result": "passed"},
                {"id": 121, "state": "done", "result": "failed"},
                {"id": 122, "state": "done", "result": "failed"},
                {"id": 123, "state": "done", "result": "failed"},
            ]
        },
        "api/v1/jobs": {
            "job": {
                "id": 123,
                "result": "failed",
                "test": "my_test",
                "testresults": [{"name": "isosize", "result": "failed"}],
                "settings": {
                    "BUILD": "1.0",
                    "DISTRI": "opensuse",
                    "VERSION": "Tumbleweed",
                    "ARCH": "x86_64",
                    "FLAVOR": "DVD",
                },
            }
        },
        "investigation_ajax": {"diff_to_last_good": {}},
        "details_ajax": {
            "modules": [
                {
                    "name": "isosize",
                    "category": "installation",
                    "result": "failed",
                    "details": [
                        {"num": 1, "result": "fail", "display_title": "Failed", "text_data": "# Test died: boom"}
                    ],
                }
            ]
        },
        "autoinst-log.txt": "failed log",
    }
    if overrides:
        responses.update(overrides)

    def mock_get(url: str, params: Any = None, **kwargs: Any) -> Mock:
        _ = kwargs
        query_str = f"?{urllib.parse.urlencode(params)}" if params else ""
        full_url = f"{url}{query_str}"
        for pattern, val in responses.items():
            if pattern in full_url:
                return Mock(json=Mock(return_value=val), text=val if isinstance(val, str) else json.dumps(val))
        return Mock(json=Mock(return_value={}), text="")

    mock_client.get.side_effect = mock_get

    def mock_stream(method: str, url: str, **kwargs: Any) -> MagicMock:
        _ = method, kwargs
        ctx = MagicMock()
        ctx.__enter__.return_value.iter_bytes.return_value = [mock_get(url).text.encode()]
        return ctx

    mock_client.stream.side_effect = mock_stream

    def mock_post_call(url: str, json: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> Mock:
        _ = url, args, kwargs
        reply = CHAT_REPLIES[len(json["messages"]) if json else 1]
        return Mock(json=Mock(return_value={"choices": [{"message": {"content": reply}}]}))

    mock_client.post.side_effect = mock_post_call
    return mock_client


def test_investigate_cmd(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker)
    llm_investigate.investigate("123")
    mock_print.assert_called_once_with("https://openqa.opensuse.org/tests/123")
    mock_post.assert_called_once()
    comment = mock_post.call_args[0][2]
    assert comment.index("* isosize:") < comment.index("`isosize` module died") < comment.index("Classification:")
    assert "BISECT: YES" in comment
    assert comment.endswith(f"\n\n{llm_investigate.COMMENT_FOOTER}")
    assert comment.endswith(" flag:verbatim")
    assert client.post.call_count == 3
    ajax_calls = [call for call in client.get.call_args_list if "investigation_ajax" in call[0][0]]
    assert len(ajax_calls) == 1
    assert ajax_calls[0][1]["timeout"] == pytest.approx(900.0)


def test_investigate_cmd_with_token(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch.dict("os.environ", {"LLM_API_TOKEN": "my-secret-token"})
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123", llm_token="my-secret-token")

    assert client.post.call_count == 3
    assert client.post.call_args[1]["headers"] == {"Authorization": "Bearer my-secret-token"}
    mock_post.assert_called_once()


def test_investigate_cmd_passed_job(mocker: MockerFixture) -> None:
    mock_print = mocker.patch("llm_investigate.typer.echo")
    setup_mock_client(mocker, overrides={"api/v1/jobs": {"job": {"id": 123, "result": "passed"}}})

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 0
    mock_print.assert_not_called()


def test_investigate_cmd_softfailed_job(mocker: MockerFixture) -> None:
    mock_print = mocker.patch("llm_investigate.typer.echo")
    setup_mock_client(mocker, overrides={"api/v1/jobs": {"job": {"id": 123, "result": "softfailed"}}})

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 0
    mock_print.assert_not_called()


def test_investigate_cmd_already_commented(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    setup_mock_client(mocker, overrides={"comments": [{"text": "**LLM Investigation summary:** already done"}]})

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 0
    mock_log.info.assert_called_once()
    assert "already has an LLM investigation summary" in mock_log.info.call_args[0][0]


def test_investigate_cmd_already_commented_with_force(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    setup_mock_client(mocker, overrides={"comments": [{"text": "**LLM Investigation summary:** already done"}]})

    llm_investigate.investigate("123", force=True)

    mock_print.assert_called_with("https://openqa.opensuse.org/tests/123")
    mock_post.assert_called_once()
    assert "BISECT: YES" in mock_post.call_args[0][2]


def test_investigate_cmd_dry_run(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker)
    client.post.side_effect = lambda *_, **__: Mock(
        json=Mock(return_value={"choices": [{"message": {"content": "BISECT: NO. Already known."}}]})
    )

    llm_investigate.investigate("123", dry=True)

    # In dry run, it should print the summary instead of posting
    mock_post.assert_not_called()
    summary = "\n\n".join(["BISECT: NO. Already known."] * 3)
    mock_print.assert_any_call(f"**LLM Investigation summary:**\n\n{summary}")
    assert all(llm_investigate.COMMENT_FOOTER not in str(c) for c in mock_print.call_args_list)


def test_investigate_cmd_print_prompt(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(
        mocker, overrides={"comments": [{"text": "**LLM Investigation summary:** already done"}]}
    )

    llm_investigate.investigate("123", print_prompt=True)

    client.post.assert_not_called()
    mock_post.assert_not_called()
    assert not any("/comments" in str(c.args[0]) for c in client.get.call_args_list)
    output = mock_print.call_args[0][0]
    assert all(s in output for s in ("Prompt 1/3", "Question 1", "Question 2", "Question 3"))


def test_investigate_cmd_connection_error(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    client = setup_mock_client(mocker)
    client.post.side_effect = httpx.ConnectError("Connection refused")

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 1
    mock_log.exception.assert_called_once()
    assert "Could not connect to LLM server" in mock_log.exception.call_args[0][0]


def test_investigate_cmd_http_status_error(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    client = setup_mock_client(mocker)
    mock_resp = Mock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    client.post.side_effect = httpx.HTTPStatusError("error", request=Mock(), response=mock_resp)

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 1
    mock_log.exception.assert_called_once()
    error_msg = mock_log.exception.call_args[0][0]
    assert error_msg == "LLM API %s (%s) status %d: %s"
    assert mock_log.exception.call_args[0][1] == "http://localhost:8080/v1/chat/completions"
    assert mock_log.exception.call_args[0][2] == "gemma-4-26B-A4B-it"
    assert mock_log.exception.call_args[0][3] == 500
    assert mock_log.exception.call_args[0][4] == "Internal Server Error"


def test_investigate_cmd_timeout(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    client = setup_mock_client(mocker)
    client.post.side_effect = httpx.ReadTimeout("timed out")

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 1
    mock_log.exception.assert_called_once()
    assert mock_log.exception.call_args[0][0] == "LLM API %s (%s) failed"
    assert mock_log.exception.call_args[0][1] == "http://localhost:8080/v1/chat/completions"
    assert mock_log.exception.call_args[0][2] == "gemma-4-26B-A4B-it"


def test_investigate_logging_levels(mocker: MockerFixture) -> None:
    mock_basic_config = mocker.patch("llm_investigate.logging.basicConfig")
    mocker.patch("llm_investigate.httpx.Client")
    mock_fetch = mocker.patch("llm_investigate.fetch_json")

    def mock_fetch_side_effect(_client: Any, url: str, params: Any = None, **_kwargs: Any) -> Any:
        _ = params
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


def test_retry_transport_no_retry_on_200(mocker: MockerFixture) -> None:
    # If the response is 200, we should not retry and sleep should not be called.
    mock_sleep = mocker.patch("time.sleep")
    mock_super_handle = mocker.patch("llm_investigate.httpx.HTTPTransport.handle_request")

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_super_handle.return_value = mock_response

    transport = llm_investigate.RetryTransport(retries=3)
    req = httpx.Request("GET", "http://example.com")
    res = transport.handle_request(req)

    assert res == mock_response
    assert mock_super_handle.call_count == 1
    mock_sleep.assert_not_called()


def test_retry_transport_retry_after_seconds(mocker: MockerFixture) -> None:
    mock_sleep = mocker.patch("time.sleep")
    mock_super_handle = mocker.patch("llm_investigate.httpx.HTTPTransport.handle_request")

    resp429 = MagicMock(spec=httpx.Response)
    resp429.status_code = 429
    resp429.headers = httpx.Headers({"Retry-After": "5"})

    resp200 = MagicMock(spec=httpx.Response)
    resp200.status_code = 200

    mock_super_handle.side_effect = [resp429, resp200]

    transport = llm_investigate.RetryTransport(retries=2)
    req = httpx.Request("GET", "http://example.com")
    res = transport.handle_request(req)

    assert res == resp200
    assert mock_super_handle.call_count == 2
    mock_sleep.assert_called_once_with(5.0)
    resp429.close.assert_called_once()


def test_retry_transport_retry_on_503(mocker: MockerFixture) -> None:
    mock_sleep = mocker.patch("time.sleep")
    mock_super_handle = mocker.patch("llm_investigate.httpx.HTTPTransport.handle_request")

    resp503 = MagicMock(spec=httpx.Response)
    resp503.status_code = 503
    resp503.headers = httpx.Headers({})  # No Retry-After header

    resp200 = MagicMock(spec=httpx.Response)
    resp200.status_code = 200

    mock_super_handle.side_effect = [resp503, resp200]

    transport = llm_investigate.RetryTransport(retries=2)
    req = httpx.Request("GET", "http://example.com")
    res = transport.handle_request(req)

    assert res == resp200
    assert mock_super_handle.call_count == 2
    mock_sleep.assert_called_once_with(1.0)  # Exponential backoff first step
    resp503.close.assert_called_once()


def test_retry_transport_retry_after_http_date(mocker: MockerFixture) -> None:
    mock_sleep = mocker.patch("time.sleep")
    mock_super_handle = mocker.patch("llm_investigate.httpx.HTTPTransport.handle_request")

    resp429 = MagicMock(spec=httpx.Response)
    resp429.status_code = 429
    # Date is Oct 21 2015 07:28:00 UTC (10 seconds later than now)
    resp429.headers = httpx.Headers({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})

    resp200 = MagicMock(spec=httpx.Response)
    resp200.status_code = 200

    mock_super_handle.side_effect = [resp429, resp200]

    transport = llm_investigate.RetryTransport(retries=2)

    # Mock _now method to return static now (Oct 21 2015 07:27:50 UTC)
    static_now = dt.datetime(2015, 10, 21, 7, 27, 50, tzinfo=dt.UTC)
    mocker.patch.object(transport, "_now", return_value=static_now)

    req = httpx.Request("GET", "http://example.com")
    res = transport.handle_request(req)

    assert res == resp200
    assert mock_super_handle.call_count == 2
    mock_sleep.assert_called_once_with(10.0)
    resp429.close.assert_called_once()


def test_retry_transport_exponential_backoff(mocker: MockerFixture) -> None:
    mock_sleep = mocker.patch("time.sleep")
    mock_super_handle = mocker.patch("llm_investigate.httpx.HTTPTransport.handle_request")

    resp429_1 = MagicMock(spec=httpx.Response)
    resp429_1.status_code = 429
    resp429_1.headers = httpx.Headers({})  # No Retry-After header

    resp429_2 = MagicMock(spec=httpx.Response)
    resp429_2.status_code = 429
    resp429_2.headers = httpx.Headers({})  # No Retry-After header

    resp200 = MagicMock(spec=httpx.Response)
    resp200.status_code = 200

    mock_super_handle.side_effect = [resp429_1, resp429_2, resp200]

    transport = llm_investigate.RetryTransport(retries=3)
    req = httpx.Request("GET", "http://example.com")
    res = transport.handle_request(req)

    assert res == resp200
    assert mock_super_handle.call_count == 3

    # First sleep should be 2 ** (1 - 1) = 1.0 second
    # Second sleep should be 2 ** (2 - 1) = 2.0 seconds
    mock_sleep.assert_has_calls([mocker.call(1.0), mocker.call(2.0)])
    resp429_1.close.assert_called_once()
    resp429_2.close.assert_called_once()


def test_retry_transport_retry_on_timeout(mocker: MockerFixture) -> None:
    mock_sleep = mocker.patch("time.sleep")
    mock_log = mocker.patch("llm_investigate.log")
    mock_super_handle = mocker.patch("llm_investigate.httpx.HTTPTransport.handle_request")

    resp200 = MagicMock(spec=httpx.Response)
    resp200.status_code = 200

    mock_super_handle.side_effect = [httpx.ReadTimeout("timed out"), resp200]

    transport = llm_investigate.RetryTransport(retries=2)
    req = httpx.Request("POST", "http://example.com")
    res = transport.handle_request(req)

    assert res == resp200
    assert mock_super_handle.call_count == 2
    mock_sleep.assert_called_once_with(1.0)
    mock_log.info.assert_called_once_with(
        "Request timed out (%s). Retrying in %.2f seconds (%d retries left)...",
        "ReadTimeout",
        1.0,
        1,
    )


def test_retry_transport_timeout_exhausted(mocker: MockerFixture) -> None:
    mock_sleep = mocker.patch("time.sleep")
    mock_log = mocker.patch("llm_investigate.log")
    mock_super_handle = mocker.patch("llm_investigate.httpx.HTTPTransport.handle_request")
    mock_super_handle.side_effect = httpx.ConnectTimeout("timed out")

    transport = llm_investigate.RetryTransport(retries=2)
    req = httpx.Request("POST", "http://example.com")

    with pytest.raises(httpx.ConnectTimeout):
        transport.handle_request(req)

    assert mock_super_handle.call_count == 3
    assert mock_sleep.call_count == 2
    assert mock_log.info.call_count == 2
    mock_log.info.assert_has_calls([
        mocker.call(
            "Request timed out (%s). Retrying in %.2f seconds (%d retries left)...",
            "ConnectTimeout",
            1.0,
            1,
        ),
        mocker.call(
            "Request timed out (%s). Retrying in %.2f seconds (%d retries left)...",
            "ConnectTimeout",
            2.0,
            0,
        ),
    ])


def test_investigate_configures_retry_transport(mocker: MockerFixture) -> None:
    # Verify that calling investigate with a specific retries value configures RetryTransport
    mock_client_class = mocker.patch("llm_investigate.httpx.Client")
    mock_retry_transport_class = mocker.patch("llm_investigate.RetryTransport")
    mocker.patch("llm_investigate._perform_investigation")

    llm_investigate.investigate("123", retries=4)

    mock_retry_transport_class.assert_called_once_with(retries=4)
    mock_client_class.assert_called_once()
    assert mock_client_class.call_args[1]["transport"] == mock_retry_transport_class.return_value
    assert mock_client_class.call_args[1]["timeout"] == pytest.approx(90.0)


def test_investigate_configures_timeouts(mocker: MockerFixture) -> None:
    mock_client_class = mocker.patch("llm_investigate.httpx.Client")
    mock_perform = mocker.patch("llm_investigate._perform_investigation")

    llm_investigate.investigate("123", openqa_timeout=45.0, investigation_timeout=60.0)

    assert mock_client_class.call_args[1]["timeout"] == pytest.approx(45.0)
    mock_perform.assert_called_once()
    assert mock_perform.call_args[0][3].investigation_timeout == pytest.approx(60.0)


def _prompt_sent(client: MagicMock) -> str:
    return client.post.call_args_list[0][1]["json"]["messages"][0]["content"]


def _question(client: MagicMock, n: int) -> str:
    return client.post.call_args[1]["json"]["messages"][2 * (n - 1)]["content"]


def test_investigate_prompt_contains_analysis_and_instructions(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123")

    prompt = _prompt_sent(client)
    assert "OpenQA Failed Job Analysis:" in prompt
    assert "Failed Module: installation/isosize" in prompt
    assert "context window of 100 lines" in prompt
    assert "Question 1:" in prompt
    assert "BISECT" not in prompt
    assert "Question 2:" in _question(client, 2)
    verdict_question = _question(client, 3)
    assert "Classification: Product bug | Test issue | Infrastructure problem" in verdict_question
    assert "BISECT: YES | NO" in verdict_question
    assert "Restart: YES | NO" in verdict_question
    assert "failing in the exact same module(s): 0" in verdict_question


def test_investigate_passes_context_lines_to_analysis(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    analyze = mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123", context_lines=7)

    assert analyze.call_args[1]["context_lines"] == 7
    assert analyze.call_args[1]["include_softfailed"] is False
    assert analyze.call_args[1]["job"]["test"] == "my_test"
    assert "the analysis" in _prompt_sent(client)
    assert "context window of 7 lines" in _prompt_sent(client)


def test_investigate_forced_softfailed_job_includes_softfailed_modules(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    analyze = mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    setup_mock_client(mocker, overrides={"api/v1/jobs": {"job": {"id": 123, "result": "softfailed"}}})

    llm_investigate.investigate("123", force=True)

    assert analyze.call_args[1]["include_softfailed"] is True


def test_investigate_no_failed_modules_skips(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.analyze_job", side_effect=llm_investigate.NoFailedModulesError("none", "partial"))
    client = setup_mock_client(mocker)

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 0
    client.post.assert_not_called()


def test_investigate_no_failed_modules_forced_uses_partial_output(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch(
        "llm_investigate.analyze_job", side_effect=llm_investigate.NoFailedModulesError("none", "partial text")
    )
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123", force=True)

    assert "partial text" in _prompt_sent(client)


def test_investigate_analysis_failure_exits(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    mocker.patch("llm_investigate.analyze_job", side_effect=llm_investigate.AnalyzerError("no details"))
    client = setup_mock_client(mocker)

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 1
    assert mock_log.exception.call_args[0][0] == "Could not analyze job %s"
    client.post.assert_not_called()


def test_investigate_unexpected_analysis_error_exits(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    mocker.patch("llm_investigate.analyze_job", side_effect=IndexError("boom"))
    client = setup_mock_client(mocker)

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 1
    mock_log.exception.assert_called_once()
    client.post.assert_not_called()


def test_investigate_log_lines_caps_analysis(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="".join(f"line{i}\n" for i in range(10)))
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123", log_lines=3)

    prompt = _prompt_sent(client)
    assert "line2\n[... 7 lines omitted: --log-lines cap of 3 reached ...]" in prompt
    assert "line3" not in prompt
    assert "truncated to the first 3 lines" in prompt


def test_investigate_short_analysis_is_not_marked_truncated(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="short\n")
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123")

    assert "context window of 100 lines):\nshort\n" in _prompt_sent(client)


def test_investigate_redacts_investigation_info(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    client = setup_mock_client(mocker, {"investigation_ajax": {"diff_to_last_good": {"REPO": "http://u:pw@h/r"}}})

    llm_investigate.investigate("123")

    assert "u:pw@" not in _prompt_sent(client)


@pytest.mark.parametrize("diff", [None, "", {}, [], "  "])
def test_investigate_omits_empty_investigation_info(mocker: MockerFixture, diff: Any) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    client = setup_mock_client(mocker, {"investigation_ajax": {"diff_to_last_good": diff}})

    llm_investigate.investigate("123")

    prompt = _prompt_sent(client)
    assert "Investigation info" not in prompt
    assert "None available" not in prompt
    assert "- Result:" in prompt
    assert "\n\nFailure analysis (" in prompt


def test_investigate_omits_investigation_info_when_the_key_is_missing(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    client = setup_mock_client(mocker, {"investigation_ajax": {"last_good": None}})

    llm_investigate.investigate("123")

    assert "Investigation info" not in _prompt_sent(client)


def test_investigate_shows_a_diff_string_as_plain_text(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    diff = '-   "BUILD" : "1",\n+   "BUILD" : "2",\n'
    client = setup_mock_client(mocker, {"investigation_ajax": {"diff_to_last_good": diff}})

    llm_investigate.investigate("123")

    assert f"Investigation info (diff to last good):\n{diff}\nFailure analysis (" in _prompt_sent(client)


def test_investigate_shows_a_structured_diff_as_indented_json(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    client = setup_mock_client(mocker, {"investigation_ajax": {"diff_to_last_good": {"BUILD": ["1", "2"]}}})

    llm_investigate.investigate("123")

    assert '{\n  "BUILD": [\n    "1",\n    "2"\n  ]\n}\n\nFailure analysis' in _prompt_sent(client)


def test_investigation_text_is_redacted() -> None:
    assert "pw" not in llm_investigate._investigation_text("+ url http://u:pw@h/r")


_COMMITS_OVERRIDES = {
    "api/v1/jobs": {
        "job": {
            "result": "failed",
            "testresults": [{"name": "isosize", "category": "installation", "result": "failed"}],
            "settings": {"BUILD": "1.0"},
        }
    },
    "investigation_ajax": {
        "diff_to_last_good": '+   "BUILD" : "2",',
        "test_log": "1111111 Other\n lib/x.pm | 1 +\n2222222 Fix isosize\n tests/installation/isosize.pm | 1 +\n",
        "test_diff_stat": " lib/x.pm | 1 +\n tests/installation/isosize.pm | 1 +\n",
    },
}
_COMMITS_SECTION = (
    "Test commits since the last good job:\n"
    "[failed module]: changes the failed module's test file;"
    " [stack]: changes a file in the failing step's stack trace\n"
    "[failed module] 2222222 Fix isosize\nand 1 other test commit\n\n"
)


def test_investigate_prompt_shows_test_commits_since_last_good(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    baseline = setup_mock_client(mocker, {"api/v1/jobs": _COMMITS_OVERRIDES["api/v1/jobs"]})
    llm_investigate.investigate("123")
    client = setup_mock_client(mocker, _COMMITS_OVERRIDES)

    llm_investigate.investigate("123")

    assert f'+   "BUILD" : "2",\n\n{_COMMITS_SECTION}Failure analysis (' in _prompt_sent(client)
    assert "Test commits since" not in _prompt_sent(baseline)
    assert client.get.call_count == baseline.get.call_count
    assert sum("investigation_ajax" in c.args[0] for c in client.get.call_args_list) == 1


_LONG_ANALYSIS = "".join(f"line{i:05d}\n" for i in range(10000))


def test_investigate_context_tokens_keep_small_test_commits_and_cut_the_analysis(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value=_LONG_ANALYSIS)
    client = setup_mock_client(mocker, _COMMITS_OVERRIDES)

    llm_investigate.investigate("123", log_lines=100000, context_tokens=4096)

    prompt = _prompt_sent(client)
    assert f"{_COMMITS_SECTION}Failure analysis (context window of 100 lines, truncated to fit 4096" in prompt
    assert "--context-tokens budget reached" in prompt
    assert len(prompt) <= (4096 - llm_investigate.RESPONSE_RESERVE_TOKENS) * llm_investigate.CHARS_PER_TOKEN


def test_investigate_context_tokens_drop_large_test_commits_before_the_analysis(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mock_log = mocker.patch("llm_investigate.log")
    mocker.patch("llm_investigate.analyze_job", return_value=_LONG_ANALYSIS)
    log = "".join(f"{i:07x} Fix isosize {'x' * 60}\n tests/installation/isosize.pm | 1 +\n" for i in range(10))
    investigation = _COMMITS_OVERRIDES["investigation_ajax"] | {"test_log": log}
    client = setup_mock_client(mocker, _COMMITS_OVERRIDES | {"investigation_ajax": investigation})

    llm_investigate.investigate("123", log_lines=100000, context_tokens=4096)

    mock_log.info.assert_any_call("Test commits left out to fit --context-tokens=%d", 4096)
    assert "Test commits since" not in _prompt_sent(client)
    assert "--context-tokens budget reached" in _prompt_sent(client)


def test_fit_context_drops_test_commits_only_when_the_analysis_needs_their_room() -> None:
    names = {name for _, name, _, _ in string.Formatter().parse(llm_investigate.INTRO_TEMPLATE) if name}
    commits = "Test commits since the last good job:\n" + "x" * 30 + "\n\n"
    fields = dict.fromkeys(names, "") | {"commits_section": commits, "analysis": "a\n" * 2000}
    opts = llm_investigate.Options("u", "m", None, 1, 100, 500, 100, 20, context_tokens=0)
    note = llm_investigate.CONTEXT_TRUNCATED_NOTE.format(context_tokens=0)
    overhead = len(llm_investigate.INTRO_TEMPLATE.format(**fields | {"analysis": "", "truncated": note}))
    room = overhead + llm_investigate.OMISSION_NOTE_CHARS + len(commits) // 2
    tokens = llm_investigate.RESPONSE_RESERVE_TOKENS + -(-room // llm_investigate.CHARS_PER_TOKEN)
    assert len(commits) <= llm_investigate.COMMITS_MAX_SHARE * room

    fitted = llm_investigate._fit_context(fields, dataclasses.replace(opts, context_tokens=tokens))
    short = llm_investigate._fit_context(fields | {"analysis": "a\n"}, dataclasses.replace(opts, context_tokens=tokens))

    assert not fitted["commits_section"]
    assert "--context-tokens budget reached" in fitted["analysis"]
    assert short["commits_section"] == commits


@pytest.mark.parametrize(
    ("response", "printed"),
    [
        ("Summary.\n**BISECT: YES** \u2014 isolated\n", True),
        ("Summary.\n- bisect: yes \u2014 isolated", True),
        ("Other jobs: BISECT: YES would be wrong.\nBISECT: NO \u2014 widespread", False),
        ("BISECT: YES?\nBISECT: NO", False),
        ("Summary.\nBISECT: YES \u2014 isolated\n```\n", True),
        ("BISECT: YES \u2014 isolated\n\nHope this helps.", True),
        ("Summary.\n`BISECT: NO` \u2014 widespread\n", False),
        ("No verdict at all", False),
    ],
)
def test_investigate_bisect_gate_uses_last_verdict(mocker: MockerFixture, response: str, *, printed: bool) -> None:
    mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    client = setup_mock_client(mocker)
    client.post.side_effect = None
    client.post.return_value = Mock(json=Mock(return_value={"choices": [{"message": {"content": response}}]}))

    llm_investigate.investigate("123")

    assert (call("https://openqa.opensuse.org/tests/123") in mock_print.call_args_list) is printed


def test_investigate_warns_when_analysis_is_cut(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="a\nb\nc\n")
    setup_mock_client(mocker)
    llm_investigate.investigate("123", log_lines=1)
    assert "--log-lines=%d" in mock_log.info.call_args[0][0]


@pytest.mark.parametrize("option", ["--context-lines", "--log-lines"])
def test_negative_line_options_are_rejected(option: str) -> None:
    result = CliRunner().invoke(llm_investigate.app, ["123", option, "-1"])
    assert result.exit_code == 2


def _payload_sent(client: Mock) -> dict[str, Any]:
    return client.post.call_args_list[0].kwargs["json"]


def test_investigate_context_tokens_caps_prompt(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="".join(f"line{i:05d}\n" for i in range(10000)))
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123", log_lines=100000, context_tokens=4096)

    prompt = _prompt_sent(client)
    assert "--context-tokens budget reached" in prompt
    assert "truncated to fit 4096 tokens" in prompt
    assert "line00000" in prompt
    assert len(prompt) <= (4096 - llm_investigate.RESPONSE_RESERVE_TOKENS) * llm_investigate.CHARS_PER_TOKEN


def test_investigate_context_tokens_keeps_short_analysis(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="short\n")
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123", context_tokens=65536)

    assert "context window of 100 lines):\nshort\n" in _prompt_sent(client)


def test_investigate_context_tokens_too_small_exits(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    mocker.patch("llm_investigate.analyze_job", return_value="x\n")
    client = setup_mock_client(mocker)

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123", context_tokens=2100)

    assert exc.value.code == 1
    assert "--context-tokens=%d" in mock_log.error.call_args[0][0]
    client.post.assert_not_called()


def test_investigate_openai_payload_by_default(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123", context_tokens=8192)

    payload = _payload_sent(client)
    assert payload["temperature"] == pytest.approx(0.3)
    assert "options" not in payload
    assert "stream" not in payload


@pytest.mark.parametrize(
    ("url", "api"),
    [
        ("http://localhost:11434/api/chat", llm_investigate.LlmApi.AUTO),
        ("http://localhost:8080/v1/chat/completions", llm_investigate.LlmApi.OLLAMA),
    ],
)
def test_investigate_ollama_payload_and_response(mocker: MockerFixture, url: str, api: Any) -> None:
    mock_print = mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.post_comment")
    client = setup_mock_client(mocker)
    client.post.side_effect = None
    client.post.return_value = Mock(json=Mock(return_value={"message": {"content": "BISECT: YES"}}))

    llm_investigate.investigate("123", llm_url=url, llm_api=api, context_tokens=8192)

    payload = _payload_sent(client)
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": pytest.approx(0.3), "num_ctx": 8192}
    assert "temperature" not in payload
    mock_print.assert_called_once_with("https://openqa.opensuse.org/tests/123")


def test_investigate_ollama_payload_without_context_tokens(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.post_comment")
    client = setup_mock_client(mocker)
    client.post.side_effect = None
    client.post.return_value = Mock(json=Mock(return_value={"message": {"content": "BISECT: NO"}}))

    llm_investigate.investigate("123", llm_url="http://h/api/chat/")

    assert _payload_sent(client)["options"] == {"temperature": pytest.approx(0.3)}


def test_investigate_unexpected_response_shape_exits(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    client = setup_mock_client(mocker)
    client.post.side_effect = None
    client.post.return_value = Mock(json=Mock(return_value={"message": {"content": "x"}}))

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 1
    assert "no usable" in mock_log.exception.call_args[0][0]
    assert mock_log.exception.call_args[0][1:] == (
        "http://localhost:8080/v1/chat/completions",
        "gemma-4-26B-A4B-it",
        "openai",
    )


def test_negative_context_tokens_is_rejected() -> None:
    assert CliRunner().invoke(llm_investigate.app, ["123", "--context-tokens", "-1"]).exit_code == 2


def test_unknown_llm_api_is_rejected() -> None:
    assert CliRunner().invoke(llm_investigate.app, ["123", "--llm-api", "nope"]).exit_code == 2


def _vertex_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, creds: dict[str, str]) -> None:
    f = tmp_path / "adc.json"
    f.write_text(json.dumps(creds))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(f))
    monkeypatch.setenv("GCLOUD_PROJECT", "proj")
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)


def test_investigate_vertex_uses_refreshed_token_and_openai_payload(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    _vertex_env(
        monkeypatch, tmp_path, {"type": "authorized_user", "client_id": "i", "client_secret": "s", "refresh_token": "r"}
    )
    client = setup_mock_client(mocker)
    chat = client.post.side_effect
    client.post.side_effect = lambda url, **kw: (
        Mock(json=Mock(return_value={"access_token": "tok"}), raise_for_status=Mock())
        if "oauth2" in url
        else chat(url, **kw)
    )

    llm_investigate.investigate("123", llm_api=llm_investigate.LlmApi.VERTEX)

    token_call, chat_call = client.post.call_args_list[:2]
    assert token_call.kwargs["data"] == {
        "client_id": "i",
        "client_secret": "s",
        "refresh_token": "r",
        "grant_type": "refresh_token",
    }
    assert chat_call.args[0] == (
        "https://aiplatform.googleapis.com/v1/projects/proj/locations/global/endpoints/openapi/chat/completions"
    )
    assert chat_call.kwargs["headers"] == {"Authorization": "Bearer tok"}
    assert chat_call.kwargs["json"]["temperature"] == pytest.approx(0.3)


def test_vertex_request_regional_url_and_explicit_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _vertex_env(monkeypatch, tmp_path, {})
    monkeypatch.setenv("VERTEX_LOCATION", "europe-west4")
    client = Mock()

    url, token = llm_investigate._vertex_request(client, "explicit")

    assert url.startswith("https://europe-west4-aiplatform.googleapis.com/v1/projects/proj/locations/europe-west4/")
    assert token == "explicit"
    client.post.assert_not_called()


def test_investigate_vertex_claude_uses_raw_predict(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    _vertex_env(monkeypatch, tmp_path, {})
    client = setup_mock_client(mocker)
    client.post.side_effect = None
    client.post.return_value = Mock(json=Mock(return_value={"content": [{"type": "text", "text": "BISECT: NO"}]}))

    llm_investigate.investigate(
        "123", llm_api=llm_investigate.LlmApi.VERTEX, llm_model="claude-haiku-4-5", llm_token="t"
    )

    chat_call = client.post.call_args_list[0]
    assert chat_call.args[0].endswith("/locations/global/publishers/anthropic/models/claude-haiku-4-5:rawPredict")
    payload = chat_call.kwargs["json"]
    assert payload["anthropic_version"] == "vertex-2023-10-16"
    assert payload["max_tokens"] == 4096
    assert "temperature" not in payload
    assert "model" not in payload


def test_vertex_request_without_project_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    with pytest.raises(SystemExit):
        llm_investigate._vertex_request(Mock(), "t")


def test_vertex_request_rejects_service_account(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _vertex_env(monkeypatch, tmp_path, {"type": "service_account"})

    with pytest.raises(SystemExit):
        llm_investigate._vertex_request(Mock(), None)


def test_investigate_asks_three_questions_in_one_growing_chat(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker)
    sent: list[dict[str, Any]] = []
    post = client.post.side_effect
    client.post.side_effect = lambda url, json, **kw: (
        sent.append({"roles": [m["role"] for m in json["messages"]], "t": json["temperature"]}),
        post(url, json=json, **kw),
    )[1]

    llm_investigate.investigate("123")

    roles = ["user", "assistant", "user", "assistant", "user"]
    assert [s["roles"] for s in sent] == [roles[:1], roles[:3], roles[:5]]
    assert [s["t"] for s in sent] == [pytest.approx(0.3), pytest.approx(0.3), pytest.approx(0.0)]
    messages = client.post.call_args.kwargs["json"]["messages"]
    assert [m["content"] for m in messages if m["role"] == "assistant"][:2] == [CHAT_REPLIES[1], CHAT_REPLIES[3]]


def test_investigate_outage_hint_only_in_verdict_question(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate._build_failures", return_value=(5, 0))
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123", build_failure_threshold=1)

    assert "backend infrastructure outage" in _question(client, 3)
    assert "outage" not in _question(client, 1) + _question(client, 2)


def test_investigate_bisect_rule_forces_no_for_same_module_failures(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate._build_failures", return_value=(2, 1))
    setup_mock_client(mocker)

    llm_investigate.investigate("123")

    mock_print.assert_not_called()
    comment = mock_post.call_args[0][2]
    assert "BISECT: NO \u2014 1 other job(s) in this build fail in the same module" in comment
    assert "BISECT: YES" not in comment
    assert "Restart: NO" in comment


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("BISECT: NO \u2014 reason\nRestart: YES - Reason.", "BISECT: NO\nRestart: YES"),
        ("BISECT: NO \u2014 <short reason>", "BISECT: NO"),
        ("BISECT: YES \u2014 isolated failure", "BISECT: YES \u2014 isolated failure"),
        ("Restart: NO \u2014 a reason to wait", "Restart: NO \u2014 a reason to wait"),
    ],
)
def test_drop_placeholder_reasons(verdict: str, expected: str) -> None:
    assert llm_investigate._drop_placeholder_reasons(verdict) == expected


def test_investigate_drops_placeholder_reasons_from_comment(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    mocker.patch.dict(
        CHAT_REPLIES, {5: "Classification: Product bug\nBISECT: YES \u2014 reason\nRestart: NO \u2014 reason"}
    )
    setup_mock_client(mocker)

    llm_investigate.investigate("123")

    mock_print.assert_called_once_with("https://openqa.opensuse.org/tests/123")
    assert mock_post.call_args[0][2].endswith(f"BISECT: YES\nRestart: NO\n\n{llm_investigate.COMMENT_FOOTER}")


def test_submission_section_omitted_without_submissions() -> None:
    assert not llm_investigate._submission_section({})


def test_submission_section_lists_submissions_and_packages() -> None:
    section = llm_investigate._submission_section({44522: ["kubernetes1.18"], 44495: ["bar", "foo"], 7: []})
    assert section == (
        "Submissions under test (the job installs these changes):\n"
        "- 7: (packages unknown)\n- 44495: bar, foo\n- 44522: kubernetes1.18\n"
        "Treat failures related to these packages as a likely product regression.\n\n"
    )


def test_submissions_looked_up_from_job_settings(mocker: MockerFixture) -> None:
    mock = mocker.patch("llm_investigate.submission_packages", return_value={5: ["a"]})
    client = Mock()
    assert llm_investigate._submissions(client, {"settings": {"OS_TEST_ISSUES": "5"}}) == {5: ["a"]}
    mock.assert_called_once_with([5], client)


def test_submissions_empty_and_only_info_logged_when_smelt_fails(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.submission_packages", side_effect=httpx.ConnectError("down"))
    log = mocker.patch("llm_investigate.log")
    assert llm_investigate._submissions(Mock(), {"settings": {"OS_TEST_ISSUES": "5"}}) == {}
    log.info.assert_called_once()
    log.warning.assert_not_called()


def test_investigate_prompt_lists_submissions(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    mocker.patch("llm_investigate.submission_packages", return_value={5: ["foo"]})
    client = setup_mock_client(
        mocker,
        {
            "api/v1/jobs": {
                "job": {
                    "result": "failed",
                    "testresults": [{"name": "isosize", "result": "failed"}],
                    "settings": {"OS_TEST_ISSUES": "5"},
                }
            }
        },
    )

    llm_investigate.investigate("123")

    assert "Submissions under test" in _prompt_sent(client)
    assert "- 5: foo\n" in _prompt_sent(client)


def test_investigate_prompt_omits_submissions_without_test_issues(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate.analyze_job", return_value="the analysis")
    mock = mocker.patch("llm_investigate.submission_packages", return_value={})
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123")

    assert "Submissions under test" not in _prompt_sent(client)
    mock.assert_called_once_with([], client)


def _prompt(extension: Any) -> str:
    settings = {} if extension is None else {"LLM_PROMPT_EXTENSION": extension}
    stats = {"investigation_info": "None"}
    return llm_investigate._build_prompt(
        {"settings": settings}, "analysis", stats, llm_investigate.Options("http://llm", "m", None, 1, 50, 100, 10, 5)
    )


def test_build_prompt_names_the_machine() -> None:
    job = {"settings": {"ARCH": "x86_64", "MACHINE": "uefi", "FLAVOR": "DVD"}}
    opts = llm_investigate.Options("http://llm", "m", None, 1, 50, 100, 10, 5)
    prompt = llm_investigate._build_prompt(job, "analysis", {"investigation_info": ""}, opts)
    assert "- Arch: x86_64\n- Machine: uefi\n- Flavor: DVD\n" in prompt


def test_build_prompt_additional_instructions_after_job_details() -> None:
    prompt = _prompt("Ignore known flaky x11 tests.")
    assert "Additional instructions for this test:\nIgnore known flaky x11 tests." in prompt
    assert prompt.index("- Result:") < prompt.index("Additional instructions") < prompt.index("Investigation info")


@pytest.mark.parametrize("extension", [None, "", "  \n"])
def test_build_prompt_ignores_empty_additional_instructions(extension: str | None) -> None:
    prompt = _prompt(extension)
    assert "Additional instructions" not in prompt
    assert "- Result: unknown\n\nInvestigation info" in prompt


def test_consecutive_failures_found() -> None:
    mock_client = MagicMock()
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [
            {"id": 10, "state": "done", "result": "passed"},
            {"id": 11, "state": "done", "result": "failed"},
            {"id": 12, "state": "done", "result": "failed"},
        ]
    }
    mock_client.get.return_value = mock_response
    count, found = llm_investigate._consecutive_failures(
        mock_client, "http://example.com", "12", {"test": "t", "settings": {}}, limit=10
    )
    assert count == 2
    assert found is True


def test_consecutive_failures_not_found(caplog: pytest.LogCaptureFixture) -> None:
    mock_client = MagicMock()
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [
            {"id": 10, "state": "done", "result": "passed"},
            {"id": 11, "state": "done", "result": "failed"},
        ]
    }
    mock_client.get.return_value = mock_response
    with caplog.at_level(logging.INFO):
        count, found = llm_investigate._consecutive_failures(
            mock_client, "http://example.com", "12", {"test": "t", "settings": {}}, limit=10
        )
    assert count == 1
    assert found is False
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert "Job 12 not found in recent history (limit 10). Streak count may be inaccurate." in caplog.text


@pytest.mark.parametrize(
    ("group_id", "machine", "extra_params"),
    [
        (7, "uefi", {"machine": "uefi", "groupid": 7}),
        (None, "uefi", {"machine": "uefi", "groupid": 0}),
        (7, None, {"groupid": 7}),
    ],
    ids=["in-group", "without-group", "without-machine"],
)
def test_consecutive_failures_queries_one_scenario(
    group_id: int | None, machine: str | None, extra_params: dict[str, Any]
) -> None:
    # Runs on other machines or in other groups, such as verification clones, must not touch the streak
    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = {"jobs": []}
    settings = {"DISTRI": "opensuse", "VERSION": "Tumbleweed", "ARCH": "x86_64", "FLAVOR": "DVD", "MACHINE": machine}
    job = {"test": "t", "group_id": group_id, "settings": settings}
    llm_investigate._consecutive_failures(mock_client, "http://example.com", "12", job, limit=10)
    scenario = {"test": "t", "distri": "opensuse", "version": "Tumbleweed", "arch": "x86_64", "flavor": "DVD"}
    assert mock_client.get.call_args.kwargs["params"] == scenario | {"limit": 10} | extra_params


def _run(job_id: int, result: str, clone_id: int | None = None, state: str = "done") -> dict[str, Any]:
    return {"id": job_id, "state": state, "result": result, "clone_id": clone_id}


@pytest.mark.parametrize(
    ("jobs", "job_id", "expected"),
    [
        ([_run(10, "passed"), _run(11, "failed", 12), _run(12, "failed"), _run(13, "failed", 14)], "13", 2),
        ([_run(89, "passed"), _run(90, "failed", 110), _run(91, "failed"), _run(110, "failed")], "91", 2),
        (
            [
                _run(10, "passed"),
                _run(11, "failed", 12),
                _run(12, "incomplete", 13),
                _run(13, "failed"),
                _run(14, "failed"),
            ],
            "14",
            2,
        ),
        (
            [_run(10, "passed"), _run(11, "failed", 12), _run(12, "obsoleted", state="cancelled"), _run(13, "failed")],
            "13",
            2,
        ),
        (
            [_run(10, "failed"), _run(11, "passed", 12), _run(12, "skipped", state="cancelled"), _run(13, "failed")],
            "13",
            1,
        ),
        ([_run(10, "passed"), _run(11, "failed", 12), _run(12, "parallel_failed"), _run(13, "failed")], "13", 2),
        ([_run(10, "passed"), _run(11, "failed", 12), _run(13, "failed")], "13", 2),
    ],
    ids=[
        "clone-counted",
        "clone-after-job",
        "chain-of-restarts",
        "clone-obsoleted",
        "clone-skipped",
        "clone-parallel-failed",
        "clone-not-fetched",
    ],
)
def test_consecutive_failures_counts_restarted_jobs_once(
    jobs: list[dict[str, Any]], job_id: str, expected: int
) -> None:
    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = {"jobs": jobs}
    count, found = llm_investigate._consecutive_failures(
        mock_client, "http://example.com", job_id, {"test": "t", "settings": {}}, limit=10
    )
    assert (count, found) == (expected, True)


def test_superseded_ends_on_rows_whose_clones_loop() -> None:
    history = {
        10: {"id": 10, "state": "done", "result": "incomplete", "clone_id": 11},
        11: {"id": 11, "state": "done", "result": "incomplete", "clone_id": 10},
    }
    assert not llm_investigate._superseded(history[10], history, 20)


PENDING_STATES = {"scheduled", "assigned", "setup", "running", "uploading"}


def _build_jobs_client(jobs: list[dict[str, Any]]) -> MagicMock:
    # Like openQA, scope=relevant keeps a job only if it has no clone or a pending one
    states = {j["id"]: j["state"] for j in jobs}

    def get(_url: str, params: dict[str, Any], **_kwargs: Any) -> Mock:
        rows = [
            j
            for j in jobs
            if j["result"] == params["result"]
            and (
                params.get("scope") != "relevant"
                or j.get("clone_id") is None
                or states[j["clone_id"]] in PENDING_STATES
            )
        ]
        return Mock(json=Mock(return_value={"jobs": rows}))

    client = MagicMock()
    client.get.side_effect = get
    return client


def _failed_job(
    job_id: int, test: str, clone_id: int | None = None, group_id: int | None = 1, machine: str = "uefi"
) -> dict[str, Any]:
    return {
        "id": job_id,
        "test": test,
        "group_id": group_id,
        "state": "done",
        "result": "failed",
        "clone_id": clone_id,
        "settings": {"MACHINE": machine, "FLAVOR": "DVD", "ARCH": "x86_64"},
        "modules": [{"name": "isosize", "result": "failed"}],
    }


def _count_build_failures(jobs: list[dict[str, Any]]) -> tuple[int, int]:
    job = jobs[0] | {"testresults": jobs[0]["modules"]}
    return llm_investigate._build_failures(_build_jobs_client(jobs), "http://example.com", str(job["id"]), job)


def test_build_failures_counts_a_job_restarted_into_a_pending_clone() -> None:
    pending = {"id": 130, "state": "scheduled", "result": "none"}
    total, same = _count_build_failures([_failed_job(123, "t"), _failed_job(124, "u", clone_id=130), pending])
    assert (total, same) == (2, 1)
    assert llm_investigate._enforce_bisect_rule("BISECT: YES \u2014 isolated", same).startswith("BISECT: NO")


def test_build_failures_counts_retried_failures_with_running_clones() -> None:
    jobs = [_failed_job(i, f"t{i}", clone_id=i + 100) for i in range(1, 26)]
    jobs += [{"id": i + 100, "state": "running", "result": "none"} for i in range(1, 26)]
    assert _count_build_failures(jobs) == (25, 24)


def test_build_failures_counts_only_the_finished_clone_of_a_restarted_job() -> None:
    jobs = [_failed_job(123, "t"), _failed_job(124, "u", clone_id=130), _failed_job(130, "u")]
    assert _count_build_failures(jobs) == (2, 1)


def test_build_failures_counts_the_finished_retry_of_the_job_as_the_job() -> None:
    jobs = [_failed_job(123, "t", clone_id=124), _failed_job(124, "t"), _failed_job(125, "t", machine="64bit")]
    assert _count_build_failures(jobs) == (2, 1)


def test_build_failures_does_not_count_an_incomplete_investigated_job() -> None:
    jobs = [_failed_job(123, "t") | {"result": "incomplete"}, _failed_job(124, "u")]
    assert _count_build_failures(jobs) == (1, 1)


def test_build_failures_counts_the_failed_job_whose_retry_passed() -> None:
    passed = {"id": 130, "state": "done", "result": "passed"}
    jobs = [_failed_job(123, "t", clone_id=130), passed, _failed_job(124, "u")]
    assert _count_build_failures(jobs) == (2, 1)


def test_build_failures_leaves_out_a_group_less_clone_of_the_job() -> None:
    jobs = [_failed_job(123, "t"), _failed_job(140, "t", group_id=None), _failed_job(125, "t", machine="64bit")]
    assert _count_build_failures(jobs) == (2, 1)


def test_investigate_job_not_found_in_history_appends_note_and_logs_info(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    mock_post = mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    setup_mock_client(
        mocker,
        overrides={
            "test=": {
                "jobs": [
                    {"id": 120, "state": "done", "result": "failed"},
                    {"id": 121, "state": "done", "result": "failed"},
                ]
            }
        },
    )
    llm_investigate.investigate("123", history_limit=50)

    mock_log.warning.assert_not_called()
    mock_log.info.assert_any_call(
        "Job %s not found in recent history (limit %s). Streak count may be inaccurate.", "123", 50
    )

    mock_post.assert_called_once()
    comment = mock_post.call_args[0][2]
    assert "*Note: Job not found in recent history (limit 50). Streak count may be inaccurate.*" in comment


def test_investigate_job_not_found_in_history_informs_llm_prompt(mocker: MockerFixture) -> None:
    mock_print = mocker.patch("llm_investigate.typer.echo")
    setup_mock_client(
        mocker,
        overrides={
            "test=": {
                "jobs": [
                    {"id": 120, "state": "done", "result": "failed"},
                    {"id": 121, "state": "done", "result": "failed"},
                ]
            }
        },
    )
    llm_investigate.investigate("123", history_limit=20, print_prompt=True)
    output = mock_print.call_args[0][0]
    assert (
        "- Consecutive recent failures for this specific test (same name, arch, flavor, machine, group): 2 "
        "(streak count may be inaccurate: job not found in recent history, limit 20)"
    ) in output


def test_investigate_dry_run_includes_streak_note_when_job_not_found(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(
        mocker,
        overrides={
            "test=": {
                "jobs": [
                    {"id": 120, "state": "done", "result": "failed"},
                ]
            }
        },
    )
    client.post.side_effect = lambda *_, **__: Mock(
        json=Mock(return_value={"choices": [{"message": {"content": "BISECT: NO. Already known."}}]})
    )

    llm_investigate.investigate("123", dry=True, history_limit=15)

    mock_post.assert_not_called()
    printed = mock_print.call_args[0][0]
    assert "*Note: Job not found in recent history (limit 15). Streak count may be inaccurate.*" in printed
