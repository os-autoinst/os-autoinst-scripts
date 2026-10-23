# Copyright SUSE LLC
"""Unit tests for openqa-schedule-mm-ping-test."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import re
import sys
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from openqa_client.exceptions import RequestError
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

rootpath = pathlib.Path(__file__).parent.parent.resolve()
script_path = rootpath / "openqa-schedule-mm-ping-test"
spec = importlib.util.spec_from_file_location(
    "openqa_schedule_mm_ping_test",
    script_path,
    loader=importlib.machinery.SourceFileLoader("openqa_schedule_mm_ping_test", str(script_path)),
)
assert spec is not None
assert spec.loader is not None
mm_ping = importlib.util.module_from_spec(spec)
sys.modules["openqa_schedule_mm_ping_test"] = mm_ping
spec.loader.exec_module(mm_ping)

runner = CliRunner()

APP_ENV_VARS = (
    "openqa_url",
    "distri",
    "flavor",
    "flavor_override",
    "arch",
    "version",
    "test_name",
    "build_regex",
    "machine",
    "worker_class",
)
PASSED_JOB = {"result": "passed", "settings": {"BUILD": "20250101", "HDD_1": "tw.qcow2"}}
TAP_CLASSES = "qemu_x86_64,tap"


def worker(host: str, status: str = "idle", classes: str = TAP_CLASSES) -> dict[str, Any]:
    return {"host": host, "status": status, "properties": {"WORKER_CLASS": f"{classes},{host}"}}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in APP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def openqa_client(mocker: MockerFixture) -> Any:
    client_cls = mocker.patch("openqa_schedule_mm_ping_test.OpenQA_Client")
    client_cls.workers = [worker("w1")]

    def respond(_method: str, route: str, **_kwargs: Any) -> Any:
        return {"workers": client_cls.workers} if route == "workers" else mocker.DEFAULT

    client_cls.return_value.openqa_request.side_effect = respond
    client_cls.return_value.openqa_request.return_value = {"jobs": [PASSED_JOB]}
    return client_cls


@pytest.fixture(autouse=True)
def scheduled(mocker: MockerFixture) -> dict[str, Any]:
    """Capture the openqa-cli argv and the scenario YAML content it is given."""
    captured: dict[str, Any] = {"returncode": 0}

    def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
        param = next(arg for arg in cmd if arg.startswith("SCENARIO_DEFINITIONS_YAML="))
        captured["yaml"] = pathlib.Path(param.split("=", 1)[1]).read_text(encoding="utf-8")
        captured["cmd"] = cmd
        return mocker.Mock(returncode=captured["returncode"])

    mocker.patch("subprocess.run", side_effect=fake_run)
    return captured


def test_scenario_yaml_is_rendered_from_options(openqa_client: Any, scheduled: dict[str, Any]) -> None:
    openqa_client.workers = [worker("w1", classes="qemu_aarch64,tap")]
    result = runner.invoke(mm_ping.app, ["--arch", "aarch64", "--machine", "M"])
    assert result.exit_code == 0
    data = yaml.safe_load(scheduled["yaml"])
    assert data["products"]["mm-ping-test"]["arch"] == "aarch64"
    assert data["machines"]["M"]["backend"] == "qemu"
    assert data["machines"]["M"]["settings"]["WORKER_CLASS"] == "qemu_aarch64,tap"
    assert data["job_templates"]["ping_client"]["machine"] == "M"
    assert data["job_templates"]["ping_client"]["settings"]["PARALLEL_WITH"] == "ping_server"


def test_worker_class_can_be_overridden(openqa_client: Any, scheduled: dict[str, Any]) -> None:
    openqa_client.workers = [worker("w1", classes="qemu_x86_64,tap,foo")]
    result = runner.invoke(mm_ping.app, ["--worker-class", "qemu_x86_64,tap,foo"])
    assert result.exit_code == 0
    data = yaml.safe_load(scheduled["yaml"])
    assert data["machines"]["uefi"]["settings"]["WORKER_CLASS"] == "qemu_x86_64,tap,foo"


def test_default_machine_is_uefi(scheduled: dict[str, Any]) -> None:
    runner.invoke(mm_ping.app, [])
    data = yaml.safe_load(scheduled["yaml"])
    assert data["machines"]["uefi"]["settings"]["UEFI"] == "1"
    assert data["job_templates"]["ping_server"]["machine"] == "uefi"


def test_uefi_pflash_vars_of_latest_job_is_passed(openqa_client: Any, scheduled: dict[str, Any]) -> None:
    job = {
        "result": "passed",
        "settings": {"BUILD": "20250101", "HDD_1": "tw.qcow2", "UEFI_PFLASH_VARS": "vars.fd"},
    }
    openqa_client.return_value.openqa_request.return_value = {"jobs": [job]}
    runner.invoke(mm_ping.app, [])
    assert "UEFI_PFLASH_VARS=vars.fd" in scheduled["cmd"]


def test_uefi_pflash_vars_is_omitted_without_parent_value(scheduled: dict[str, Any]) -> None:
    runner.invoke(mm_ping.app, [])
    assert not any(arg.startswith("UEFI_PFLASH_VARS=") for arg in scheduled["cmd"])


def test_schedule_command_contains_settings(scheduled: dict[str, Any]) -> None:
    result = runner.invoke(mm_ping.app, ["--poll-interval", "5"])
    assert result.exit_code == 0
    cmd = scheduled["cmd"]
    assert cmd[:4] == ["openqa-cli", "schedule", "--host", "https://openqa.opensuse.org"]
    assert cmd[cmd.index("--poll-interval") + 1] == "5"
    assert "--monitor" in cmd
    assert "--follow" in cmd
    assert "DISTRI=opensuse" in cmd
    assert "FLAVOR=mm-monitoring" in cmd
    assert "_GROUP_ID=0" in cmd
    assert "HDD_1=tw.qcow2" in cmd
    build = next(arg for arg in cmd if arg.startswith("BUILD="))
    assert re.fullmatch(r"BUILD=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}", build)


def test_scenario_file_is_removed_after_run(scheduled: dict[str, Any]) -> None:
    runner.invoke(mm_ping.app, [])
    param = next(arg for arg in scheduled["cmd"] if arg.startswith("SCENARIO_DEFINITIONS_YAML="))
    assert not pathlib.Path(param.split("=", 1)[1]).exists()


def test_poll_interval_is_omitted_by_default(scheduled: dict[str, Any]) -> None:
    runner.invoke(mm_ping.app, [])
    assert "--poll-interval" not in scheduled["cmd"]


@pytest.mark.parametrize(
    ("args", "absent_flag"),
    [(["--no-monitor"], "--monitor"), (["--no-follow"], "--follow")],
    ids=["no-monitor", "no-follow"],
)
def test_monitor_and_follow_can_be_disabled(args: list[str], absent_flag: str, scheduled: dict[str, Any]) -> None:
    runner.invoke(mm_ping.app, args)
    assert absent_flag not in scheduled["cmd"]


@pytest.mark.parametrize(
    ("jobs", "regex", "expected"),
    [
        ([PASSED_JOB], r"[0-9]+", "tw.qcow2"),
        (
            [
                {"result": "passed", "settings": {"BUILD": "build-20250101", "HDD_1": "a.qcow2"}},
                {"result": "failed", "settings": {"BUILD": "build-20250201", "HDD_1": "b.qcow2"}},
                {"result": "passed", "settings": {"BUILD": "abc", "HDD_1": "c.qcow2"}},
            ],
            r"[0-9]+",
            "a.qcow2",
        ),
        (
            [
                {"result": "passed", "settings": {"BUILD": "20250101", "HDD_1": "a.qcow2"}},
                {"result": "passed", "settings": {"BUILD": "20250201", "HDD_1": "b.qcow2"}},
            ],
            r"[0-9]+",
            "b.qcow2",
        ),
        (
            [
                {"result": "passed", "settings": {"BUILD": "prefix-123", "HDD_1": "a.qcow2"}},
                {"result": "passed", "settings": {"BUILD": "123", "HDD_1": "b.qcow2"}},
            ],
            r"^[0-9]+$",
            "b.qcow2",
        ),
    ],
    ids=["single-passed", "skip-failed-and-nonmatching", "latest-build-wins", "anchored-regex"],
)
def test_selects_latest_matching_passed_hdd(
    openqa_client: Any, scheduled: dict[str, Any], jobs: list[dict[str, Any]], regex: str, expected: str
) -> None:
    openqa_client.return_value.openqa_request.return_value = {"jobs": jobs}
    result = runner.invoke(mm_ping.app, ["--build-regex", regex])
    assert result.exit_code == 0
    assert f"HDD_1={expected}" in scheduled["cmd"]


def test_queries_jobs_with_expected_params(openqa_client: Any) -> None:
    openqa_client.workers = [worker("w1", classes="qemu_A,tap")]
    result = runner.invoke(
        mm_ping.app,
        [
            "--host",
            "https://custom.openqa",
            "--flavor",
            "JeOS",
            "--test-name",
            "t",
            "--machine",
            "M",
            "--arch",
            "A",
            "--version",
            "V",
        ],
    )
    assert result.exit_code == 0
    openqa_client.assert_called_with(server="https://custom.openqa")
    openqa_client.return_value.openqa_request.assert_any_call(
        "GET",
        "jobs",
        params={
            "version": "V",
            "scope": "relevant",
            "arch": "A",
            "machine": "M",
            "flavor": "JeOS",
            "test": "t",
            "latest": 1,
        },
    )


@pytest.mark.parametrize(
    "jobs",
    [[], [{"result": "passed", "settings": {"BUILD": "20250101"}}]],
    ids=["no-jobs", "passed-without-hdd"],
)
def test_fails_without_hdd(openqa_client: Any, scheduled: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    openqa_client.return_value.openqa_request.return_value = {"jobs": jobs}
    result = runner.invoke(mm_ping.app, [])
    assert result.exit_code == 1
    assert "Could not find a passed job with HDD_1" in result.output
    assert "cmd" not in scheduled


def test_fails_on_openqa_client_error(openqa_client: Any, scheduled: dict[str, Any]) -> None:
    openqa_client.return_value.openqa_request.side_effect = RequestError(
        "GET", "https://openqa.test/api/v1/jobs", 500, "boom"
    )
    result = runner.invoke(mm_ping.app, [])
    assert result.exit_code == 1
    assert "Error finding latest HDD" in result.output
    assert "cmd" not in scheduled


def test_pins_jobs_to_a_random_host_and_its_neighbour(
    mocker: MockerFixture, openqa_client: Any, scheduled: dict[str, Any]
) -> None:
    openqa_client.workers = [worker("w1"), worker("w1", status="running"), worker("w2"), worker("w3")]
    randrange = mocker.patch("random.randrange", return_value=2)
    result = runner.invoke(mm_ping.app, [])
    assert result.exit_code == 0
    randrange.assert_called_once_with(3)
    templates = yaml.safe_load(scheduled["yaml"])["job_templates"]
    assert templates["ping_server"]["settings"]["WORKER_CLASS"] == f"{TAP_CLASSES},w3"
    assert templates["ping_client"]["settings"]["WORKER_CLASS"] == f"{TAP_CLASSES},w1"


def test_only_online_hosts_providing_the_worker_class_are_picked(mocker: MockerFixture, openqa_client: Any) -> None:
    openqa_client.workers = [
        worker("w1"),
        worker("w2", status="dead"),
        worker("w3", status="broken"),
        worker("w4", classes="qemu_x86_64"),
        worker("w5", status="running"),
    ]
    mocker.patch("random.randrange", return_value=0)
    assert mm_ping.pick_hosts(openqa_client.return_value, TAP_CLASSES) == ("w1", "w5")


def test_fails_without_online_hosts(openqa_client: Any, scheduled: dict[str, Any]) -> None:
    openqa_client.workers = [worker("w1", status="dead")]
    result = runner.invoke(mm_ping.app, [])
    assert result.exit_code == 1
    assert "No online hosts found providing 'qemu_x86_64,tap'" in result.output
    assert "cmd" not in scheduled


def test_fails_on_workers_query_error(openqa_client: Any, scheduled: dict[str, Any]) -> None:
    error = RequestError("GET", "https://openqa.test/api/v1/workers", 500, "boom")

    def respond(_method: str, route: str, **_kwargs: Any) -> Any:
        if route == "workers":
            raise error
        return {"jobs": [PASSED_JOB]}

    openqa_client.return_value.openqa_request.side_effect = respond
    result = runner.invoke(mm_ping.app, [])
    assert result.exit_code == 1
    assert "Error finding online workers" in result.output
    assert "cmd" not in scheduled


def test_invalid_build_regex_is_rejected() -> None:
    result = runner.invoke(mm_ping.app, ["--build-regex", "[invalid"])
    assert result.exit_code == 2
    assert "invalid regex" in result.output


def test_propagates_openqa_cli_exit_code(scheduled: dict[str, Any]) -> None:
    scheduled["returncode"] = 2
    result = runner.invoke(mm_ping.app, [])
    assert result.exit_code == 2


def test_fails_when_openqa_cli_is_missing(mocker: MockerFixture) -> None:
    mocker.patch("subprocess.run", side_effect=FileNotFoundError)
    result = runner.invoke(mm_ping.app, [])
    assert result.exit_code == 1
    assert "openqa-cli not found" in result.output


def test_help_lists_options() -> None:
    result = runner.invoke(mm_ping.app, ["--help"])
    assert result.exit_code == 0
    plain_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for option in ("--host", "--build-regex", "--monitor", "--no-follow", "--poll-interval"):
        assert option in plain_output


def test_environment_overrides_default(monkeypatch: pytest.MonkeyPatch, scheduled: dict[str, Any]) -> None:
    monkeypatch.setenv("version", "EnvVersion")
    runner.invoke(mm_ping.app, [])
    assert "VERSION=EnvVersion" in scheduled["cmd"]


def test_cli_overrides_environment(monkeypatch: pytest.MonkeyPatch, scheduled: dict[str, Any]) -> None:
    monkeypatch.setenv("version", "EnvVersion")
    runner.invoke(mm_ping.app, ["--version", "CliVersion"])
    assert "VERSION=CliVersion" in scheduled["cmd"]
