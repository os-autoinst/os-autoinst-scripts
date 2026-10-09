#!/usr/bin/env python3
# Copyright SUSE LLC
# ruff: file-ignore[print]

"""Automated openQA Zombie Reaper for s390x.

Checks s390x hypervisors for persistent zombie qemu processes and reboots the host
if any are found, while also retriggering the affected openQA jobs.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
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
FAILED_JOB_RESULTS = frozenset({"failed", "incomplete", "parallel_failed", "timeout_exceeded"})


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


def run_cmd(cmd: str, *, check: bool = True, verbose: bool = False) -> str:
    """Run a shell command and return its output."""
    if verbose:
        print(f"Executing: {cmd}")
    try:
        res = subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=check)
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {cmd}\n{e.stderr}")
        return ""


def get_running_jobs(hypervisor_host: str, *, verbose: bool = False) -> list[int]:
    """Fetch job IDs currently assigned to workers using this hypervisor."""
    # Extract the short name (e.g., s390zl12) from the FQDN
    short_name = hypervisor_host.split(".", maxsplit=1)[0]

    output = run_cmd("openqa-cli api --osd -X GET workers", verbose=verbose)
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


def get_job_info(job_id: int, *, verbose: bool = False) -> dict[str, Any] | None:
    """Fetch job details from openQA."""
    output = run_cmd(f"openqa-cli api --osd -X GET jobs/{job_id}", verbose=verbose)
    if not output:
        return None
    try:
        data: dict[str, Any] = json.loads(output)
        return data.get("job")
    except json.JSONDecodeError as e:
        print(f"Failed to fetch job info for {job_id}: {e}")
        return None


def should_restart_job(job_id: int, *, verbose: bool = False) -> bool:
    """Check whether a job has failed and needs to be restarted."""
    job = get_job_info(job_id, verbose=verbose)
    if not job:
        print(f"Could not retrieve info for job {job_id}. Skipping restart.")
        return False

    state = job.get("state")
    result = job.get("result")
    clone_id = job.get("clone_id")

    if clone_id:
        print(f"Job {job_id} already cloned as job {clone_id}. Skipping restart.")
        return False

    if state != "done":
        print(f"Job {job_id} is in state '{state}' (not done). Skipping restart.")
        return False

    if result not in FAILED_JOB_RESULTS:
        print(f"Job {job_id} completed with result '{result}'. Skipping restart.")
        return False

    return True


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


def restart_failed_jobs(jobs: list[int], config: ReaperConfig) -> None:
    """Restart jobs that have failed."""
    for job_id in jobs:
        if not should_restart_job(job_id, verbose=config.verbose):
            continue
        retrigger_cmd = f"openqa-cli api --osd -X POST jobs/{job_id}/restart"
        if config.dry_run:
            print(f"[DRY-RUN] Would execute: {retrigger_cmd}")
        else:
            print(f"Retriggering job {job_id}...")
            run_cmd(retrigger_cmd, verbose=config.verbose)


def trigger_actions(
    host: str,
    jobs: list[int],
    config: ReaperConfig,
) -> None:
    """Trigger kdump (which reboots the machine) or standard reboot, and job re-triggering."""
    if config.reboot_method == RebootMethod.SYSRQ:
        # Echoing 'c' to sysrq-trigger panics the kernel, dumping core and rebooting
        reboot_cmd = f"ssh {host} \"sudo bash -c 'echo c > /proc/sysrq-trigger'\""
        action_msg = f"Triggering kernel crash dump (kdump) on {host}..."
    else:
        reboot_cmd = f'ssh {host} "sudo reboot"'
        action_msg = f"Triggering reboot on {host}..."

    print(action_msg)
    if config.dry_run:
        print(f"[DRY-RUN] Would execute: {reboot_cmd}")
    else:
        run_cmd(reboot_cmd, check=False, verbose=config.verbose)

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

    restart_failed_jobs(jobs, config)


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


def handle_host(
    host: str,
    config: ReaperConfig,
) -> None:
    """Check a single host for zombies and take action if found."""
    if config.verbose:
        print(f"Checking {host} for zombies...")

    critical_msg = None

    # Discover zombie qemu processes
    zombie_pids_str = run_cmd(f"ssh {host} pgrep -r Z qemu-system-s39", check=False, verbose=config.verbose)
    if zombie_pids_str:
        # Snapshot process candidates (PID + start time + state) to detect PID reuse
        pids = ",".join(zombie_pids_str.split())
        id_cmd = f"ssh {host} ps -o pid,lstart,state -p {pids} --no-headers"
        initial_candidates = run_cmd(id_cmd, check=False, verbose=config.verbose)

        print(f"Detected potential zombies on {host}, waiting 10s to verify persistence...")
        time.sleep(10)

        # Re-verify candidates. Only processes that match exactly are confirmed as persistent zombies.
        verified_candidates = run_cmd(id_cmd, check=False, verbose=config.verbose)
        stuck_candidates = set(initial_candidates.splitlines()).intersection(verified_candidates.splitlines())

        if stuck_candidates:
            stuck_pids = [line.split(maxsplit=1)[0] for line in stuck_candidates]
            critical_msg = f"!!! CRITICAL: Found persistent zombie processes on {host}: {', '.join(stuck_pids)}"
        elif config.verbose:
            print(f"Zombies on {host} were transient or PIDs were reused.")

    if not critical_msg and not check_libvirt_health(host, config):
        critical_msg = f"!!! CRITICAL: libvirt is unhealthy on {host}"

    if critical_msg:
        print(critical_msg)
        print(f"Identifying jobs which ran on {host}...")
        jobs = get_running_jobs(host, verbose=config.verbose)
        print(
            f"Active jobs on {host} to check after reboot: {', '.join(map(str, jobs))}"
            if jobs
            else f"No active jobs found using {host}."
        )
        trigger_actions(host, jobs, config)
    elif config.verbose:
        print(f"Host {host} is clean and libvirt is healthy.")


@app.command()
def reap(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be done without executing")] = False,  # ruff: ignore[boolean-default-value-positional-argument]
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,  # ruff: ignore[boolean-default-value-positional-argument]
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
) -> None:
    """Check hypervisors for zombies and reboot/retrigger jobs if found."""
    config = ReaperConfig(
        dry_run=dry_run,
        verbose=verbose,
        reboot_method=reboot_method,
        max_wait_minutes=max_wait_minutes,
        stability_delay_minutes=stability_delay_minutes,
    )
    for host in HYPERVISORS:
        handle_host(host, config)


if __name__ == "__main__":
    app()
