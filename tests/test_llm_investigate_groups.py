# Copyright SUSE LLC
"""Tests for skipping jobs in groups that openqa-investigate excludes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.test_openqa_llm_investigate import llm_investigate, setup_mock_client

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_URL = "https://openqa.opensuse.org/tests/123"
_DEV_GROUP = {"parent_group": "Development", "group": "Development Tumbleweed"}


def _job(**group: str) -> dict[str, Any]:
    """Return the overrides for a failed job in the given group."""
    job = {"id": 123, "result": "failed", "test": "my_test", "testresults": [{"name": "isosize", "result": "failed"}]}
    return {"api/v1/jobs": {"job": job | group}}


def test_investigate_leaves_an_excluded_group_to_the_standard_investigation(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides=_job(**_DEV_GROUP))

    llm_investigate.investigate("123")

    mock_print.assert_called_once_with(_URL)
    client.post.assert_not_called()
    mock_post.assert_not_called()
    fetched = [c.args[0].removeprefix("https://openqa.opensuse.org") for c in client.get.call_args_list]
    assert fetched == ["/api/v1/jobs/123/comments", "/api/v1/jobs/123/details"]


@pytest.mark.parametrize(
    "group",
    [
        pytest.param({}, id="no-group"),
        pytest.param({"group": "Development Tumbleweed"}, id="no-parent-group"),
        pytest.param({"parent_group": "openSUSE Tumbleweed", "group": "openSUSE Tumbleweed"}, id="other-parent"),
        pytest.param({"parent_group": "Development", "group": "GitHub"}, id="pr-verification-runs"),
    ],
)
def test_investigate_asks_the_llm_for_other_groups(mocker: MockerFixture, group: dict[str, str]) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides=_job(**group))

    llm_investigate.investigate("123")

    assert client.post.call_count == 3
    mock_print.assert_called_once_with(_URL)
    mock_post.assert_called_once()


@pytest.mark.parametrize(
    ("option", "llm_calls"),
    [
        pytest.param("force", 3, id="force"),
        pytest.param("dry", 3, id="dry"),
        pytest.param("print_prompt", 0, id="print-prompt"),
    ],
)
def test_investigate_manual_runs_ignore_the_group(mocker: MockerFixture, option: str, llm_calls: int) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides=_job(**_DEV_GROUP))

    llm_investigate.investigate("123", **{option: True})

    assert any("details_ajax" in c.args[0] for c in client.get.call_args_list)
    assert client.post.call_count == llm_calls


def _opted_out_job() -> dict[str, Any]:
    """Return the overrides for a failed job in an excluded group that also sets NO_LLM_INVESTIGATION=1."""
    overrides = _job(**_DEV_GROUP)
    overrides["api/v1/jobs"]["job"]["settings"] = {"NO_LLM_INVESTIGATION": "1"}
    return overrides


@pytest.mark.parametrize("option", ["dry", "print_prompt"])
def test_investigate_manual_runs_keep_the_opt_out_by_setting(mocker: MockerFixture, option: str) -> None:
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides=_opted_out_job())

    llm_investigate.investigate("123", **{option: True})

    mock_print.assert_called_once_with(_URL)
    assert not any("details_ajax" in c.args[0] for c in client.get.call_args_list)
    client.post.assert_not_called()


def test_investigate_names_the_setting_when_the_group_is_excluded_too(mocker: MockerFixture) -> None:
    mock_log = mocker.patch("llm_investigate.log")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides=_opted_out_job())

    llm_investigate.investigate("123")

    mock_print.assert_called_once_with(_URL)
    client.post.assert_not_called()
    mock_log.info.assert_called_once()
    assert "NO_LLM_INVESTIGATION=1" in mock_log.info.call_args[0][0]


_DEFAULT = llm_investigate.DEFAULT_EXCLUDE_GROUP_REGEX


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({}, _DEFAULT, id="unset"),
        pytest.param({"llm_exclude_group_regex": ""}, _DEFAULT, id="empty-keeps-default"),
        pytest.param({"llm_exclude_group_regex": "^Nothing"}, "^Nothing", id="set"),
        pytest.param({"exclude_group_regex": "^Nothing"}, _DEFAULT, id="not-the-variable-of-openqa-investigate"),
    ],
)
def test_investigate_reads_its_own_variable(mocker: MockerFixture, env: dict[str, str], expected: str) -> None:
    mock_perform = mocker.patch("llm_investigate._perform_investigation")

    result = CliRunner().invoke(
        llm_investigate.app, ["123"], env={"llm_exclude_group_regex": None, "exclude_group_regex": None} | env
    )

    assert result.exit_code == 0, result.output
    assert mock_perform.call_args[0][3].exclude_group_regex == expected


@pytest.mark.parametrize(
    ("regex", "group", "expected"),
    [
        pytest.param("^Development / Development Tumbleweed$", _DEV_GROUP, True, id="parent-slash-group"),
        pytest.param("^ / Development Tumbleweed$", {"group": "Development Tumbleweed"}, True, id="no-parent"),
        pytest.param("^Nothing", _DEV_GROUP, False, id="no-match"),
        pytest.param(
            _DEFAULT,
            {"parent_group": "Desktop Environments - Development", "group": "openSUSE Argon"},
            True,
            id="default-mid-string",
        ),
        pytest.param(_DEFAULT, {"parent_group": "Development", "group": "GitHub"}, False, id="default-keeps-github"),
    ],
)
def test_group_is_formatted_like_in_openqa_investigate(regex: str, group: dict[str, str], *, expected: bool) -> None:
    assert llm_investigate._in_excluded_group("123", group, regex) is expected
