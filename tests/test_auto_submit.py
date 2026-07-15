# Copyright SUSE LLC
# ruff: noqa: S404, FBT001, FBT003, PLC1901, F841
"""Unit tests for os-autoinst-obs-auto-submit."""

from __future__ import annotations

import datetime
import importlib.machinery
import importlib.util
import pathlib
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
    res = auto_submit.get_obs_sr_id("openSUSE:Factory", "proj", "pkg", "osc", False)
    assert res == "42"


def test_get_obs_sr_id_empty(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="<collection></collection>")
    res = auto_submit.get_obs_sr_id("openSUSE:Factory", "proj", "pkg", "osc", False)
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
        ["osc", "sr", "-s", "23", "-m", "Update to 3.14", "Factory"], dry_run=False, mutating=True
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
    mock_run.assert_called_once_with(["osc", "sr", "-m", "Update to 3.14", "Factory"], dry_run=False, mutating=True)


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


def test_last_revision(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(
        ["osc"], 0, stdout="* Update to version 162312.c0f8ee6a233ed250dbc54c19dee50118:\n"
    )
    res = auto_submit.last_revision("proj", "pkg", "Factory", "osc")
    assert res == "c0f8ee6a233ed250dbc54c19dee50118"


def test_last_revision_none(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="")
    res = auto_submit.last_revision("proj", "pkg", "Factory", "osc")
    assert res == ""
