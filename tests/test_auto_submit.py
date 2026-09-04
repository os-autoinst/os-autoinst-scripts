# Copyright SUSE LLC
# ruff: file-ignore[suspicious-subprocess-import, boolean-type-hint-positional-argument, compare-to-empty-string, unused-variable]
"""Unit tests for os-autoinst-obs-auto-submit."""

from __future__ import annotations

import datetime
import importlib.machinery
import importlib.util
import logging
import pathlib
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# Load the script dynamically as a module
rootpath = pathlib.Path(__file__).parent.parent.resolve()
path = rootpath / "os-autoinst-obs-auto-submit"
spec = importlib.util.spec_from_file_location(
    "auto_submit",
    path,
    loader=importlib.machinery.SourceFileLoader("auto_submit", str(path)),
)
assert spec is not None
assert spec.loader is not None
auto_submit = importlib.util.module_from_spec(spec)
sys.modules["auto_submit"] = auto_submit
spec.loader.exec_module(auto_submit)


def test_is_transient_osc_error() -> None:
    exc = subprocess.CalledProcessError(1, "osc", stderr="HTTP Error 503: Service Unavailable")
    assert auto_submit.is_transient_osc_error(exc) is True

    exc_404 = subprocess.CalledProcessError(1, "osc", stderr="HTTP Error 404: Not Found")
    assert auto_submit.is_transient_osc_error(exc_404) is False


def test_get_obs_sr_id(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(
        ["osc"], 0, stdout='<collection><request id="42"/></collection>'
    )
    res = auto_submit.get_obs_sr_id("openSUSE:Factory", "proj", "pkg", "osc", dry_run=False)
    assert res == "42"


def test_get_obs_sr_id_empty(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="<collection></collection>")
    res = auto_submit.get_obs_sr_id("openSUSE:Factory", "proj", "pkg", "osc", dry_run=False)
    assert res == ""


@pytest.mark.parametrize(
    ("target", "days", "pr_json", "sr_stdout", "expected"),
    [
        ("openSUSE:Factory", 0, None, "", True),
        ("openSUSE:Factory", 1, None, "openSUSE:Factory", False),
        ("openSUSE:Factory", 1, None, "different_target", True),
        ("openSUSE:Leap:16.0", 1, [], "", True),
        (
            "openSUSE:Leap:16.0",
            3,
            [
                {
                    "updated_at": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "html_url": "https://foo/bar",
                    "user": {"login": "os-autoinst-obs-workflow"},
                    "base": {"ref": "leap-16.0"},
                }
            ],
            "",
            False,
        ),
        (
            "openSUSE:Leap:16.0",
            1,
            [
                {
                    "updated_at": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "html_url": "https://foo/bar",
                    "user": {"login": "os-autoinst-obs-workflow"},
                    "base": {"ref": "leap-16.0"},
                }
            ],
            "",
            True,
        ),
    ],
)
def test_has_pending_submission(
    mocker: MockerFixture,
    target: str,
    days: int,
    pr_json: list[dict[str, Any]] | None,
    sr_stdout: str,
    expected: bool,
) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    if pr_json is not None:
        mock_run.return_value = subprocess.CompletedProcess(["git-obs"], 0, stdout=auto_submit.json.dumps(pr_json))
    else:
        mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout=sr_stdout)

    submitter = auto_submit.AutoSubmitter(
        dst_project="proj",
        throttle_days=days,
        throttle_days_leap_16=days,
        git_user="os-autoinst-obs-workflow",
        osc_cmd_str="osc",
        git_obs_cmd_str="git-obs",
        dry_run=False,
    )
    res = submitter.has_pending_submission(
        package="openQA",
        target=target,
    )
    assert res is expected


def test_prepare_local_clone_fetches_before_switch(mocker: MockerFixture) -> None:
    """Fetch parent before creating the branch so a fork lacking it still works."""
    mock_run = mocker.patch("auto_submit.subprocess.run")
    mocker.patch("auto_submit.pathlib.Path.iterdir", return_value=[])
    submitter = auto_submit.AutoSubmitter(dst_project="dst", git_cmd_str="git", dry_run=False)
    submitter._prepare_local_clone("openQA", "leap-16.0")  # ruff: ignore[private-member-access]
    git_calls = [call.args[0] for call in mock_run.call_args_list]
    assert git_calls[0] == ["git", "fetch", "parent"]
    assert git_calls[1] == ["git", "switch", "-C", "leap-16.0", "parent/leap-16.0"]


def test_prepare_local_clone_dry_run_logs_fetch_first(caplog: pytest.LogCaptureFixture) -> None:
    submitter = auto_submit.AutoSubmitter(dst_project="dst", git_cmd_str="git", dry_run=True)
    with caplog.at_level("INFO"):
        submitter._prepare_local_clone("openQA", "leap-16.0")  # ruff: ignore[private-member-access]
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == "[dry-run] Would execute: git fetch parent"
    assert messages[1] == "[dry-run] Would execute: git switch -C leap-16.0 parent/leap-16.0"


def test_make_obs_submit_request_success(mocker: MockerFixture) -> None:
    mocker.patch("auto_submit.get_obs_sr_id", return_value="23")
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
    )
    res = submitter.make_obs_submit_request("pkg", "Factory", "3.14")
    assert res is True
    mock_run.assert_called_once_with(
        ["osc", "sr", "-s", "23", "-m", "Update to 3.14", "dst", "pkg", "Factory"], dry_run=False, mutating=True
    )


def test_make_obs_submit_request_new(mocker: MockerFixture) -> None:
    mocker.patch("auto_submit.get_obs_sr_id", return_value="")
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
    )
    res = submitter.make_obs_submit_request("pkg", "Factory", "3.14")
    assert res is True
    mock_run.assert_called_once_with(
        ["osc", "sr", "-m", "Update to 3.14", "dst", "pkg", "Factory"], dry_run=False, mutating=True
    )


def test_make_obs_submit_request_failure(mocker: MockerFixture) -> None:
    mocker.patch("auto_submit.get_obs_sr_id", return_value="")
    mock_run = mocker.patch("auto_submit.run_osc_cmd", side_effect=subprocess.CalledProcessError(1, "sr"))
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
    )
    res = submitter.make_obs_submit_request("pkg", "Factory", "3.14")
    assert res is False


def test_last_revision(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    sha = 'c0f8ee6a233ed250dbc54c19dee50118'
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(
        ["osc"], 0, stdout=f"* Update to version 162312.{sha}:\n  * fix: foo\n  * feat: bar\n  * perf: boo\n"
    )
    res = auto_submit.last_revision("proj", "pkg", "Factory", "osc")
    assert res == sha
    assert re.search(r"First 4 lines of 'proj/pkg/_service:obs_scm:pkg.changes'", caplog.records[0].getMessage()) is not None

    assert caplog.records[1].getMessage() == f"Last revision for 'proj/pkg': {sha}"


def test_last_revision_none(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="")
    res = auto_submit.last_revision("proj", "pkg", "Factory", "osc")
    assert res == ""


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", "unknown"),
        ("   \n  ", "unknown"),
        ("Package pkg is not yet ready for release\nscheduled", "Package pkg is not yet ready for release\nscheduled"),
        ('{"failed_jobs": [123, 456]}', "failed openQA jobs: 123, 456"),
        ('{"failed_jobs": []}', '{"failed_jobs": []}'),
        ('{"other": 1}', '{"other": 1}'),
        ("not json { at all", "not json { at all"),
    ],
)
def test_format_skip_reason(content: str, expected: str) -> None:
    assert auto_submit._format_skip_reason(content) == expected  # ruff: ignore[private-member-access]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("single line", "Skipping submission, reason: single line"),
        ("note\npkg1\npkg2", "Skipping submission, reason:\n  note\n  pkg1\n  pkg2"),
    ],
)
def test_log_skip_reason(reason: str, expected: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        auto_submit._log_skip_reason(reason)  # ruff: ignore[private-member-access]
    assert caplog.records[0].getMessage() == expected


def test_get_packages_to_submit_env(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {"PACKAGES": "pkg1 pkg2"})
    res = auto_submit._get_packages_to_submit("dst", "osc", dry_run=False)  # ruff: ignore[private-member-access]
    assert res == ["pkg1", "pkg2"]


def test_get_packages_to_submit_osc(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {}, clear=True)
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="pkg1\npkg2-test\npkg3\n")
    res = auto_submit._get_packages_to_submit("dst", "osc", dry_run=True)  # ruff: ignore[private-member-access]
    assert res == ["pkg1", "pkg3"]
    mock_run.assert_called_once_with(["osc", "ls", "dst"], dry_run=True, mutating=False)


@pytest.mark.parametrize(
    ("verbose", "quiet", "expected_level"),
    [
        (0, 0, logging.INFO),
        (1, 0, logging.DEBUG),
        (0, 1, logging.WARNING),
        (0, 2, logging.ERROR),
        (0, 3, logging.CRITICAL),
    ],
)
def test_main_logging(mocker: MockerFixture, verbose: int, quiet: int, expected_level: int) -> None:
    mocker.patch("auto_submit._run_submissions")
    mock_basic_config = mocker.patch("logging.basicConfig")
    auto_submit.main(verbose=verbose, quiet=quiet)
    mock_basic_config.assert_called_with(level=expected_level, format="%(levelname)s: %(message)s", force=True)


def test_run_submissions_force(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_exists = mocker.patch("auto_submit.pathlib.Path.exists", return_value=True)
    mock_unlink = mocker.patch("auto_submit.pathlib.Path.unlink")
    mocker.patch("auto_submit._get_packages_to_submit", return_value=["pkg1"])
    mocker.patch("auto_submit.AutoSubmitter")

    auto_submit._run_submissions(  # ruff: ignore[private-member-access]
        src_project="devel:openQA",
        dst_project="devel:openQA:tested",
        staging_project="devel:openQA:testing",
        submit_target="openSUSE:Factory",
        dry_run=False,
        force=True,
        osc_poll_interval=2,
        osc_build_start_poll_tries=90,
        throttle_days=2,
        throttle_days_leap_16=7,
        git_user="user",
        submit_target_extra="none",
    )

    mock_unlink.assert_called_once()
    mock_run.assert_any_call(["cleanup-obs-project", "devel:openQA:testing", "I am sure"], capture_output=False, text=False, check=True)


def test_run_osc_cmd_error_logging(caplog: pytest.LogCaptureFixture, mocker: MockerFixture) -> None:
    mock_run = mocker.patch(
        "auto_submit.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["osc", "sr"], output="some stdout", stderr="some stderr"),
    )

    with caplog.at_level("ERROR"), pytest.raises(subprocess.CalledProcessError):
        auto_submit.run_osc_cmd(["osc", "sr"], dry_run=False, mutating=True)

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "Command 'osc sr' failed with exit code 1" in messages[0]
    assert "Command stdout:\nsome stdout" in messages[0]
    assert "Command stderr:\nsome stderr" in messages[0]


def test_update_package(caplog: pytest.LogCaptureFixture, mocker: MockerFixture, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    mocker.patch("auto_submit.AutoSubmitter._disable_service_buildtime", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._cleanup_and_rename_files", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._find_version", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._osc_addremove_and_filter_specs", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._commit_local_changes", return_value=True)
    content = "Line 1\nLine 2"
    filename = "pkg.changes"
    changes_file = tmp_path / filename
    changes_file.write_text(content, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
    )
    caplog.set_level(logging.INFO)
    res = submitter.update_package("pkg")
    assert caplog.records[0].getMessage() == "update_package pkg"
    assert caplog.records[1].getMessage() == f"First 2 lines of '{filename}':\n{content}"
    assert res is True
