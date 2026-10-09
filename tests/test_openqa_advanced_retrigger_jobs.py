# Copyright SUSE LLC
"""Unit tests for openqa-advanced-retrigger-jobs."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import pathlib
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

MODULE_NAME = "openqa_advanced_retrigger_jobs"
rootpath = pathlib.Path(__file__).parent.parent.resolve()
script_path = rootpath / "openqa-advanced-retrigger-jobs"
spec = importlib.util.spec_from_file_location(
    MODULE_NAME,
    script_path,
    loader=importlib.machinery.SourceFileLoader(MODULE_NAME, str(script_path)),
)
assert spec is not None
assert spec.loader is not None
retrigger = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = retrigger
spec.loader.exec_module(retrigger)

runner = CliRunner()

SQL_PREFIX = "select id from jobs where ("
SQL_TAIL = "result='incomplete' and clone_id is null and t_finished >= '2026-10-09');"
DEFAULT_QUERY = SQL_PREFIX + SQL_TAIL


def test_build_sql_command_default() -> None:
    query = retrigger.build_sql_command(None, None, "result='incomplete'", "2026-10-09", None)
    assert query == DEFAULT_QUERY


def test_build_sql_command_worker_filters() -> None:
    query = retrigger.build_sql_command("worker1", "42", "result='failed'", "2026-01-01", "build='1'")
    assert query.startswith("select id from jobs where (assigned_worker_id in (select id from workers where ")
    assert "host='worker1' and instance='42'" in query
    assert query.endswith(")) and result='failed' and clone_id is null and t_finished >= '2026-01-01' and build='1');")


@pytest.mark.parametrize(
    ("worker", "instance", "worker_clause"),
    [
        (None, None, ""),
        ("worker1", None, "assigned_worker_id in (select id from workers where (host='worker1')) and "),
        (
            "worker1",
            "7",
            "assigned_worker_id in (select id from workers where (host='worker1' and instance='7')) and ",
        ),
    ],
)
def test_build_sql_command_worker_clause(worker: str | None, instance: str | None, worker_clause: str) -> None:
    query = retrigger.build_sql_command(worker, instance, "result='incomplete'", "2026-10-09", None)
    assert query == SQL_PREFIX + worker_clause + SQL_TAIL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,2 3", [1, 2, 3]),
        (" 5 ", [5]),
        ("", []),
    ],
)
def test_parse_job_ids(raw: str, expected: list[int]) -> None:
    assert retrigger.parse_job_ids(raw) == expected


def test_parse_job_ids_invalid() -> None:
    with pytest.raises(ValueError, match="boom"):
        retrigger.parse_job_ids("1,boom")


def test_fetch_job_ids(mocker: MockerFixture) -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="11\n12\n", stderr="")
    mocked = mocker.patch("openqa_advanced_retrigger_jobs.subprocess.run", return_value=completed)
    assert retrigger.fetch_job_ids("openqa.opensuse.org", DEFAULT_QUERY) == [11, 12]
    argv = mocked.call_args.args[0]
    assert argv[:2] == ["ssh", "openqa.opensuse.org"]
    assert shlex.split(argv[2]) == [
        "sudo",
        "-u",
        "geekotest",
        "psql",
        "--no-align",
        "--tuples-only",
        "--command=" + DEFAULT_QUERY,
        "openqa",
    ]


def test_fetch_job_ids_failure(mocker: MockerFixture) -> None:
    mocker.patch(
        "openqa_advanced_retrigger_jobs.subprocess.run",
        side_effect=subprocess.CalledProcessError(2, "ssh"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        retrigger.fetch_job_ids("openqa.opensuse.org", "select id from jobs where (1);")


def test_restart_job_ids_batching(mocker: MockerFixture) -> None:
    client = mocker.MagicMock()
    retrigger.restart_job_ids(client, [1, 2, 3, 4], comment="why", max_jobs_per_request=3)
    assert client.openqa_request.call_count == 2
    assert client.method_calls[-1].kwargs["params"] == {"jobs": [4], "comment": "why"}


def test_restart_job_ids_without_comment(mocker: MockerFixture) -> None:
    client = mocker.MagicMock()
    retrigger.restart_job_ids(client, [1])
    assert client.method_calls[-1].kwargs["params"] == {"jobs": [1]}


def test_restart_job_ids_empty(mocker: MockerFixture) -> None:
    client = mocker.MagicMock()
    retrigger.restart_job_ids(client, [])
    assert client.openqa_request.call_count == 0


def test_restart_job_ids_dry_run(caplog: pytest.LogCaptureFixture) -> None:
    client = MagicMock()
    with caplog.at_level(logging.INFO):
        retrigger.restart_job_ids(client, [1, 2], comment="why", max_jobs_per_request=1, dry_run=True)
    assert client.openqa_request.call_count == 0
    assert "would POST jobs/restart with {'jobs': [1], 'comment': 'why'}" in caplog.text
    assert "would POST jobs/restart with {'jobs': [2], 'comment': 'why'}" in caplog.text


def test_main_dry_run(mocker: MockerFixture) -> None:
    client = mocker.MagicMock()
    mocker.patch("openqa_advanced_retrigger_jobs.OpenQA_Client", return_value=client)
    result = runner.invoke(retrigger.app, ["-v", "--dry-run", "--job-ids", "7,8", "--max-jobs-per-request", "1"])
    assert result.exit_code == 0
    assert "would POST jobs/restart with {'jobs': [7]}" in result.output
    assert "would POST jobs/restart with {'jobs': [8]}" in result.output
    assert client.openqa_request.call_count == 0


def test_main_retriggers_via_api(mocker: MockerFixture) -> None:
    client = mocker.MagicMock()
    mocker.patch("openqa_advanced_retrigger_jobs.OpenQA_Client", return_value=client)
    mocker.patch("openqa_advanced_retrigger_jobs.fetch_job_ids", return_value=[9, 10])
    result = runner.invoke(retrigger.app, ["--worker", "worker1", "--instance", "3", "--comment", "again"])
    assert result.exit_code == 0
    assert client.openqa_request.call_count == 1
    assert client.method_calls[-1].kwargs["params"] == {"jobs": [9, 10], "comment": "again"}


def test_main_query_failure(mocker: MockerFixture) -> None:
    mocker.patch("openqa_advanced_retrigger_jobs.fetch_job_ids", side_effect=subprocess.CalledProcessError(255, "ssh"))
    result = runner.invoke(retrigger.app, [])
    assert result.exit_code == 1
    assert "Failed to determine the job IDs to retrigger" in result.output


def test_main_invalid_job_ids() -> None:
    result = runner.invoke(retrigger.app, ["--job-ids", "1,boom"])
    assert result.exit_code == 1
    assert "Failed to determine the job IDs to retrigger" in result.output


def test_main_no_jobs(mocker: MockerFixture) -> None:
    mocker.patch("openqa_advanced_retrigger_jobs.fetch_job_ids", return_value=[])
    result = runner.invoke(retrigger.app, [])
    assert result.exit_code == 0
    assert "No jobs to retrigger" in result.output


def test_main_invalid_batch_size() -> None:
    result = runner.invoke(retrigger.app, ["--job-ids", "1", "--max-jobs-per-request", "0"])
    assert result.exit_code == 2
    assert "max-jobs-per-request must be at least 1" in result.output


def test_main_environment_defaults(mocker: MockerFixture) -> None:
    client = mocker.MagicMock()
    mocker.patch("openqa_advanced_retrigger_jobs.OpenQA_Client", return_value=client)
    result = runner.invoke(retrigger.app, ["-v"], env={"JOB_IDS": "7,8", "dry_run": "1", "comment": "env"})
    assert result.exit_code == 0
    assert client.openqa_request.call_count == 0
    assert "would POST jobs/restart with {'jobs': [7, 8], 'comment': 'env'}" in result.output


def test_main_server_url_with_protocol_and_port(mocker: MockerFixture) -> None:
    mocked_client = mocker.patch("openqa_advanced_retrigger_jobs.OpenQA_Client")
    mocker.patch("openqa_advanced_retrigger_jobs.fetch_job_ids", return_value=[1])
    result = runner.invoke(retrigger.app, ["--cli-protocol", "http", "--cli-port", "8080"])
    assert result.exit_code == 0
    assert mocked_client.call_args.kwargs == {"server": "openqa.opensuse.org:8080", "scheme": "http"}


def test_main_verbosity_controls_dry_run_output(mocker: MockerFixture) -> None:
    client = mocker.MagicMock()
    mocker.patch("openqa_advanced_retrigger_jobs.OpenQA_Client", return_value=client)
    quiet = runner.invoke(retrigger.app, ["--dry-run", "--job-ids", "7"])
    verbose = runner.invoke(retrigger.app, ["-v", "--dry-run", "--job-ids", "7"])
    assert quiet.exit_code == verbose.exit_code == 0
    assert "would POST" not in quiet.output
    assert "would POST" in verbose.output
