# Copyright SUSE LLC
# ruff: noqa: S404, FBT001
"""Unit tests for monitor-openqa_job."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import pathlib
import subprocess
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
path = rootpath / "monitor-openqa_job"
spec = importlib.util.spec_from_file_location(
    "monitor_job",
    path,
    loader=importlib.machinery.SourceFileLoader("monitor_job", str(path)),
)
assert spec is not None
assert spec.loader is not None
monitor_job = importlib.util.module_from_spec(spec)
sys.modules["monitor_job"] = monitor_job
spec.loader.exec_module(monitor_job)


def test_logging(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        monitor_job.log.info("info")
        monitor_job.log.warning("warn")
        monitor_job.log.error("err")
        monitor_job.log.debug("dbg")
    for log_msg in ["info", "warn", "err", "dbg"]:
        assert log_msg in caplog.text


@pytest.mark.parametrize(
    ("content", "exists", "expected"),
    [
        ('{"ids": [100]}', True, [100]),
        (None, False, typer.Exit),
        ("bad json", True, typer.Exit),
    ],
)
def test_load_job_ids(tmp_path: pathlib.Path, content: str | None, exists: bool, expected: Any) -> None:
    f = tmp_path / "job_post_response"
    if exists and content is not None:
        f.write_text(content)
    if expected is typer.Exit:
        with pytest.raises(typer.Exit) as exc:
            monitor_job.load_job_ids(str(f) if exists else "missing")
        assert exc.value.exit_code == 2
    else:
        assert monitor_job.load_job_ids(str(f)) == expected


def test_fetch_job_api() -> None:
    client, resp = MagicMock(spec=httpx.Client), Mock()
    resp.json.return_value = {"key": "val"}
    client.get.return_value = resp
    assert monitor_job.fetch_job_api(client, "http://host", {}, 2) == {"key": "val"}


@pytest.mark.parametrize(
    ("side_effects", "expected_calls"),
    [
        ([Mock(stdout="pkg1\npkg2-test\n"), Mock()], 2),
        (subprocess.CalledProcessError(1, "ls"), 1),
        ([Mock(stdout="pkg1\n"), subprocess.CalledProcessError(1, "del")], 2),
    ],
)
def test_delete_packages(mocker: MockerFixture, side_effects: Any, expected_calls: int) -> None:
    mock_run = mocker.patch("monitor_job.subprocess.run", side_effect=side_effects)
    monitor_job.delete_packages_from_obs_project("proj", "osc")
    assert mock_run.call_count == expected_calls


def test_extract_comment_ids() -> None:
    xml = '<comment id="12">test failed</comment>\n<comment id="34">ok</comment>'
    assert monitor_job.extract_comment_ids(xml) == ["12"]


@pytest.mark.parametrize(
    ("side_effects", "expected_calls"),
    [
        ([Mock(stdout='<comment id="9">test failed</comment>'), Mock()], 2),
        (subprocess.CalledProcessError(1, "api"), 1),
        ([Mock(stdout='<comment id="9">test failed</comment>'), subprocess.CalledProcessError(1, "del")], 2),
    ],
)
def test_delete_old_comments(mocker: MockerFixture, side_effects: Any, expected_calls: int) -> None:
    mock_run = mocker.patch("monitor_job.subprocess.run", side_effect=side_effects)
    monitor_job.delete_old_comments("osc", "pkg", "name")
    assert mock_run.call_count == expected_calls


@pytest.mark.parametrize("fails", [False, True])
def test_post_comment(mocker: MockerFixture, fails: bool) -> None:
    mock_run = mocker.patch("monitor_job.subprocess.run")
    if fails:
        mock_run.side_effect = subprocess.CalledProcessError(1, "post")
    monitor_job.post_comment("osc", "pkg", "name", "comment")
    mock_run.assert_called_once()


@pytest.mark.parametrize(
    ("side_effects", "expected_id", "expected_res", "expected_ver", "should_raise"),
    [
        ([{"job": {"id": 1, "state": "done", "result": "passed"}}], 1, "passed", "", False),
        (
            [
                {"job": {"id": 2, "state": "running"}},
                {"job": {"id": 2, "state": "done", "result": "failed", "settings": {"VERSION": "1"}}},
            ],
            2,
            "failed",
            "1",
            False,
        ),
        (Exception("error"), 0, "", "", True),
    ],
)
def test_monitor_single_job(
    mocker: MockerFixture,
    side_effects: Any,
    expected_id: int,
    expected_res: str,
    expected_ver: str,
    should_raise: bool,
) -> None:
    mocker.patch("time.sleep")
    mock_fetch = mocker.patch("monitor_job.fetch_job_api", side_effect=side_effects)
    client = MagicMock()
    if should_raise:
        with pytest.raises(typer.Exit) as exc:
            monitor_job.monitor_single_job(client, 1, "http://host", 2, 0)
        assert exc.value.exit_code == 1
    else:
        fid, res, ver = monitor_job.monitor_single_job(client, 1, "http://host", 2, 0)
        assert (fid, res, ver) == (expected_id, expected_res, expected_ver)
        assert mock_fetch.call_count == len(side_effects)


@pytest.mark.parametrize(
    ("job_res", "pkg_name", "comment_obs", "env_patch", "expected_exit"),
    [
        ((1, "passed", ""), "", "", {}, 0),
        ((1, "failed", "15"), "", "", {}, 1),
        ((1, "failed", "15"), "pkg", "1", {}, 1),
        ((1, "passed", ""), "", "", {"OPENQA_CLI_RETRIES": "invalid"}, 0),
        ((1, "passed", ""), "", "", {"PREFIX": "pre", "OSC": ""}, 0),
        ((1, "passed", ""), "", "", {"OSC": "custom"}, 0),
    ],
)
def test_main_flow(
    mocker: MockerFixture,
    job_res: tuple[int, str, str],
    pkg_name: str,
    comment_obs: str,
    env_patch: dict[str, str],
    expected_exit: int,
) -> None:
    mocker.patch("monitor_job.load_job_ids", return_value=[1])
    mocker.patch("monitor_job.monitor_single_job", return_value=job_res)
    mocker.patch("monitor_job.delete_packages_from_obs_project")
    mocker.patch("monitor_job.delete_old_comments")
    mocker.patch("monitor_job.post_comment")
    mocker.patch("monitor_job.httpx.Client")
    if env_patch:
        mocker.patch.dict("os.environ", env_patch)

    with pytest.raises(typer.Exit) as exc:
        monitor_job.main(obs_package_name=pkg_name, comment_on_obs=comment_obs)
    assert exc.value.exit_code == expected_exit


@pytest.mark.parametrize(
    ("pkg", "comment_obs", "expected_exit"),
    [
        ("pkg", "", 1),
        ("pkg", "1", 1),
    ],
)
def test_comment_on_failed_jobs(mocker: MockerFixture, pkg: str, comment_obs: str, expected_exit: int) -> None:
    mock_del = mocker.patch("monitor_job.delete_old_comments")
    mock_post = mocker.patch("monitor_job.post_comment")
    with pytest.raises(typer.Exit) as exc:
        monitor_job.comment_on_failed_jobs("osc", "package", pkg, comment_obs, [1], {"1": 1}, "24", "http://host")
    assert exc.value.exit_code == expected_exit
    if comment_obs:
        mock_del.assert_called_once()
        mock_post.assert_called_once()


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Server returned an error: HTTP Error 503: Service Unavailable", True),
        ("Server returned an error: HTTP Error 500: Internal Server Error", True),
        ("Server returned an error: HTTP Error 404: Not Found", False),
        ("", False),
    ],
)
def test_is_transient_osc_error(stderr: str, expected: bool) -> None:
    exc = subprocess.CalledProcessError(1, "osc", stderr=stderr)
    assert monitor_job.is_transient_osc_error(exc) is expected


def test_is_transient_osc_error_non_called_process_error() -> None:
    assert monitor_job.is_transient_osc_error(ValueError("some error")) is False


def test_run_osc_cmd_success(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("monitor_job.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="success")
    res = monitor_job.run_osc_cmd(["osc"])
    assert res.stdout == "success"
    mock_run.assert_called_once_with(["osc"], check=True, capture_output=True, text=True)


def test_run_osc_cmd_retry_then_success(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")
    mock_run = mocker.patch("monitor_job.subprocess.run")
    err_503 = subprocess.CalledProcessError(1, "osc", stderr="HTTP Error 503: Service Unavailable")
    success = subprocess.CompletedProcess(["osc"], 0, stdout="done")
    mock_run.side_effect = [err_503, success]

    res = monitor_job.run_osc_cmd(["osc"])
    assert res.stdout == "done"
    assert mock_run.call_count == 2


def test_run_osc_cmd_non_transient_fails_immediately(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")
    mock_run = mocker.patch("monitor_job.subprocess.run")
    err_404 = subprocess.CalledProcessError(1, "osc", stderr="HTTP Error 404: Not Found")
    mock_run.side_effect = err_404

    with pytest.raises(subprocess.CalledProcessError):
        monitor_job.run_osc_cmd(["osc"])
    assert mock_run.call_count == 1
