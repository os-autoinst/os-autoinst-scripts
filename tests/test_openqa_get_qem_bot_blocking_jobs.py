# Copyright SUSE LLC
"""Unit tests for openqa-get-qem-bot-blocking-jobs."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import pathlib
import re
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, Mock

import httpx
import pytest
import typer

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# Load the script dynamically as a module
rootpath = pathlib.Path(__file__).parent.parent.resolve()
path = rootpath / "openqa-get-qem-bot-blocking-jobs"
spec = importlib.util.spec_from_file_location(
    "blocking_jobs",
    path,
    loader=importlib.machinery.SourceFileLoader("blocking_jobs", str(path)),
)
assert spec is not None
assert spec.loader is not None
blocking_jobs = importlib.util.module_from_spec(spec)
sys.modules["blocking_jobs"] = blocking_jobs
spec.loader.exec_module(blocking_jobs)


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = Mock(spec=httpx.Response)
    return client


def test_get_gitlab_access_token(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "gitlab_token"
    f.write_text("my-secret-token\n", encoding="utf-8")
    assert blocking_jobs.get_gitlab_access_token(str(f)) == "my-secret-token"

    with pytest.raises(typer.Exit) as exc:
        blocking_jobs.get_gitlab_access_token("non_existent_file")
    assert exc.value.exit_code == 1


def test_get_gitlab_jobs(mock_client: MagicMock) -> None:
    mock_client.get.return_value.json.return_value = [{"id": 1, "name": "approve submissions"}]

    result = blocking_jobs.get_gitlab_jobs(mock_client, "http://gitlab", 6096, {})
    assert result == [{"id": 1, "name": "approve submissions"}]
    mock_client.get.assert_called_once_with("http://gitlab/api/v4/projects/6096/jobs", headers={})


def test_get_gitlab_job_raw(mock_client: MagicMock) -> None:
    mock_client.get.return_value.text = "raw logs"

    result = blocking_jobs.get_gitlab_job_raw(mock_client, "http://gitlab", 6096, 1, {})
    assert result == "raw logs"
    mock_client.get.assert_called_once_with("http://gitlab/api/v4/projects/6096/jobs/1/trace", headers={})


@pytest.mark.parametrize(
    ("raw_content", "expected"),
    [
        (
            (
                "2026-06-17 INFO Found not-ok, not-ignored job https://o.de/t22778588 for submission git:5254\n"
                "2026-06-17 INFO Found not-ok, not-ignored job https://o.de/t22871672 for submission smelt:44345\n"
                "2026-06-17 INFO Found not-ok, not-ignored job https://o.de/t22871672 for submission smelt:44495"
            ),
            ["22778588", "22871672"],
        ),
        ("2026-06-17 INFO Ignoring not-ok job https://openqa.suse.de/t123 (manually marked)", []),
        ("", []),
    ],
)
def test_extract_openqa_job_ids(raw_content: str, expected: list[str]) -> None:
    assert blocking_jobs.extract_openqa_job_ids(raw_content) == expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"git:", ["22778588"]),
        (r"smelt:44345", ["22871672"]),
        (r"non_existent", []),
    ],
    ids=[
        "match_all_git_submissions",
        "match_specific_smelt_submission",
        "no_matching_submission_pattern",
    ],
)
def test_extract_openqa_job_ids_with_submission_regex(pattern: str, expected: list[str]) -> None:
    raw_content = (
        "2026-06-17 INFO Found not-ok, not-ignored job https://o.de/t22778588 for submission git:5254\n"
        "2026-06-17 INFO Found not-ok, not-ignored job https://o.de/t22871672 for submission smelt:44345\n"
        "2026-06-17 INFO Found not-ok, not-ignored job https://o.de/t22871672 for submission smelt:44495"
    )
    regex = re.compile(pattern)
    assert blocking_jobs.extract_openqa_job_ids(raw_content, regex) == expected


def test_get_openqa_job_info(mock_client: MagicMock) -> None:
    mock_client.get.return_value.json.return_value = {"job": {"id": 12345, "group": "Container"}}

    result = blocking_jobs.get_openqa_job_info(mock_client, "http://openqa", "12345")
    assert result == {"job": {"id": 12345, "group": "Container"}}
    mock_client.get.assert_called_once_with("http://openqa/api/v1/jobs/12345")


@pytest.mark.parametrize(
    ("job_info", "regex_str", "should_print"),
    [
        ({"id": 12345, "group": "Container", "parent_group": "SLE"}, r"Container", True),
        ({"id": 12345, "group": "Other", "parent_group": "SLE"}, r"Container", False),
    ],
)
def test_process_openqa_job(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    mock_client: MagicMock,
    job_info: dict[str, Any],
    regex_str: str,
    *,
    should_print: bool,
) -> None:
    mocker.patch("blocking_jobs.get_openqa_job_info", return_value={"job": job_info})

    compiled_regex = re.compile(regex_str)

    blocking_jobs.process_openqa_job(mock_client, "http://openqa", "12345", compiled_regex)
    captured = capsys.readouterr().out
    expected_output = f"http://openqa/t{job_info.get('id')}: {job_info.get('group')} / {job_info.get('parent_group')}\n"
    if should_print:
        assert captured == expected_output
    else:
        assert not captured


def test_find_blocking_jobs_success(mocker: MockerFixture, mock_client: MagicMock) -> None:
    mock_get_jobs = mocker.patch(
        "blocking_jobs.get_gitlab_jobs",
        return_value=[{"id": 1, "name": "approve submissions"}],
    )
    mock_get_raw = mocker.patch("blocking_jobs.get_gitlab_job_raw", return_value="logs")
    mock_extract = mocker.patch("blocking_jobs.extract_openqa_job_ids", return_value=["12345"])
    mock_process = mocker.patch("blocking_jobs.process_openqa_job")

    blocking_jobs.find_blocking_jobs(
        mock_client,
        6096,
        "approve submissions",
        "http://gitlab",
        "http://openqa",
        "Container",
        {},
    )

    mock_get_jobs.assert_called_once()
    mock_get_raw.assert_called_once_with(mock_client, "http://gitlab", 6096, 1, {})
    mock_extract.assert_called_once_with("logs", None)
    mock_process.assert_called_once()


def test_find_blocking_jobs_with_submission(mocker: MockerFixture, mock_client: MagicMock) -> None:
    mock_get_jobs = mocker.patch(
        "blocking_jobs.get_gitlab_jobs",
        return_value=[{"id": 1, "name": "approve submissions"}],
    )
    mock_get_raw = mocker.patch("blocking_jobs.get_gitlab_job_raw", return_value="logs")
    mock_extract = mocker.patch("blocking_jobs.extract_openqa_job_ids", return_value=["12345"])
    mock_process = mocker.patch("blocking_jobs.process_openqa_job")

    blocking_jobs.find_blocking_jobs(
        mock_client, 6096, "approve submissions", "http://gitlab", "http://openqa", "Container", {}, "git:5254"
    )

    mock_get_jobs.assert_called_once()
    mock_get_raw.assert_called_once_with(mock_client, "http://gitlab", 6096, 1, {})
    mock_extract.assert_called_once()
    called_args = mock_extract.call_args[0]
    assert called_args[0] == "logs"
    assert called_args[1].pattern == "git:5254"
    mock_process.assert_called_once()


def test_find_blocking_jobs_not_found(mocker: MockerFixture, mock_client: MagicMock) -> None:
    mocker.patch("blocking_jobs.get_gitlab_jobs", return_value=[])

    with pytest.raises(typer.Exit) as exc:
        blocking_jobs.find_blocking_jobs(
            mock_client,
            6096,
            "approve submissions",
            "http://gitlab",
            "http://openqa",
            "Container",
            {},
        )
    assert exc.value.exit_code == 1


@pytest.mark.parametrize(
    ("side_effect", "expected_exception"),
    [
        (None, None),
        (httpx.HTTPStatusError("error", request=Mock(), response=Mock()), httpx.HTTPStatusError),
        (ValueError("error"), ValueError),
    ],
)
def test_main_flow(mocker: MockerFixture, side_effect: Any, expected_exception: Any) -> None:
    mocker.patch("blocking_jobs.get_gitlab_access_token", return_value="token")
    mock_find = mocker.patch("blocking_jobs.find_blocking_jobs")
    if side_effect:
        mock_find.side_effect = side_effect

    if expected_exception:
        with pytest.raises(expected_exception):
            blocking_jobs.main(gitlab_access_token_file="foo")
    else:
        blocking_jobs.main(gitlab_access_token_file="foo")
        mock_find.assert_called_once()


@pytest.mark.parametrize(
    ("verbose", "expected_level"),
    [
        (0, logging.WARNING),
        (1, logging.INFO),
        (2, logging.DEBUG),
        (3, logging.DEBUG),
    ],
)
def test_setup_logging(verbose: int, expected_level: int) -> None:
    blocking_jobs.setup_logging(verbose)
    assert logging.getLogger().getEffectiveLevel() == expected_level
