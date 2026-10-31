# Copyright SUSE LLC
"""Tests for skipping jobs that openQA shows as reviewed."""

from __future__ import annotations

import json
import urllib.parse
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, Mock, call

import pytest

from tests.test_openqa_llm_investigate import llm_investigate, setup_mock_client

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_REVIEWED = {"bugs": [{"content": "boo#1288770"}], "comments": None, "label": None, "reviewed": 1}
_COMMENTED = {"bugs": [], "comments": 1, "label": None, "reviewed": None}
_URL = "https://openqa.opensuse.org/tests/123"
_SMALLEST_PAGE = "?previous_limit=0&next_limit=0"


def _page(current: Any, latest: Any = 0) -> dict[str, Any]:
    """Return openQA's next & previous results with the comment data of the current and the latest job."""
    return {
        "info": [],
        "data": [
            {"id": 124, "islatest": 1, "comment_data": latest},
            {"id": 123, "iscurrent": 1, "comment_data": current},
        ],
    }


def _ajax_urls(client: MagicMock) -> list[str]:
    """Return the 'Next & previous results' URLs the client fetched, in order."""
    return [c.args[0].removeprefix(_URL) for c in client.get.call_args_list if "/ajax" in c.args[0]]


def test_investigate_leaves_a_reviewed_job_to_the_standard_investigation(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides={"/ajax": _page(_REVIEWED)})

    llm_investigate.investigate("123")

    mock_print.assert_called_once_with(_URL)
    client.post.assert_not_called()
    client.stream.assert_not_called()
    mock_post.assert_not_called()
    fetched = [f"{c.args[0]}?{urllib.parse.urlencode(c.kwargs.get('params') or {})}" for c in client.get.call_args_list]
    assert not [url for url in fetched if "details_ajax" in url or "/api/v1/jobs?test=" in url]
    assert _ajax_urls(client) == ["/ajax"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"api/v1/jobs": {"job": {"id": 123, "result": "passed"}}}, id="passed"),
        pytest.param({"comments": [{"text": "**LLM Investigation summary:** done"}]}, id="llm-summary"),
    ],
)
def test_investigate_exits_before_the_review_check(mocker: MockerFixture, overrides: dict[str, Any]) -> None:
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides={"/ajax": _page(_REVIEWED), **overrides})

    with pytest.raises(SystemExit) as exc:
        llm_investigate.investigate("123")

    assert exc.value.code == 0
    mock_print.assert_not_called()
    assert not _ajax_urls(client)


@pytest.mark.parametrize(
    ("verdict", "printed"),
    [
        pytest.param("BISECT: YES \u2014 isolated", True, id="bisect-yes"),
        pytest.param("BISECT: NO \u2014 known issue", False, id="bisect-no"),
    ],
)
def test_investigate_keeps_the_bisect_gate_but_does_not_post_when_a_review_arrives_meanwhile(
    mocker: MockerFixture, verdict: str, *, printed: bool
) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker)
    pages = iter([_page(_COMMENTED), _page(_REVIEWED)])
    replies = {1: "* isosize: died", 3: "The module died.", 5: f"Classification: Product bug\n{verdict}"}
    serve = client.get.side_effect

    def get(url: str, *args: Any, **kwargs: Any) -> Any:
        return Mock(text=json.dumps(next(pages))) if "/ajax" in url else serve(url, *args, **kwargs)

    def post(*_args: Any, **kwargs: Any) -> Mock:
        reply = replies[len(kwargs["json"]["messages"])]
        return Mock(json=Mock(return_value={"choices": [{"message": {"content": reply}}]}))

    client.get.side_effect = get
    client.post.side_effect = post

    llm_investigate.investigate("123")

    assert client.post.call_count == 3
    assert mock_print.call_args_list == ([call(_URL)] if printed else [])
    mock_post.assert_not_called()
    assert _ajax_urls(client) == ["/ajax", f"/ajax{_SMALLEST_PAGE}"]


@pytest.mark.parametrize(
    ("option", "output"),
    [
        pytest.param("dry", "**LLM Investigation summary:**", id="dry"),
        pytest.param("print_prompt", "===== Prompt 1/3 =====", id="print-prompt"),
    ],
)
def test_investigate_dry_and_print_prompt_skip_the_review_check(
    mocker: MockerFixture, option: str, output: str
) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides={"/ajax": _page(_REVIEWED)})

    llm_investigate.investigate("123", **{option: True})

    assert any(output in c.args[0] for c in mock_print.call_args_list)
    assert _ajax_urls(client) == ["/ajax"]
    mock_post.assert_not_called()


def test_investigate_force_posts_on_a_reviewed_job(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides={"/ajax": _page(_REVIEWED)})

    llm_investigate.investigate("123", force=True)

    mock_post.assert_called_once()
    assert _ajax_urls(client) == ["/ajax"]
