# Copyright SUSE LLC
# ruff: noqa: FBT001
"""Unit tests for s390x-qemu-zombie-reaper.py."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# Load the script as module "reaper" (the file is named `s390x-qemu-zombie-reaper.py`)
rootpath = pathlib.Path(__file__).parent.parent.resolve()
loader = importlib.machinery.SourceFileLoader("reaper", f"{rootpath}/s390x-qemu-zombie-reaper.py")
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
reaper = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = reaper
loader.exec_module(reaper)


def test_run_cmd(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = mocker.MagicMock(stdout="  some output \n", stderr="", returncode=0)
    assert reaper.run_cmd("echo test") == "some output"
    mock_run.assert_called_once()


def test_get_running_jobs(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("reaper.run_cmd")
    mock_run.return_value = '{"workers": [{"properties": {"WORKER_CLASS": "qemu_x86_64,s390zl12"}, "jobid": 123}]}'
    assert reaper.get_running_jobs("s390zl12.oqa.prg2.suse.org") == [123]


@pytest.mark.parametrize(
    ("dry_run", "jobs", "expected_calls"),
    [
        (
            True,
            [123],
            [
                (
                    "[DRY-RUN] Would execute: ssh s390zl12.oqa.prg2.suse.org "
                    "\"sudo bash -c 'echo c > /proc/sysrq-trigger'\""
                ),
                "[DRY-RUN] Would execute: openqa-cli api --osd -X POST jobs/123/restart",
            ],
        ),
        (
            False,
            [123],
            ["Triggering kernel crash dump (kdump) on s390zl12.oqa.prg2.suse.org...", "Retriggering job 123..."],
        ),
    ],
)
def test_trigger_actions(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str], dry_run: bool, jobs: list[int], expected_calls: list[str]
) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    reaper.trigger_actions("s390zl12.oqa.prg2.suse.org", jobs, dry_run=dry_run, verbose=False)
    captured = capsys.readouterr().out
    for expected in expected_calls:
        assert expected in captured
    if not dry_run:
        mock_run_cmd.assert_any_call(
            "ssh s390zl12.oqa.prg2.suse.org \"sudo bash -c 'echo c > /proc/sysrq-trigger'\"", verbose=False
        )


def test_handle_host_clean(mocker: MockerFixture) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_run_cmd.return_value = ""
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", dry_run=False, verbose=False)
    mock_run_cmd.assert_called_once_with(
        "ssh s390zl12.oqa.prg2.suse.org pgrep -r Z qemu-system-s39", check=False, verbose=False
    )


def test_handle_host_zombie_persistent(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_sleep = mocker.patch("time.sleep")
    mock_run_cmd.side_effect = ["12345", "12345 Fri Jul 17 2026 Z", "12345 Fri Jul 17 2026 Z", '{"workers": []}', ""]
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", dry_run=False, verbose=False)
    captured = capsys.readouterr().out
    assert "!!! CRITICAL: Found persistent zombie processes on s390zl12.oqa.prg2.suse.org: 12345" in captured
    mock_sleep.assert_called_once_with(10)
