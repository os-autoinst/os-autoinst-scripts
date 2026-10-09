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
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import Any

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
DEFAULT_LOCK_FILE = (
    Path(tempfile.gettempdir()) / f"s390x-qemu-zombie-reaper-{getattr(os, 'getuid', lambda: 'global')()}.lock"
)

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
    stability_delay_minutes: int = 3
    ssh_timeout: int = 10
    ssh_check_interval: int = 15
    cmd_timeout: int = 60


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


def wait_for_host(host: str, config: ReaperConfig) -> bool:
    """Wait until the host is responsive over SSH."""
    print(f"Waiting for {host} to become responsive over SSH...")
    start_time = time.time()
    max_wait_seconds = config.max_wait_minutes * 60

    while time.time() - start_time < max_wait_seconds:
        # Use short timeout to avoid hanging indefinitely
        check_cmd = f"ssh -o ConnectTimeout={config.ssh_timeout} -o BatchMode=yes {host} true"
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
        time.sleep(config.ssh_check_interval)

    print(f"Timeout waiting for {host} to become responsive after {config.max_wait_minutes} minutes.")
    return False


def trigger_actions(
    host: str,
    jobs: list[int],
    config: ReaperConfig,
) -> None:
    """Trigger kdump (which reboots the machine) or standard reboot, and job re-triggering."""
    if config.reboot_method == RebootMethod.SYSRQ:
        # Echoing 'c' to sysrq-trigger panics the kernel, dumping core and rebooting
        reboot_cmd = (
            f"ssh -o ConnectTimeout={config.ssh_timeout} -o BatchMode=yes {host} "
            "\"sudo bash -c 'echo c > /proc/sysrq-trigger'\""
        )
        action_msg = f"Triggering kernel crash dump (kdump) on {host}..."
    else:
        reboot_cmd = f'ssh -o ConnectTimeout={config.ssh_timeout} -o BatchMode=yes {host} "sudo reboot"'
        action_msg = f"Triggering reboot on {host}..."

    print(action_msg)
    if config.dry_run:
        print(f"[DRY-RUN] Would execute: {reboot_cmd}")
    else:
        run_cmd(reboot_cmd, check=False, verbose=config.verbose, timeout=config.ssh_timeout + 15)

    if not jobs:
        return

    if config.dry_run:
        print(f"[DRY-RUN] Would wait for {host} to be responsive over SSH.")
        print(f"[DRY-RUN] Would wait an additional {config.stability_delay_minutes} minutes.")
    else:
        if not wait_for_host(host, config):
            print(f"Host {host} did not come back online. Skipping job retriggering.")
            return

        if config.stability_delay_minutes > 0:
            print(f"Waiting {config.stability_delay_minutes} minutes for host stability before restarting jobs...")
            time.sleep(config.stability_delay_minutes * 60)

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
    check_cmd = f'ssh -o ConnectTimeout={config.ssh_timeout} -o BatchMode=yes {host} "sudo virsh list"'
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
        if config.verbose:
            print(f"Host {host} is unreachable over SSH (exit code 255). Skipping libvirt health check.")
        return True
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
    cmd = f"ssh -o ConnectTimeout={config.ssh_timeout} -o BatchMode=yes {host} python3"
    output = run_cmd(
        cmd,
        check=False,
        verbose=config.verbose,
        timeout=config.ssh_timeout + 15,
        cmd_input=ZOMBIE_DETECTOR,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def handle_host(
    host: str,
    config: ReaperConfig,
) -> None:
    """Check a single host for zombies and take action if found."""
    if config.verbose:
        print(f"Checking {host} for zombies...")

    critical_msg = None

    # Discover zombie or stuck qemu processes
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
            critical_msg = f"!!! CRITICAL: Found persistent zombie processes on {host}: {', '.join(stuck_pids)}"
        elif config.verbose:
            print(f"Zombies on {host} were transient or PIDs were reused.")

    if not critical_msg and not check_libvirt_health(host, config):
        critical_msg = f"!!! CRITICAL: libvirt is unhealthy on {host}"

    if critical_msg:
        print(critical_msg)
        print(f"Identifying jobs which ran on {host}...")
        jobs = get_running_jobs(host, verbose=config.verbose, timeout=config.cmd_timeout)
        print(
            f"Affected jobs to be retriggered: {', '.join(map(str, jobs))}"
            if jobs
            else f"No active jobs found using {host}."
        )
        trigger_actions(host, jobs, config)
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


@app.command()
def reap(  # ruff: ignore[too-many-arguments]
    *,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be done without executing")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,
    reboot_method: Annotated[
        RebootMethod,
        typer.Option("--reboot-method", help="Reboot method to use ('sysrq' to induce crash or 'reboot' to reboot)"),
    ] = RebootMethod.SYSRQ,
    max_wait_minutes: Annotated[
        int,
        typer.Option("--max-wait-minutes", help="Maximum time in minutes to wait for host to become responsive"),
    ] = 30,
    stability_delay_minutes: Annotated[
        int,
        typer.Option("--stability-delay-minutes", help="Time in minutes to wait for host stability after reboot"),
    ] = 3,
    cmd_timeout: Annotated[
        int,
        typer.Option("--cmd-timeout", help="Default timeout in seconds for shell commands"),
    ] = 60,
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
) -> None:
    """Check hypervisors for zombies and reboot/retrigger jobs if found."""
    config = ReaperConfig(
        dry_run=dry_run,
        verbose=verbose,
        reboot_method=reboot_method,
        max_wait_minutes=max_wait_minutes,
        stability_delay_minutes=stability_delay_minutes,
        cmd_timeout=cmd_timeout,
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
