# Copyright SUSE LLC
# ruff: file-ignore[boolean-type-hint-positional-argument]
"""Unit tests for s390x-qemu-zombie-reaper.py."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess
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
                    "[DRY-RUN] Would execute: ssh -o ConnectTimeout=10 -o BatchMode=yes s390zl12.oqa.prg2.suse.org "
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
                (
                    "[DRY-RUN] Would execute: ssh -o ConnectTimeout=10 -o BatchMode=yes s390zl12.oqa.prg2.suse.org "
                    '"sudo reboot"'
                ),
                "[DRY-RUN] Would execute: openqa-cli api --osd -X POST jobs/123/restart",
            ],
            None,
        ),
        (
            False,
            [123],
            "sysrq",
            ["Triggering kernel crash dump (kdump) on s390zl12.oqa.prg2.suse.org...", "Retriggering job 123..."],
            (
                "ssh -o ConnectTimeout=10 -o BatchMode=yes s390zl12.oqa.prg2.suse.org \"sudo bash -c 'echo c >"
                " /proc/sysrq-trigger'\""
            ),
        ),
        (
            False,
            [123],
            "reboot",
            ["Triggering reboot on s390zl12.oqa.prg2.suse.org...", "Retriggering job 123..."],
            'ssh -o ConnectTimeout=10 -o BatchMode=yes s390zl12.oqa.prg2.suse.org "sudo reboot"',
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
        mock_run_cmd.assert_any_call(expected_cmd, check=False, verbose=False, timeout=25)


def test_handle_host_clean(mocker: MockerFixture) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_run_cmd.return_value = ""
    mock_health = mocker.patch("reaper.check_libvirt_health", return_value=True)
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    mock_run_cmd.assert_called_once_with(
        "ssh -o ConnectTimeout=10 -o BatchMode=yes s390zl12.oqa.prg2.suse.org python3",
        check=False,
        verbose=False,
        timeout=25,
        cmd_input=reaper.ZOMBIE_DETECTOR,
    )
    mock_health.assert_called_once_with("s390zl12.oqa.prg2.suse.org", config)


def test_handle_host_zombie_persistent(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_sleep = mocker.patch("time.sleep")
    mock_run_cmd.side_effect = ["12345 8380780 Z", "12345 8380780 Z", '{"workers": []}', ""]
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "!!! CRITICAL: Found persistent zombie processes on s390zl12.oqa.prg2.suse.org: 12345" in captured
    mock_sleep.assert_called_once_with(10)


def test_handle_host_zombie_persistent_reboot(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_sleep = mocker.patch("time.sleep")
    mock_run_cmd.side_effect = ["12345 8380780 Z", "12345 8380780 Z", '{"workers": []}', ""]
    config = reaper.ReaperConfig(dry_run=False, verbose=False, reboot_method=reaper.RebootMethod.REBOOT)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "Triggering reboot on s390zl12.oqa.prg2.suse.org..." in captured
    mock_sleep.assert_called_once_with(10)


def test_wait_for_host_success(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = mocker.MagicMock(returncode=0)
    mock_sleep = mocker.patch("time.sleep")
    assert reaper.wait_for_host("s390zl12.oqa.prg2.suse.org", reaper.ReaperConfig()) is True
    mock_sleep.assert_not_called()


def test_wait_for_host_timeout(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = mocker.MagicMock(returncode=255)
    mock_sleep = mocker.patch("time.sleep")
    mock_time = mocker.patch("time.time")
    # First time.time() returns start_time (0). Subsequent calls simulate elapsed time.
    # To timeout with max_wait_minutes=1, we need elapsed > 60.
    mock_time.side_effect = [0, 10, 30, 65]
    assert reaper.wait_for_host("s390zl12.oqa.prg2.suse.org", reaper.ReaperConfig(max_wait_minutes=1)) is False
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
    mock_wait.assert_called_once_with("s390zl12.oqa.prg2.suse.org", config)
    mock_sleep.assert_called_once_with(60)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "side_effect", "expected"),
    [
        (0, "Id Name State\n----------------\n1 openQA-SUT-1 running", "", None, True),
        (reaper.SSH_ERR_CONNECT, "", "ssh: connect to host ...", None, True),
        (1, "", "error: failed to connect to the hypervisor", None, False),
        (0, "error: Disconnected from qemu:///system due to end of file", "", None, False),
        (0, "", "", subprocess.TimeoutExpired("ssh ...", 15), False),
    ],
    ids=["success", "unreachable_ssh", "failed_command", "error_in_output", "timeout"],
)
def test_check_libvirt_health(
    mocker: MockerFixture, returncode: int, stdout: str, stderr: str, side_effect: Exception | None, expected: bool
) -> None:
    mock_run = mocker.patch("subprocess.run")
    if side_effect:
        mock_run.side_effect = side_effect
    else:
        mock_run.return_value = mocker.MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    assert reaper.check_libvirt_health("s390zl12.oqa.prg2.suse.org", config) is expected


def test_handle_host_unhealthy_libvirt(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_run_cmd.side_effect = ["", '{"workers": []}', ""]
    mock_health = mocker.patch("reaper.check_libvirt_health", return_value=False)
    mocker.patch("reaper.wait_for_host", return_value=True)
    mocker.patch("time.sleep")
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "!!! CRITICAL: libvirt is unhealthy on s390zl12.oqa.prg2.suse.org" in captured
    assert "Triggering kernel crash dump (kdump) on s390zl12.oqa.prg2.suse.org..." in captured
    mock_health.assert_called_once_with("s390zl12.oqa.prg2.suse.org", config)


def test_run_cmd_timeout(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run = mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("test_cmd", 5))
    result = reaper.run_cmd("test_cmd", timeout=5)
    assert not result
    assert "Command timed out after 5s: test_cmd" in capsys.readouterr().out
    mock_run.assert_called_once()


def test_get_zombie_candidates_success(mocker: MockerFixture) -> None:
    mocker.patch("reaper.run_cmd", return_value="962249 8363641 Z\n975196 8438974 D\n")
    candidates = reaper.get_zombie_candidates("s390zl13.oqa.prg2.suse.org", reaper.ReaperConfig())
    assert candidates == ["962249 8363641 Z", "975196 8438974 D"]


def test_handle_host_zombie_transient(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mocker.patch("time.sleep")
    mock_health = mocker.patch("reaper.check_libvirt_health", return_value=True)
    mock_run_cmd.side_effect = ["12345 8380780 Z", ""]
    config = reaper.ReaperConfig(dry_run=False, verbose=True)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "Detected potential zombies on s390zl12.oqa.prg2.suse.org, waiting 10s to verify persistence..." in captured
    assert "Zombies on s390zl12.oqa.prg2.suse.org were transient or PIDs were reused." in captured
    mock_health.assert_called_once_with("s390zl12.oqa.prg2.suse.org", config)


def test_handle_host_d_state_persistent(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mocker.patch("time.sleep")
    mock_run_cmd.side_effect = ["975196 8438974 D", "975196 8438974 D", '{"workers": []}', ""]
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    reaper.handle_host("s390zl13.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "!!! CRITICAL: Found persistent zombie processes on s390zl13.oqa.prg2.suse.org: 975196" in captured


def test_file_lock_lifecycle(tmp_path: pathlib.Path) -> None:
    lock_path = tmp_path / "test.lock"
    with reaper.file_lock(lock_path) as acquired:
        assert acquired is True
        with reaper.file_lock(lock_path) as second_acquired:
            assert second_acquired is False
    with reaper.file_lock(lock_path) as re_acquired:
        assert re_acquired is True


def test_reap_lock_busy(mocker: MockerFixture, capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path) -> None:
    lock_path = tmp_path / "busy.lock"
    mocker.patch("reaper.handle_host")
    with reaper.file_lock(lock_path), pytest.raises(reaper.typer.Exit):
        reaper.reap(lock_file=lock_path)
    captured = capsys.readouterr().out
    assert "Another instance is already running" in captured


def test_run_cmd_error(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "bad_cmd", stderr="command failed"),
    )
    result = reaper.run_cmd("bad_cmd")
    assert not result
    assert "Error executing command: bad_cmd\ncommand failed" in capsys.readouterr().out


def test_get_running_jobs_empty(mocker: MockerFixture) -> None:
    mocker.patch("reaper.run_cmd", return_value="")
    assert reaper.get_running_jobs("s390zl12.oqa.prg2.suse.org") == []


def test_get_running_jobs_invalid_json(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("reaper.run_cmd", return_value="not valid json")
    assert reaper.get_running_jobs("s390zl12.oqa.prg2.suse.org") == []
    assert "Failed to fetch running jobs for s390zl12.oqa.prg2.suse.org" in capsys.readouterr().out


def test_reap_success(mocker: MockerFixture, tmp_path: pathlib.Path) -> None:
    lock_path = tmp_path / "success.lock"
    mock_handle = mocker.patch("reaper.handle_host")
    reaper.reap(lock_file=lock_path, dry_run=True)
    assert mock_handle.call_count == len(reaper.HYPERVISORS)


def test_reap_no_lock(mocker: MockerFixture) -> None:
    mock_handle = mocker.patch("reaper.handle_host")
    reaper.reap(no_lock=True, dry_run=True)
    assert mock_handle.call_count == len(reaper.HYPERVISORS)


def test_reap_all_hosts_parallel(mocker: MockerFixture) -> None:
    mock_handle = mocker.patch("reaper.handle_host")
    config = reaper.ReaperConfig(dry_run=True)
    reaper.reap_all_hosts(config, concurrency=3)
    assert mock_handle.call_count == len(reaper.HYPERVISORS)
    for host in reaper.HYPERVISORS:
        mock_handle.assert_any_call(host, config)


def test_reap_all_hosts_sequential(mocker: MockerFixture) -> None:
    mock_handle = mocker.patch("reaper.handle_host")
    config = reaper.ReaperConfig(dry_run=True)
    reaper.reap_all_hosts(config, concurrency=1)
    assert mock_handle.call_count == len(reaper.HYPERVISORS)
    for host in reaper.HYPERVISORS:
        mock_handle.assert_any_call(host, config)


def test_reap_all_hosts_exception_propagates(mocker: MockerFixture) -> None:
    mocker.patch("reaper.handle_host", side_effect=RuntimeError("connection error"))
    config = reaper.ReaperConfig(dry_run=True)
    with pytest.raises(RuntimeError, match="connection error"):
        reaper.reap_all_hosts(config, concurrency=3)
