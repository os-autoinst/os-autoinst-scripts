# Copyright SUSE LLC
# ruff: file-ignore[boolean-type-hint-positional-argument]
"""Unit tests for s390x-qemu-zombie-reaper.py."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

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
    mock_run_ssh = mocker.patch("reaper.run_ssh", return_value="")
    mock_health = mocker.patch("reaper.check_libvirt_health", return_value=True)
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    mock_run_ssh.assert_called_once_with(
        "s390zl12.oqa.prg2.suse.org", "python3", config, timeout=25, cmd_input=reaper.ZOMBIE_DETECTOR
    )
    mock_health.assert_called_once_with("s390zl12.oqa.prg2.suse.org", config)


def test_handle_host_zombie_persistent(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("reaper.run_ssh", side_effect=["12345 8380780 Z", "12345 8380780 Z"])
    mocker.patch("reaper.run_cmd", side_effect=['{"workers": []}', ""])
    mock_sleep = mocker.patch("time.sleep")
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "!!! CRITICAL: Found persistent zombie processes on s390zl12.oqa.prg2.suse.org: 12345" in captured
    mock_sleep.assert_called_once_with(10)


def test_handle_host_zombie_persistent_reboot(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("reaper.run_ssh", side_effect=["12345 8380780 Z", "12345 8380780 Z"])
    mocker.patch("reaper.run_cmd", side_effect=['{"workers": []}', ""])
    mock_sleep = mocker.patch("time.sleep")
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
    mock_wait.assert_called_once()
    assert mock_wait.call_args.args == ("s390zl12.oqa.prg2.suse.org", config)
    mock_sleep.assert_called_once_with(60)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "side_effect", "expected"),
    [
        (0, "Id Name State\n----------------\n1 openQA-SUT-1 running", "", None, True),
        (1, "", "error: failed to connect to the hypervisor", None, False),
        (0, "error: Disconnected from qemu:///system due to end of file", "", None, False),
        (0, "", "", subprocess.TimeoutExpired("ssh ...", 15), False),
    ],
    ids=["success", "failed_command", "error_in_output", "timeout"],
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


def test_check_libvirt_health_unreachable_raises(mocker: MockerFixture) -> None:
    mocker.patch("subprocess.run").return_value = mocker.MagicMock(
        returncode=reaper.SSH_ERR_CONNECT, stdout="", stderr="ssh: connect to host ..."
    )
    config = reaper.ReaperConfig(dry_run=False, verbose=False)
    with pytest.raises(reaper.HostUnreachableError):
        reaper.check_libvirt_health("s390zl12.oqa.prg2.suse.org", config)


def test_handle_host_unhealthy_libvirt(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("reaper.run_ssh", return_value="")
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_run_cmd.side_effect = ['{"workers": []}', ""]
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
    mocker.patch("reaper.run_ssh", return_value="962249 8363641 Z\n975196 8438974 D\n")
    candidates = reaper.get_zombie_candidates("s390zl13.oqa.prg2.suse.org", reaper.ReaperConfig())
    assert candidates == ["962249 8363641 Z", "975196 8438974 D"]


def test_handle_host_zombie_transient(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("reaper.run_ssh", side_effect=["12345 8380780 Z", ""])
    mocker.patch("time.sleep")
    mock_health = mocker.patch("reaper.check_libvirt_health", return_value=True)
    config = reaper.ReaperConfig(dry_run=False, verbose=True)
    reaper.handle_host("s390zl12.oqa.prg2.suse.org", config)
    captured = capsys.readouterr().out
    assert "Detected potential zombies on s390zl12.oqa.prg2.suse.org, waiting 10s to verify persistence..." in captured
    assert "Zombies on s390zl12.oqa.prg2.suse.org were transient or PIDs were reused." in captured
    mock_health.assert_called_once_with("s390zl12.oqa.prg2.suse.org", config)


def test_handle_host_d_state_persistent(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("reaper.run_ssh", side_effect=["975196 8438974 D", "975196 8438974 D"])
    mocker.patch("reaper.run_cmd", side_effect=['{"workers": []}', ""])
    mocker.patch("time.sleep")
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


def test_reaper_config_ssh_options() -> None:
    config = reaper.ReaperConfig(ssh_timeout=20)
    assert config.ssh_options == "-o ConnectTimeout=20 -o BatchMode=yes"


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


HOST = "s390zl13.oqa.prg2.suse.org"
HMC_HOST = "hmc.example.com"
HMC_NETRC_ENTRY = f"machine {HMC_HOST} login netrc-user password netrc-secret"
LPAR_ID = "lpar-id-13"
LPAR_PATH = f"/logical-partitions/{LPAR_ID}"
HMC_BASE_URL = f"https://{HMC_HOST}:{reaper.HMC_DEFAULT_PORT}/api"


def hmc_config(**overrides: object) -> reaper.ReaperConfig:
    settings: dict[str, object] = {
        "hmc_host": HMC_HOST,
        "hmc_user": "user",
        "hmc_password": "secret",
        "hmc_lpars": {HOST: LPAR_ID},
    }
    return reaper.ReaperConfig(**(settings | overrides))


@pytest.fixture
def environ(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HMC_")}
    monkeypatch.setattr(os, "environ", env)
    return env


def test_recover_host_ssh_success_skips_hmc(mocker: MockerFixture) -> None:
    mocker.patch("reaper.wait_for_host", return_value=True)
    mock_hmc = mocker.patch("reaper.hmc_force_reboot")
    assert reaper.recover_host(HOST, reaper.ReaperConfig()) is True
    mock_hmc.assert_not_called()


def test_recover_host_falls_back_to_hmc_and_waits_again(mocker: MockerFixture) -> None:
    mock_wait = mocker.patch("reaper.wait_for_host", side_effect=[False, True])
    mock_hmc = mocker.patch("reaper.hmc_force_reboot", return_value=True)
    config = reaper.ReaperConfig()
    assert reaper.recover_host(HOST, config) is True
    mock_hmc.assert_called_once_with(HOST, config)
    assert mock_wait.call_count == 2


def test_recover_host_fallback_disabled(mocker: MockerFixture) -> None:
    mocker.patch("reaper.wait_for_host", return_value=False)
    mock_hmc = mocker.patch("reaper.hmc_force_reboot")
    assert reaper.recover_host(HOST, reaper.ReaperConfig(hmc_fallback=False)) is False
    mock_hmc.assert_not_called()


def test_recover_host_hmc_failure_is_not_recovered(mocker: MockerFixture) -> None:
    mock_wait = mocker.patch("reaper.wait_for_host", return_value=False)
    mocker.patch("reaper.hmc_force_reboot", return_value=False)
    assert reaper.recover_host(HOST, reaper.ReaperConfig()) is False
    mock_wait.assert_called_once()


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def write_netrc(home: pathlib.Path, content: str, mode: int = 0o600) -> None:
    netrc_file = home / ".netrc"
    netrc_file.write_text(content)
    netrc_file.chmod(mode)


def test_hmc_credentials_configured_user_and_password_take_precedence(isolated_home: pathlib.Path) -> None:
    write_netrc(isolated_home, HMC_NETRC_ENTRY)
    assert hmc_credentials_for(hmc_user="env-user", hmc_password="env-secret") == ("env-user", "env-secret")


def test_hmc_credentials_from_netrc_entry(isolated_home: pathlib.Path) -> None:
    write_netrc(isolated_home, HMC_NETRC_ENTRY)
    assert hmc_credentials_for() == ("netrc-user", "netrc-secret")


def test_hmc_credentials_without_netrc_entry_is_none(isolated_home: pathlib.Path) -> None:
    write_netrc(isolated_home, "machine other.example.com login u password p")
    assert hmc_credentials_for() is None


def test_hmc_credentials_without_netrc_file_is_none() -> None:
    assert hmc_credentials_for() is None


def test_hmc_credentials_without_hmc_host_ignores_netrc(isolated_home: pathlib.Path) -> None:
    write_netrc(isolated_home, HMC_NETRC_ENTRY)
    assert reaper.hmc_credentials(reaper.ReaperConfig()) is None


def test_hmc_credentials_insecure_netrc_is_ignored(isolated_home: pathlib.Path) -> None:
    write_netrc(isolated_home, HMC_NETRC_ENTRY, mode=0o644)
    assert hmc_credentials_for() is None


def hmc_credentials_for(**overrides: object) -> tuple[str, str] | None:
    return reaper.hmc_credentials(hmc_config(**({"hmc_user": None, "hmc_password": None} | overrides)))


def test_hmc_force_reboot_uses_netrc_credentials(mocker: MockerFixture, isolated_home: pathlib.Path) -> None:
    write_netrc(isolated_home, HMC_NETRC_ENTRY)
    mocker.patch("time.sleep")
    mock_session = mocker.patch("reaper.HmcSession")
    hmc = mock_session.return_value.__enter__.return_value
    hmc.get.side_effect = [{"name": "ZL13", "last-used-load-address": "06000"}, {"status": "operating"}]
    assert reaper.hmc_force_reboot(HOST, hmc_config(hmc_user=None, hmc_password=None)) is True
    mock_session.assert_called_once_with(HMC_BASE_URL, "netrc-user", "netrc-secret", verify_tls=True, ca_file=None)


def test_hmc_force_reboot_passes_ca_file_and_port_to_session(mocker: MockerFixture) -> None:
    mock_session = mocker.patch("reaper.HmcSession")
    mock_session.return_value.__enter__.return_value.get.side_effect = reaper.HmcError("stop after session")
    mocker.patch("time.sleep")
    reaper.hmc_force_reboot(HOST, hmc_config(hmc_port=7000, hmc_ca_file="hmc-ca.pem", hmc_verify_tls=False))
    mock_session.assert_called_once_with(
        f"https://{HMC_HOST}:7000/api", "user", "secret", verify_tls=False, ca_file="hmc-ca.pem"
    )


def test_hmc_force_reboot_without_credentials_skips_session(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_session = mocker.patch("reaper.HmcSession")
    assert reaper.hmc_force_reboot(HOST, hmc_config(hmc_user=None, hmc_password=None)) is False
    mock_session.assert_not_called()
    assert "HMC fallback unavailable" in capsys.readouterr().out


def test_hmc_force_reboot_without_hmc_host_skips_session(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_session = mocker.patch("reaper.HmcSession")
    assert reaper.hmc_force_reboot(HOST, hmc_config(hmc_host=None)) is False
    mock_session.assert_not_called()
    assert "HMC fallback unavailable" in capsys.readouterr().out


def test_hmc_force_reboot_without_lpar_mapping(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_session = mocker.patch("reaper.HmcSession")
    assert reaper.hmc_force_reboot("s390zl99.oqa.prg2.suse.org", hmc_config()) is False
    mock_session.assert_not_called()
    assert "HMC fallback unavailable" in capsys.readouterr().out


def test_hmc_force_reboot_resets_then_loads_last_used_address(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("time.sleep")
    mock_session = mocker.patch("reaper.HmcSession")
    hmc = mock_session.return_value.__enter__.return_value
    hmc.get.side_effect = [
        {"name": "ZL13", "last-used-load-address": "06000"},
        {"status": "not-operating"},
        {"status": "operating"},
    ]
    assert reaper.hmc_force_reboot(HOST, hmc_config()) is True
    assert hmc.operate.call_args_list == [
        mocker.call(f"{LPAR_PATH}/operations/reset-clear", {"force": True}),
        mocker.call(
            f"{LPAR_PATH}/operations/load",
            {"load-address": "06000", "clear-indicator": False, "force": True},
        ),
    ]
    assert "HMC reset and load of s390zl13.oqa.prg2.suse.org completed." in capsys.readouterr().out


def test_hmc_force_reboot_reports_operation_failure(mocker: MockerFixture, capsys: pytest.CaptureFixture[str]) -> None:
    mock_session = mocker.patch("reaper.HmcSession")
    hmc = mock_session.return_value.__enter__.return_value
    hmc.get.return_value = {"name": "ZL13", "last-used-load-address": "06000"}
    hmc.operate.side_effect = reaper.HmcError("reset-clear failed: 500 1")
    assert reaper.hmc_force_reboot(HOST, hmc_config()) is False
    assert (
        "HMC force reboot failed for s390zl13.oqa.prg2.suse.org: reset-clear failed: 500 1" in capsys.readouterr().out
    )


def test_hmc_session_operate_polls_job_until_complete(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")
    session = reaper.HmcSession(HMC_BASE_URL, "user", "secret", verify_tls=True)
    mocker.patch.object(
        session,
        "request",
        return_value=(202, {"job-uri": "/api/jobs/1"}),
    )
    mocker.patch.object(
        session,
        "get",
        side_effect=[{"status": "running"}, {"status": "complete", "job-status-code": 204, "job-reason-code": 0}],
    )
    session.operate(f"{LPAR_PATH}/operations/reset-clear", {"force": True})


@pytest.mark.parametrize(
    ("status", "body", "job", "expected_error"),
    [
        (500, None, None, "unexpected HTTP 500"),
        (
            202,
            {"job-uri": "/api/jobs/1"},
            {"status": "complete", "job-status-code": 500, "job-reason-code": 7},
            "failed: 500 7",
        ),
    ],
    ids=["unexpected_http_status", "failed_job"],
)
def test_hmc_session_operate_errors(
    mocker: MockerFixture, status: int, body: dict | None, job: dict | None, expected_error: str
) -> None:
    mocker.patch("time.sleep")
    session = reaper.HmcSession(HMC_BASE_URL, "user", "secret", verify_tls=False)
    mocker.patch.object(session, "request", return_value=(status, body))
    mocker.patch.object(session, "get", return_value=job)
    with pytest.raises(reaper.HmcError, match=expected_error):
        session.operate(f"{LPAR_PATH}/operations/load", {})


def test_hmc_session_request_http_error_raises(mocker: MockerFixture) -> None:
    http_error = reaper.urllib.error.HTTPError("https://hmc", 401, "Unauthorized", None, None)
    mocker.patch("urllib.request.urlopen", side_effect=http_error)
    session = reaper.HmcSession(HMC_BASE_URL, "user", "wrong", verify_tls=True)
    with pytest.raises(reaper.HmcError, match="POST /sessions failed with HTTP 401"):
        session.request("POST", "/sessions", body={})


def test_trigger_actions_dry_run_mentions_hmc_fallback(capsys: pytest.CaptureFixture[str]) -> None:
    reaper.trigger_actions(HOST, [123], reaper.ReaperConfig(dry_run=True))
    assert f"[DRY-RUN] Would force reset and load {HOST} via HMC if it stays unresponsive." in capsys.readouterr().out


def test_hmc_session_logon_sends_token_and_logs_off(mocker: MockerFixture) -> None:
    response = mocker.MagicMock(status=200)
    response.read.return_value = b'{"api-session": "token-1"}'
    urlopen = mocker.patch("urllib.request.urlopen")
    urlopen.return_value.__enter__.return_value = response
    with reaper.HmcSession(HMC_BASE_URL, "user", "secret", verify_tls=True) as hmc:
        assert hmc.token == "token-1"
    logoff_request = urlopen.call_args_list[-1].args[0]
    assert logoff_request.get_method() == "DELETE"
    assert logoff_request.get_header("X-api-session") == "token-1"


def test_wait_for_lpar_status_times_out(mocker: MockerFixture) -> None:
    mocker.patch("time.sleep")
    mocker.patch("time.time", side_effect=[0, reaper.HMC_STATUS_TIMEOUT + 1])
    hmc = mocker.MagicMock()
    hmc.get.return_value = {"status": "not-operating"}
    with pytest.raises(reaper.HmcError, match="did not reach status operating"):
        reaper._wait_for_lpar_status(hmc, LPAR_PATH, "operating")


def test_run_ssh_connection_failure_raises_unreachable(mocker: MockerFixture) -> None:
    mocker.patch("subprocess.run").return_value = mocker.MagicMock(returncode=reaper.SSH_ERR_CONNECT, stdout="")
    with pytest.raises(reaper.HostUnreachableError):
        reaper.run_ssh(HOST, "python3", reaper.ReaperConfig(), timeout=5)


def test_run_ssh_timeout_raises_unreachable(mocker: MockerFixture) -> None:
    mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 5))
    with pytest.raises(reaper.HostUnreachableError):
        reaper.run_ssh(HOST, "python3", reaper.ReaperConfig(), timeout=5)


def test_wait_for_host_zero_max_wait_checks_once(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("subprocess.run", return_value=mocker.MagicMock(returncode=reaper.SSH_ERR_CONNECT))
    mock_sleep = mocker.patch("time.sleep")
    assert reaper.wait_for_host(HOST, reaper.ReaperConfig(max_wait_minutes=0)) is False
    mock_run.assert_called_once()
    mock_sleep.assert_not_called()


def test_handle_host_unreachable_back_within_grace_is_clean(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("reaper.run_ssh", side_effect=[reaper.HostUnreachableError(HOST), ""])
    mock_health = mocker.patch("reaper.check_libvirt_health", return_value=True)
    mock_wait = mocker.patch("reaper.wait_for_host", return_value=True)
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    reaper.handle_host(HOST, reaper.ReaperConfig(verbose=False))
    assert "CRITICAL" not in capsys.readouterr().out
    mock_wait.assert_called_once()
    mock_health.assert_called_once()
    mock_run_cmd.assert_not_called()


def test_handle_host_unreachable_past_deadline_recovers_via_hmc_without_ssh_reboot(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("reaper.run_ssh", side_effect=reaper.HostUnreachableError(HOST))
    mocker.patch("time.sleep")
    mock_wait = mocker.patch("reaper.wait_for_host", side_effect=[False, True])
    mock_hmc = mocker.patch("reaper.hmc_force_reboot", return_value=True)
    mock_run_cmd = mocker.patch(
        "reaper.run_cmd",
        side_effect=[
            '{"workers": [{"properties": {"WORKER_CLASS": "qemu_s390x,s390zl13"}, "jobid": 123}]}',
            "",
        ],
    )
    reaper.handle_host(HOST, reaper.ReaperConfig(verbose=False))
    captured = capsys.readouterr().out
    assert f"!!! CRITICAL: {HOST} is unreachable over SSH" in captured
    assert f"Host {HOST} is down, skipping reboot command." in captured
    assert "sysrq" not in captured
    mock_hmc.assert_called_once_with(HOST, mocker.ANY)
    assert mock_wait.call_count == 2
    assert mock_run_cmd.call_args_list[-1] == mocker.call(
        "openqa-cli api --osd -X POST jobs/123/restart", verbose=False, timeout=60
    )


def test_trigger_actions_host_down_without_jobs_still_recovers_via_hmc(mocker: MockerFixture) -> None:
    mock_run_cmd = mocker.patch("reaper.run_cmd")
    mock_wait = mocker.patch("reaper.wait_for_host", return_value=True)
    mock_hmc = mocker.patch("reaper.hmc_force_reboot", return_value=True)
    reaper.trigger_actions(HOST, [], reaper.ReaperConfig(dry_run=False), host_down=True)
    mock_run_cmd.assert_not_called()
    mock_hmc.assert_called_once_with(HOST, mocker.ANY)
    mock_wait.assert_called_once()


def test_trigger_actions_records_reboot_in_state_file(mocker: MockerFixture, tmp_path: pathlib.Path) -> None:
    mocker.patch("reaper.run_cmd")
    mocker.patch("time.time", return_value=1234.5)
    state_file = tmp_path / "state.json"
    config = reaper.ReaperConfig(dry_run=False, state_file=state_file)
    reaper.trigger_actions(HOST, [], config)
    assert reaper.read_reboot_state(config) == {HOST: 1234.5}


def test_record_reboot_keeps_other_hosts(mocker: MockerFixture, tmp_path: pathlib.Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text('{"s390zl12.oqa.prg2.suse.org": 1.0}')
    mocker.patch("time.time", return_value=2.0)
    reaper.record_reboot(HOST, reaper.ReaperConfig(state_file=state_file))
    assert reaper.read_reboot_state(reaper.ReaperConfig(state_file=state_file)) == {
        "s390zl12.oqa.prg2.suse.org": 1.0,
        HOST: 2.0,
    }


def test_record_reboot_without_state_file_writes_nothing(tmp_path: pathlib.Path) -> None:
    reaper.record_reboot(HOST, reaper.ReaperConfig(state_file=None))
    assert not list(tmp_path.iterdir())


def test_record_reboot_write_failure_is_reported(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    state_file = tmp_path / "missing-dir" / "state.json"
    reaper.record_reboot(HOST, reaper.ReaperConfig(state_file=state_file))
    assert f"Failed to record reboot of {HOST} in {state_file}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, {}),
        ("not json", {}),
        ("[1, 2]", {}),
        ('{"s390zl13.oqa.prg2.suse.org": 1000.0}', {HOST: 1000.0}),
    ],
    ids=["missing", "corrupt", "not_a_mapping", "valid"],
)
def test_read_reboot_state(tmp_path: pathlib.Path, content: str | None, expected: dict[str, object]) -> None:
    state_file = tmp_path / "state.json"
    if content is not None:
        state_file.write_text(content)
    assert reaper.read_reboot_state(reaper.ReaperConfig(state_file=state_file)) == expected


def test_reboot_deadline_counts_from_recorded_reboot(tmp_path: pathlib.Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(f'{{"{HOST}": 1000.0}}')
    config = reaper.ReaperConfig(max_wait_minutes=30, state_file=state_file)
    assert reaper.reboot_deadline(HOST, config) == pytest.approx(1000.0 + 30 * 60)


def test_reboot_deadline_without_state_counts_from_now(mocker: MockerFixture) -> None:
    mocker.patch("time.time", return_value=500.0)
    assert reaper.reboot_deadline(HOST, reaper.ReaperConfig(max_wait_minutes=30)) == pytest.approx(500.0 + 30 * 60)


def test_reap_no_state_disables_state_file(mocker: MockerFixture) -> None:
    mock_handle = mocker.patch("reaper.handle_host")
    reaper.reap(no_lock=True, no_state=True, dry_run=True)
    assert mock_handle.call_args.args[1].state_file is None


def test_reap_uses_default_state_file_by_default(mocker: MockerFixture) -> None:
    mock_handle = mocker.patch("reaper.handle_host")
    reaper.reap(no_lock=True, dry_run=True)
    assert mock_handle.call_args.args[1].state_file == reaper.DEFAULT_STATE_FILE


def test_recover_host_uses_boot_wait_budget_independent_of_max_wait(mocker: MockerFixture) -> None:
    mocker.patch("time.time", return_value=100.0)
    mock_wait = mocker.patch("reaper.wait_for_host", side_effect=[False, True])
    mocker.patch("reaper.hmc_force_reboot", return_value=True)
    config = reaper.ReaperConfig(max_wait_minutes=0, boot_wait_minutes=30)
    assert reaper.recover_host(HOST, config) is True
    assert [call.kwargs["deadline"] for call in mock_wait.call_args_list] == [pytest.approx(100.0 + 30 * 60)] * 2


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("HMC_HOST=hmc.example.com\n", {"HMC_HOST": "hmc.example.com"}),
        ("# HMC_HOST=commented.example.com\n\nHMC_PORT=7000\n", {"HMC_PORT": "7000"}),
        (
            "export HMC_USER='quoted user'\nHMC_PASSWORD=\"secret\"\n",
            {"HMC_USER": "quoted user", "HMC_PASSWORD": "secret"},
        ),
        ("not an assignment\nHMC_CA_FILE=a=b.pem\n", {"HMC_CA_FILE": "a=b.pem"}),
    ],
)
def test_load_env_file_sets_assignments(
    environ: dict[str, str], tmp_path: pathlib.Path, content: str, expected: dict[str, str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(content)
    reaper.load_env_file(env_file)
    assert {key: value for key, value in environ.items() if key.startswith("HMC_")} == expected


def test_load_env_file_keeps_existing_environment(environ: dict[str, str], tmp_path: pathlib.Path) -> None:
    environ["HMC_PORT"] = "8000"
    env_file = tmp_path / ".env"
    env_file.write_text("HMC_PORT=7000\n")
    reaper.load_env_file(env_file)
    assert environ["HMC_PORT"] == "8000"


def test_load_env_file_missing_default_is_optional(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert reaper.load_env_file(None) is None


def test_parse_hmc_lpars() -> None:
    assert reaper.parse_hmc_lpars([f"{HOST}=lpar-id-13", " other.example.com = lpar-2 "]) == {
        HOST: "lpar-id-13",
        "other.example.com": "lpar-2",
    }


def run_reap_cli(mocker: MockerFixture, *args: str) -> reaper.ReaperConfig:
    mock_reap = mocker.patch("reaper.reap_all_hosts")
    result = CliRunner().invoke(reaper.app, ["--no-lock", "--no-state", *args])
    assert result.exit_code == 0, result.output
    return mock_reap.call_args.args[0]


@pytest.mark.usefixtures("environ")
def test_reap_defaults_without_env_file_or_settings(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.chdir(tmp_path)
    config = run_reap_cli(mocker)
    assert (config.hmc_host, config.hmc_port, config.hmc_user, config.hmc_lpars, config.hmc_verify_tls) == (
        None,
        reaper.HMC_DEFAULT_PORT,
        None,
        {},
        True,
    )


@pytest.mark.usefixtures("environ")
def test_reap_reads_hmc_settings_from_default_env_file(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"HMC_HOST=hmc.example.com\nHMC_USER=file-user\nHMC_PASSWORD=file-secret\n"
        f"HMC_LPARS={HOST}=lpar-id-13\nHMC_VERIFY_TLS=0\nHMC_CA_FILE=hmc-ca.pem\n"
    )
    config = run_reap_cli(mocker)
    assert config.hmc_host == "hmc.example.com"
    assert (config.hmc_user, config.hmc_password) == ("file-user", "file-secret")
    assert config.hmc_lpars == {HOST: "lpar-id-13"}
    assert (config.hmc_verify_tls, config.hmc_ca_file) == (False, "hmc-ca.pem")


def test_reap_precedence_cli_over_environment_over_env_file(
    mocker: MockerFixture, environ: dict[str, str], tmp_path: pathlib.Path
) -> None:
    env_file = tmp_path / "site.env"
    env_file.write_text("HMC_HOST=file.example.com\nHMC_PORT=7000\n")
    environ["HMC_HOST"] = "env.example.com"
    environ["HMC_PORT"] = "8000"
    config = run_reap_cli(mocker, "--env-file", str(env_file), "--hmc-port", "9000")
    assert (config.hmc_host, config.hmc_port) == ("env.example.com", 9000)


def test_reap_hmc_lpars_from_space_separated_environment_variable(
    mocker: MockerFixture, environ: dict[str, str]
) -> None:
    environ["HMC_LPARS"] = "a.example.com=lpar-1 b.example.com=lpar-2"
    assert run_reap_cli(mocker).hmc_lpars == {"a.example.com": "lpar-1", "b.example.com": "lpar-2"}


@pytest.mark.usefixtures("environ")
def test_reap_hmc_lpar_option_is_repeatable(mocker: MockerFixture) -> None:
    config = run_reap_cli(mocker, "--hmc-lpar", "a.example.com=lpar-1", "--hmc-lpar", "b.example.com=lpar-2")
    assert config.hmc_lpars == {"a.example.com": "lpar-1", "b.example.com": "lpar-2"}


@pytest.mark.parametrize("args", [["--hmc-lpar", "no-separator"], ["--hmc-lpar", "=lpar-1"], ["--hmc-lpar", "host="]])
@pytest.mark.usefixtures("environ")
def test_reap_rejects_malformed_hmc_lpar(mocker: MockerFixture, args: list[str]) -> None:
    mock_reap = mocker.patch("reaper.reap_all_hosts")
    result = CliRunner().invoke(reaper.app, ["--no-lock", "--no-state", *args])
    assert result.exit_code == 2
    mock_reap.assert_not_called()


@pytest.mark.usefixtures("environ")
def test_reap_missing_explicit_env_file_is_rejected(mocker: MockerFixture, tmp_path: pathlib.Path) -> None:
    mock_reap = mocker.patch("reaper.reap_all_hosts")
    result = CliRunner().invoke(reaper.app, ["--no-lock", "--no-state", "--env-file", str(tmp_path / "absent.env")])
    assert result.exit_code == 2
    assert "file not found" in result.output
    mock_reap.assert_not_called()
