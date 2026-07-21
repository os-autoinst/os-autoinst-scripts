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
    ("dry_run", "jobs", "reboot_method", "expected_calls", "expected_cmd"),
    [
        (
            True,
            [123],
            "sysrq",
            [
                (
                    "[DRY-RUN] Would execute: ssh s390zl12.oqa.prg2.suse.org "
                    "\"sudo bash -c 'echo c > /proc/sysrq-trigger'\""
                ),
                "[DRY-RUN] Would execute: openqa-cli api --osd -X POST jobs/123/restart",
            ],
            None,
        ),
        (
            True,
            [123],
            "reboot",
            [
                ('[DRY-RUN] Would execute: ssh s390zl12.oqa.prg2.suse.org "sudo reboot"'),
                "[DRY-RUN] Would execute: openqa-cli api --osd -X POST jobs/123/restart",
            ],
            None,
        ),
        (
            False,
            [123],
            "sysrq",
            ["Triggering kernel crash dump (kdump) on s390zl12.oqa.prg2.suse.org...", "Retriggering job 123..."],
            "ssh s390zl12.oqa.prg2.suse.org \"sudo bash -c 'echo c > /proc/sysrq-trigger'\"",
        ),
        (
            False,
            [123],
            "reboot",
            ["Triggering reboot on s390zl12.oqa.prg2.suse.org...", "Retriggering job 123..."],
            'ssh s390zl12.oqa.prg2.suse.org "sudo reboot"',
        ),
    ],
)
def test_trigger_actions(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    jobs: list[int],
    reboot_method: str,
    expected_calls: list[str],
    expected_cmd: str | None,
) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mocker.patch("reaper.wait_for_host", return_value=True)
    mocker.patch("time.sleep")
    method = reaper.RebootMethod(reboot_method)
    config = reaper.ReaperConfig(dry_run=dry_run, verbose=False, reboot_method=method)
    reaper.trigger_actions("s390zl12.oqa.prg2.suse.org", jobs, config)
    captured = capsys.readouterr().out
    for expected in expected_calls:
        assert expected in captured
    if not dry_run and expected_cmd:
        mock_run_cmd.assert_any_call(expected_cmd, check=False, verbose=False)


def test_handle_host_clean(mocker: MockerFixture) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_run_cmd.return_value = ""
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    mock_run_cmd.assert_called_once_with(
        "ssh s390zl12.oqa.prg2.suse.org pgrep -r Z qemu-system-s39", check=False, verbose=False
    )


def test_handle_host_zombie_persistent(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_sleep = mocker.patch("time.sleep")
    mock_run_cmd.side_effect = ["12345", "12345 Fri Jul 17 2026 Z", "12345 Fri Jul 17 2026 Z", '{"workers": []}', ""]
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "!!! CRITICAL: Found persistent zombie processes on s390zl12.oqa.prg2.suse.org: 12345" in captured
    mock_sleep.assert_called_once_with(10)


def test_handle_host_zombie_persistent_reboot(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_sleep = mocker.patch("time.sleep")
    mock_run_cmd.side_effect = ["12345", "12345 Fri Jul 17 2026 Z", "12345 Fri Jul 17 2026 Z", '{"workers": []}', ""]
    config = reaper.ReaperConfig(dry_run=False, verbose=False, reboot_method=reaper.RebootMethod.REBOOT)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "Triggering reboot on s390zl12.oqa.prg2.suse.org..." in captured
    mock_sleep.assert_called_once_with(10)


def test_wait_for_host_success(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = mocker.MagicMock(returncode=0)
    mock_sleep = mocker.patch("time.sleep")
    assert reaper.wait_for_host("s390zl12.oqa.prg2.suse.org") is True
    mock_sleep.assert_not_called()


def test_wait_for_host_timeout(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = mocker.MagicMock(returncode=255)
    mock_sleep = mocker.patch("time.sleep")
    mock_time = mocker.patch("time.time")
    # First time.time() returns start_time (0). Subsequent calls simulate elapsed time.
    # To timeout with max_wait_minutes=1, we need elapsed > 60.
    mock_time.side_effect = [0, 10, 30, 65]
    assert reaper.wait_for_host("s390zl12.oqa.prg2.suse.org", max_wait_minutes=1) is False
    assert mock_sleep.call_count == 2


def test_trigger_actions_custom_limits(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mocker.patch("reaper.run_cmd")
    mock_wait = mocker.patch("reaper.wait_for_host", return_value=True)
    mock_sleep = mocker.patch("time.sleep")

    config = reaper.ReaperConfig(
        dry_run=False,
        verbose=False,
        max_wait_minutes=5,
        stability_delay_minutes=1,
    )
    reaper.trigger_actions(
        "s390zl12.oqa.prg2.suse.org",
        [123],
        config,
    )
    captured = capsys.readouterr().out
    assert "Waiting 1 minutes for host stability before restarting jobs..." in captured
    mock_wait.assert_called_once_with("s390zl12.oqa.prg2.suse.org", verbose=False, max_wait_minutes=5)
    mock_sleep.assert_called_once_with(60)
