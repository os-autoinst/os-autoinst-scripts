#!/usr/bin/env python3
# Copyright SUSE LLC
# ruff: file-ignore[print]

"""Automated openQA Zombie Reaper for s390x.

Checks s390x hypervisors for persistent zombie qemu processes and reboots the host
if any are found, while also retriggering the affected openQA jobs.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fcntl
import json
import netrc
import os
import shlex
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import Any, Self

app = typer.Typer(help="Automated openQA Zombie Reaper for s390x")


class RebootMethod(StrEnum):
    """Method used to reboot the hypervisor."""

    SYSRQ = "sysrq"
    REBOOT = "reboot"


# Mapping of hypervisors to the workers that use them
HYPERVISORS = {
    "s390zl12.oqa.prg2.suse.org": ["worker31", "worker32"],
    "s390zl13.oqa.prg2.suse.org": ["worker32", "worker33"],
    "s390zl19.oqa.prg2.suse.org": ["worker31"],
}

RET_SUCCESS = 0
SSH_ERR_CONNECT = 255
HMC_DEFAULT_PORT = 6794
HMC_JOB_TIMEOUT = 900
HMC_STATUS_TIMEOUT = 900
HTTP_ACCEPTED = 202
HTTP_NO_CONTENT = 204
JOB_STATUS_OK = 204
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_LOCK_FILE = (
    Path(tempfile.gettempdir()) / f"s390x-qemu-zombie-reaper-{getattr(os, 'getuid', lambda: 'global')()}.lock"
)
DEFAULT_STATE_FILE = (
    Path(tempfile.gettempdir()) / f"s390x-qemu-zombie-reaper-state-{getattr(os, 'getuid', lambda: 'global')()}.json"
)
_state_lock = threading.Lock()

ZOMBIE_DETECTOR = """
import os
for entry in os.scandir('/proc'):
    if not entry.name.isdigit():
        continue
    try:
        with open(os.path.join(entry.path, 'stat'), 'r') as f:
            content = f.read()
        rparen = content.rfind(')')
        if rparen == -1:
            continue
        comm = content[content.find('(') + 1 : rparen]
        rest = content[rparen + 2 :].split()
        if comm.startswith('qemu') and rest[0] in ('Z', 'D'):
            print(f'{entry.name} {rest[19]} {rest[0]}')
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, OSError):
        continue
""".strip()


@dataclass(frozen=True)
class ReaperConfig:
    """Configuration for the zombie reaper execution."""

    dry_run: bool = False
    verbose: bool = False
    reboot_method: RebootMethod = RebootMethod.SYSRQ
    max_wait_minutes: int = 30
    boot_wait_minutes: int = 30
    stability_delay_minutes: int = 3
    ssh_timeout: int = 10
    ssh_check_interval: int = 15
    cmd_timeout: int = 60
    hmc_fallback: bool = True
    hmc_host: str | None = None
    hmc_port: int = HMC_DEFAULT_PORT
    hmc_user: str | None = None
    hmc_password: str | None = None
    hmc_lpars: dict[str, str] = field(default_factory=dict)
    hmc_verify_tls: bool = True
    hmc_ca_file: str | None = None
    state_file: Path | None = None

    @property
    def ssh_options(self) -> str:
        return f"-o ConnectTimeout={self.ssh_timeout} -o BatchMode=yes"


class HostUnreachableError(Exception):
    """SSH to a hypervisor failed to connect, e.g. because it is still rebooting."""


def run_ssh(host: str, remote_cmd: str, config: ReaperConfig, *, timeout: int, cmd_input: str | None = None) -> str:
    """Run a remote command over SSH and return its stdout. Raises HostUnreachableError on connection failure."""
    cmd = f"ssh {config.ssh_options} {host} {remote_cmd}"
    if config.verbose:
        print(f"Executing: {cmd}")
    try:
        res = subprocess.run(
            shlex.split(cmd), input=cmd_input, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise HostUnreachableError(host) from e
    if res.returncode == SSH_ERR_CONNECT:
        raise HostUnreachableError(host)
    return res.stdout.strip()


@contextlib.contextmanager
def file_lock(path: Path) -> Generator[bool, None, None]:
    """Context manager for acquiring an exclusive non-blocking file lock.

    Yields:
        True if lock was acquired, False otherwise.

    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            acquired = False
        try:
            yield acquired
        finally:
            if acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def run_cmd(
    cmd: str,
    *,
    check: bool = True,
    verbose: bool = False,
    timeout: int = 60,
    cmd_input: str | None = None,
) -> str:
    """Run a shell command and return its output."""
    if verbose:
        print(f"Executing: {cmd}")
    try:
        res = subprocess.run(
            shlex.split(cmd),
            input=cmd_input,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {cmd}\n{e.stderr}")
        return ""
    except subprocess.TimeoutExpired:
        print(f"Command timed out after {timeout}s: {cmd}")
        return ""


def get_running_jobs(hypervisor_host: str, *, verbose: bool = False, timeout: int = 60) -> list[int]:
    """Fetch job IDs currently assigned to workers using this hypervisor."""
    # Extract the short name (e.g., s390zl12) from the FQDN
    short_name = hypervisor_host.split(".", maxsplit=1)[0]

    output = run_cmd("openqa-cli api --osd -X GET workers", verbose=verbose, timeout=timeout)
    if not output:
        return []

    try:
        data: dict[str, Any] = json.loads(output)
        workers_data = data.get("workers", [])
    except json.JSONDecodeError as e:
        print(f"Failed to fetch running jobs for {hypervisor_host}: {e}")
        return []

    jobs_to_restart = []
    for worker in workers_data:
        properties = worker.get("properties", {})
        worker_class = properties.get("WORKER_CLASS", "")

        # Match if the hypervisor short name is in the WORKER_CLASS
        if short_name in worker_class:
            job_id = worker.get("jobid")
            if job_id:
                jobs_to_restart.append(job_id)

    return list(set(jobs_to_restart))


def wait_for_host(host: str, config: ReaperConfig, *, deadline: float | None = None) -> bool:
    """Poll the host over SSH until it responds or the deadline (epoch seconds) passes. Checks at least once."""
    print(f"Waiting for {host} to become responsive over SSH...")
    if deadline is None:
        deadline = time.time() + config.max_wait_minutes * 60

    while True:
        # Use short timeout to avoid hanging indefinitely
        check_cmd = f"ssh {config.ssh_options} {host} true"
        if config.verbose:
            print(f"Checking host availability: {check_cmd}")
        try:
            res = subprocess.run(
                shlex.split(check_cmd),
                capture_output=True,
                text=True,
                check=False,
                timeout=config.ssh_timeout + 5,
            )
            if res.returncode == RET_SUCCESS:
                print(f"Host {host} is responsive again.")
                return True
        except (OSError, subprocess.TimeoutExpired) as e:
            if config.verbose:
                print(f"SSH check failed: {e}")
        if time.time() >= deadline:
            break
        time.sleep(config.ssh_check_interval)

    print(f"Timeout waiting for {host} to become responsive.")
    return False


def read_reboot_state(config: ReaperConfig) -> dict[str, object]:
    """Return the host to last-reboot-time mapping, empty if disabled, missing or corrupt."""
    if config.state_file is None:
        return {}
    try:
        data = json.loads(config.state_file.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_reboot(host: str, config: ReaperConfig) -> None:
    """Record the current time as the last reboot of the host in the state file."""
    if config.state_file is None:
        return
    with _state_lock:
        state = read_reboot_state(config) | {host: time.time()}
        tmp_file = config.state_file.with_name(config.state_file.name + ".tmp")
        try:
            tmp_file.write_text(json.dumps(state))
            tmp_file.replace(config.state_file)
        except OSError as e:
            print(f"Failed to record reboot of {host} in {config.state_file}: {e}")


def boot_deadline(config: ReaperConfig) -> float:
    """Deadline for a host to come back after a reboot or HMC reset."""
    return time.time() + config.boot_wait_minutes * 60


def reboot_deadline(host: str, config: ReaperConfig) -> float:
    """Deadline until which an unreachable host may still be booting, counted from its last recorded reboot."""
    last_reboot = read_reboot_state(config).get(host)
    base = last_reboot if isinstance(last_reboot, int | float) else time.time()
    return base + config.max_wait_minutes * 60


class HmcError(Exception):
    """Failure of an HMC REST operation, with the parts joined into the message."""

    def __init__(self, *parts: object) -> None:
        super().__init__(" ".join(str(part) for part in parts))


class HmcSession:
    """HMC REST API session, logged off on exit."""

    def __init__(
        self, base_url: str, user: str, password: str, *, verify_tls: bool, ca_file: str | None = None
    ) -> None:
        self.base_url = base_url
        self.user = user
        self.password = password
        self.context = ssl.create_default_context(cafile=ca_file)
        if not verify_tls:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.token: str | None = None

    def __enter__(self) -> Self:
        """Log on to the HMC and keep the API session token."""
        status, body = self.request("POST", "/sessions", body={"userid": self.user, "password": self.password})
        if body is None:
            raise HmcError(self.base_url, "logon returned no body, HTTP", status)
        self.token = str(body["api-session"])
        return self

    def __exit__(self, *exc: object) -> None:
        """Log off from the HMC, ignoring failures."""
        with contextlib.suppress(OSError, HmcError):
            self.request("DELETE", "/sessions/this-session")

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None, timeout: int = 120
    ) -> tuple[int, dict[str, object] | None]:
        """Send a request and return the HTTP status with the decoded JSON body, if any."""
        data = json.dumps(body).encode() if body is not None else None
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=data, method=method)  # ruff: ignore[suspicious-url-open-usage]
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-API-Session", self.token)
        try:
            with urllib.request.urlopen(req, context=self.context, timeout=timeout) as resp:  # ruff: ignore[suspicious-url-open-usage]
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise HmcError(method, path, "failed with HTTP", e.code) from e

    def get(self, path: str) -> dict[str, object]:
        """Return the JSON object found at path."""
        return self.request("GET", path)[1] or {}

    def operate(self, path: str, body: dict[str, object]) -> None:
        """Run an HMC operation and wait for its job to complete if it is asynchronous."""
        status, result = self.request("POST", path, body=body)
        if status == HTTP_NO_CONTENT:
            return
        if status != HTTP_ACCEPTED or result is None:
            raise HmcError(path, "returned unexpected HTTP", status)
        self._wait_for_job(str(result["job-uri"]).removeprefix("/api"), path)

    def _wait_for_job(self, job_path: str, operation: str) -> None:
        deadline = time.time() + HMC_JOB_TIMEOUT
        job = self.get(job_path)
        while job["status"] != "complete":
            if time.time() > deadline:
                raise HmcError(operation, "job did not complete within", HMC_JOB_TIMEOUT, "seconds")
            time.sleep(5)
            job = self.get(job_path)
        if job["job-status-code"] != JOB_STATUS_OK:
            raise HmcError(operation, "failed:", job["job-status-code"], job["job-reason-code"])


def _wait_for_lpar_status(hmc: HmcSession, lpar: str, expected: str) -> None:
    deadline = time.time() + HMC_STATUS_TIMEOUT
    while hmc.get(lpar)["status"] != expected:
        if time.time() > deadline:
            raise HmcError(lpar, "did not reach status", expected, "within", HMC_STATUS_TIMEOUT, "seconds")
        time.sleep(10)


def _reset_and_load(hmc: HmcSession, lpar: str, host: str) -> None:
    props = hmc.get(lpar)
    print(f"Forcing reset and load of LPAR {props['name']} for {host} via HMC...")
    hmc.operate(f"{lpar}/operations/reset-clear", {"force": True})
    load_body = {"load-address": props["last-used-load-address"], "clear-indicator": False, "force": True}
    hmc.operate(f"{lpar}/operations/load", load_body)
    _wait_for_lpar_status(hmc, lpar, "operating")


def hmc_credentials(config: ReaperConfig) -> tuple[str, str] | None:
    """Return the configured HMC (user, password), else the ~/.netrc entry for the HMC host."""
    if config.hmc_user and config.hmc_password:
        return config.hmc_user, config.hmc_password
    if config.hmc_host is None:
        return None
    try:
        entry = netrc.netrc().authenticators(config.hmc_host)
    except (OSError, netrc.NetrcParseError):
        return None
    return (entry[0], entry[2]) if entry and entry[0] and entry[2] else None


def hmc_force_reboot(host: str, config: ReaperConfig) -> bool:
    """Force a reset and load of the host's LPAR via the HMC, using its last-used load address."""
    credentials = hmc_credentials(config)
    lpar_id = config.hmc_lpars.get(host)
    if credentials is None or config.hmc_host is None or lpar_id is None:
        print(
            f"HMC fallback unavailable for {host}: provide the HMC host, credentials (--hmc-user/--hmc-password "
            f"or a ~/.netrc entry for the HMC host) and map its LPAR (--hmc-lpar {host}=LPAR_ID)."
        )
        return False

    user, password = credentials
    base_url = f"https://{config.hmc_host}:{config.hmc_port}/api"
    try:
        with HmcSession(base_url, user, password, verify_tls=config.hmc_verify_tls, ca_file=config.hmc_ca_file) as hmc:
            _reset_and_load(hmc, f"/logical-partitions/{lpar_id}", host)
    except (OSError, HmcError, KeyError) as e:
        print(f"HMC force reboot failed for {host}: {e}")
        return False
    print(f"HMC reset and load of {host} completed.")
    return True


def recover_host(host: str, config: ReaperConfig, *, wait_first: bool = True) -> bool:
    """Wait for the host to boot over SSH, forcing a reset and load via HMC if it does not return."""
    if wait_first and wait_for_host(host, config, deadline=boot_deadline(config)):
        return True
    if not config.hmc_fallback:
        return False
    print(f"Host {host} did not come back over SSH, forcing reset and load via HMC...")
    record_reboot(host, config)
    return hmc_force_reboot(host, config) and wait_for_host(host, config, deadline=boot_deadline(config))


def reboot_command(host: str, config: ReaperConfig) -> tuple[str, str]:
    """Return the reboot command for the configured method and the message announcing it."""
    ssh = f"ssh {config.ssh_options} {host}"
    if config.reboot_method == RebootMethod.SYSRQ:
        # Echoing 'c' to sysrq-trigger panics the kernel, dumping core and rebooting
        return (
            f"{ssh} \"sudo bash -c 'echo c > /proc/sysrq-trigger'\"",
            f"Triggering kernel crash dump (kdump) on {host}...",
        )
    return f'{ssh} "sudo reboot"', f"Triggering reboot on {host}..."


def issue_reboot(host: str, config: ReaperConfig) -> None:
    """Trigger kdump (which reboots the machine) or standard reboot via SSH."""
    reboot_cmd, action_msg = reboot_command(host, config)
    print(action_msg)
    if config.dry_run:
        print(f"[DRY-RUN] Would execute: {reboot_cmd}")
        return
    record_reboot(host, config)
    run_cmd(reboot_cmd, check=False, verbose=config.verbose, timeout=config.ssh_timeout + 15)


def recover_for_jobs(host: str, jobs: list[int], config: ReaperConfig, *, host_down: bool) -> bool:
    """Wait for the host to be back (or force it via HMC) and apply the stability delay. False if it did not return."""
    if config.dry_run:
        if not host_down:
            print(f"[DRY-RUN] Would wait for {host} to be responsive over SSH.")
        if config.hmc_fallback:
            print(f"[DRY-RUN] Would force reset and load {host} via HMC if it stays unresponsive.")
        if jobs:
            print(f"[DRY-RUN] Would wait an additional {config.stability_delay_minutes} minutes.")
        return True
    if not recover_host(host, config, wait_first=not host_down):
        print(f"Host {host} did not come back online. Skipping job retriggering.")
        return False
    if jobs and config.stability_delay_minutes > 0:
        print(f"Waiting {config.stability_delay_minutes} minutes for host stability before restarting jobs...")
        time.sleep(config.stability_delay_minutes * 60)
    return True


def trigger_actions(
    host: str,
    jobs: list[int],
    config: ReaperConfig,
    *,
    host_down: bool = False,
) -> None:
    """Reboot the host unless it is already down, recover it if needed and re-trigger affected jobs."""
    if host_down:
        print(f"Host {host} is down, skipping reboot command.")
    else:
        issue_reboot(host, config)

    if (jobs or host_down) and not recover_for_jobs(host, jobs, config, host_down=host_down):
        return

    for job_id in jobs:
        retrigger_cmd = f"openqa-cli api --osd -X POST jobs/{job_id}/restart"
        if config.dry_run:
            print(f"[DRY-RUN] Would execute: {retrigger_cmd}")
        else:
            print(f"Retriggering job {job_id}...")
            run_cmd(retrigger_cmd, verbose=config.verbose, timeout=config.cmd_timeout)


def check_libvirt_health(host: str, config: ReaperConfig) -> bool:
    """Check if libvirt is responsive and healthy on the host."""
    if config.verbose:
        print(f"Checking libvirt health on {host}...")
    check_cmd = f'ssh {config.ssh_options} {host} "sudo virsh list"'
    try:
        res = subprocess.run(
            shlex.split(check_cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"libvirt health check failed on {host}: {e}")
        return False

    if res.returncode == SSH_ERR_CONNECT:
        raise HostUnreachableError(host)
    if res.returncode != RET_SUCCESS:
        print(f"libvirt health check failed on {host}: virsh list returned {res.returncode}")
        if res.stdout:
            print(f"output: {res.stdout.strip()}")
        return False
    if "error:" in res.stdout:
        print(f"libvirt health check failed on {host}: error detected in output")
        return False
    return True


def get_zombie_candidates(host: str, config: ReaperConfig) -> list[str]:
    """Find qemu processes in zombie (Z) or stuck (D) state on the host."""
    output = run_ssh(host, "python3", config, timeout=config.ssh_timeout + 15, cmd_input=ZOMBIE_DETECTOR)
    return [line.strip() for line in output.splitlines() if line.strip()]


def find_critical(host: str, config: ReaperConfig) -> str | None:
    """Return a critical message for persistent zombies or unhealthy libvirt, None if healthy.

    Raises HostUnreachableError if the host cannot be reached over SSH.
    """
    initial_candidates = get_zombie_candidates(host, config)
    if initial_candidates:
        print(f"Detected potential zombies on {host}, waiting 10s to verify persistence...")
        time.sleep(10)

        # Re-verify candidates. Only processes that match exactly are confirmed as persistent zombies.
        verified_candidates = get_zombie_candidates(host, config)
        stuck_candidates = set(initial_candidates).intersection(verified_candidates)

        if stuck_candidates:
            stuck_pids = sorted(
                [line.split(maxsplit=1)[0] for line in stuck_candidates],
                key=lambda x: int(x) if x.isdigit() else x,
            )
            return f"!!! CRITICAL: Found persistent zombie processes on {host}: {', '.join(stuck_pids)}"
        if config.verbose:
            print(f"Zombies on {host} were transient or PIDs were reused.")

    if not check_libvirt_health(host, config):
        return f"!!! CRITICAL: libvirt is unhealthy on {host}"
    return None


def check_host(host: str, config: ReaperConfig) -> tuple[str | None, bool]:
    """Return the critical message (if any) and whether the host is down.

    A host unreachable over SSH may still be rebooting, so it is polled until the reboot deadline before
    it counts as critical.
    """
    try:
        return find_critical(host, config), False
    except HostUnreachableError:
        print(f"Host {host} is unreachable over SSH, it may still be rebooting.")

    if wait_for_host(host, config, deadline=reboot_deadline(host, config)):
        with contextlib.suppress(HostUnreachableError):
            return find_critical(host, config), False
    return f"!!! CRITICAL: {host} is unreachable over SSH", True


def handle_host(
    host: str,
    config: ReaperConfig,
) -> None:
    """Check a single host for zombies and take action if found."""
    if config.verbose:
        print(f"Checking {host} for zombies...")

    critical_msg, host_down = check_host(host, config)
    if critical_msg:
        print(critical_msg)
        print(f"Identifying jobs which ran on {host}...")
        jobs = get_running_jobs(host, verbose=config.verbose, timeout=config.cmd_timeout)
        print(
            f"Affected jobs to be retriggered: {', '.join(map(str, jobs))}"
            if jobs
            else f"No active jobs found using {host}."
        )
        trigger_actions(host, jobs, config, host_down=host_down)
    elif config.verbose:
        print(f"Host {host} is clean and libvirt is healthy.")


def reap_all_hosts(config: ReaperConfig, concurrency: int = len(HYPERVISORS)) -> None:
    """Check all hypervisors, concurrently or sequentially based on concurrency."""
    if concurrency <= 1:
        for host in HYPERVISORS:
            handle_host(host, config)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(handle_host, host, config) for host in HYPERVISORS]
        for future in concurrent.futures.as_completed(futures):
            future.result()


def load_env_file(path: Path | None) -> Path | None:
    """Set variables from a .env file in os.environ, keeping already set ones. The default .env is optional."""
    env_file = path or DEFAULT_ENV_FILE
    if not env_file.is_file():
        if path is not None:
            message = f"file not found: {path}"
            raise typer.BadParameter(message, param_hint="--env-file")
        return path
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().removeprefix("export ")
        key, sep, value = line.partition("=")
        if not sep or not key.strip() or key.startswith("#"):
            continue
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    return path


def parse_hmc_lpars(entries: list[str]) -> dict[str, str]:
    """Parse HOST=LPAR_ID entries into a mapping from hypervisor to its HMC LPAR object ID."""
    lpars = {}
    for entry in entries:
        host, sep, lpar_id = entry.partition("=")
        if not sep or not host.strip() or not lpar_id.strip():
            message = f"expected HOST=LPAR_ID, got {entry!r}"
            raise typer.BadParameter(message, param_hint="--hmc-lpar")
        lpars[host.strip()] = lpar_id.strip()
    return lpars


@app.command()
def reap(  # ruff: ignore[too-many-arguments]
    *,
    env_file: Annotated[  # ruff: ignore[unused-function-argument]
        Path | None,
        typer.Option(
            "--env-file",
            callback=load_env_file,
            is_eager=True,
            help="Read settings from this .env file (default: ./.env if present). "
            "Environment variables and CLI options take precedence over it.",
        ),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be done without executing")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,
    reboot_method: Annotated[
        RebootMethod,
        typer.Option("--reboot-method", help="Reboot method to use ('sysrq' to induce crash or 'reboot' to reboot)"),
    ] = RebootMethod.SYSRQ,
    max_wait_minutes: Annotated[
        int,
        typer.Option(
            "--max-wait-minutes",
            help="Maximum time in minutes to wait for an unreachable host, counted from its last recorded reboot",
        ),
    ] = 30,
    stability_delay_minutes: Annotated[
        int,
        typer.Option("--stability-delay-minutes", help="Time in minutes to wait for host stability after reboot"),
    ] = 3,
    cmd_timeout: Annotated[
        int,
        typer.Option("--cmd-timeout", help="Default timeout in seconds for shell commands"),
    ] = 60,
    hmc_fallback: Annotated[
        bool,
        typer.Option(
            "--hmc-fallback/--no-hmc-fallback",
            help="Force reset and load via HMC if a host does not return over SSH (needs HMC settings)",
        ),
    ] = True,
    hmc_host: Annotated[
        str | None,
        typer.Option(
            "--hmc-host",
            envvar="HMC_HOST",
            help="HMC hostname managing the hypervisor LPARs, e.g. hmc.example.com.",
        ),
    ] = None,
    hmc_port: Annotated[
        int,
        typer.Option(
            "--hmc-port",
            envvar="HMC_PORT",
            help="HMC WebServices REST API port. 6794 is the HMC default, only change it if the HMC uses another port.",
        ),
    ] = HMC_DEFAULT_PORT,
    hmc_user: Annotated[
        str | None,
        typer.Option(
            "--hmc-user",
            envvar="HMC_USER",
            help="HMC user allowed to reset and load partitions. Falls back to the ~/.netrc entry "
            "'machine HMC_HOST login USER password PASS' (file must be mode 600).",
        ),
    ] = None,
    hmc_password: Annotated[
        str | None,
        typer.Option(
            "--hmc-password",
            envvar="HMC_PASSWORD",
            help="Password for HMC_USER. Falls back to the ~/.netrc entry. Prefer HMC_PASSWORD in .env or the "
            "environment: CLI values are visible in the process list.",
        ),
    ] = None,
    hmc_lpar: Annotated[
        list[str] | None,
        typer.Option(
            "--hmc-lpar",
            envvar="HMC_LPARS",
            help="Hypervisor FQDN to HMC LPAR object ID as HOST=LPAR_ID, repeatable; HMC_LPARS takes a "
            "space-separated list, e.g. hypervisor1.example.com=UUID. LPAR_ID is the partition's 'object-id' (a UUID), "
            "not its name. Get it from the HMC REST API: GET https://HMC_HOST:HMC_PORT/api/logical-partitions after "
            "logon, and match the 'name' field of each partition to the hypervisor to take its 'object-id'.",
        ),
    ] = None,
    hmc_verify_tls: Annotated[
        bool,
        typer.Option(
            "--hmc-verify-tls/--no-hmc-verify-tls",
            envvar="HMC_VERIFY_TLS",
            help="Verify the HMC TLS certificate. Use --hmc-ca-file with the HMC's CA instead of disabling this.",
        ),
    ] = True,
    hmc_ca_file: Annotated[
        str | None,
        typer.Option(
            "--hmc-ca-file",
            envvar="HMC_CA_FILE",
            help="PEM file with the CA certificate that signs the HMC certificate. The HMC often uses a self-signed "
            "certificate: export it from the HMC's certificate settings or get it from the HMC admin.",
        ),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option("--concurrency", help="Number of concurrent host checks (1 for sequential)"),
    ] = len(HYPERVISORS),
    lock_file: Annotated[
        Path | None,
        typer.Option("--lock-file", help="Path to lockfile to prevent concurrent execution"),
    ] = None,
    no_lock: Annotated[
        bool,
        typer.Option("--no-lock", help="Disable lockfile check"),
    ] = False,
    no_state: Annotated[
        bool,
        typer.Option("--no-state", help="Ignore and do not update the reboot state file"),
    ] = False,
) -> None:
    """Check hypervisors for zombies and reboot/retrigger jobs if found."""
    config = ReaperConfig(
        dry_run=dry_run,
        verbose=verbose,
        reboot_method=reboot_method,
        max_wait_minutes=max_wait_minutes,
        stability_delay_minutes=stability_delay_minutes,
        cmd_timeout=cmd_timeout,
        hmc_fallback=hmc_fallback,
        hmc_host=hmc_host,
        hmc_port=hmc_port,
        hmc_user=hmc_user,
        hmc_password=hmc_password,
        hmc_lpars=parse_hmc_lpars(hmc_lpar or []),
        hmc_verify_tls=hmc_verify_tls,
        hmc_ca_file=hmc_ca_file,
        state_file=None if no_state else DEFAULT_STATE_FILE,
    )
    if no_lock:
        reap_all_hosts(config, concurrency)
        return

    resolved_lock_file = lock_file or DEFAULT_LOCK_FILE
    with file_lock(resolved_lock_file) as acquired:
        if not acquired:
            print(f"Another instance is already running (lockfile: {resolved_lock_file}). Exiting.")
            raise typer.Exit(code=0)
        reap_all_hosts(config, concurrency)


if __name__ == "__main__":
    app()
