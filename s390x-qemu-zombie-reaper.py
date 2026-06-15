#!/usr/bin/env python3
# Copyright SUSE LLC
# ruff: noqa: T201, S404

"""Automated openQA Zombie Reaper for s390x.

Checks s390x hypervisors for persistent zombie qemu processes and reboots the host
if any are found, while also retriggering the affected openQA jobs.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from typing import Any

app = typer.Typer(help="Automated openQA Zombie Reaper for s390x")

# Mapping of hypervisors to the workers that use them
HYPERVISORS = {
    "s390zl12.oqa.prg2.suse.org": ["worker31", "worker32"],
    "s390zl13.oqa.prg2.suse.org": ["worker32", "worker33"],
}


def run_cmd(cmd: str, *, check: bool = True, verbose: bool = False) -> str:
    """Run a shell command and return its output."""
    if verbose:
        print(f"Executing: {cmd}")
    try:
        res = subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=check)  # noqa: S603
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {cmd}\n{e.stderr}")
        return ""


def get_running_jobs(hypervisor_host: str, *, verbose: bool = False) -> list[int]:
    """Fetch job IDs currently assigned to workers using this hypervisor."""
    try:  # noqa: PLW0717
        # Extract the short name (e.g., s390zl12) from the FQDN
        short_name = hypervisor_host.split(".", maxsplit=1)[0]

        output = run_cmd("openqa-cli api --osd -X GET workers", verbose=verbose)
        if not output:
            return []

        data: dict[str, Any] = json.loads(output)
        workers_data = data.get("workers", [])
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
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"Failed to fetch running jobs for {hypervisor_host}: {e}")
        return []


def trigger_actions(host: str, jobs: list[int], *, dry_run: bool, verbose: bool) -> None:
    """Trigger kdump (which reboots the machine) and job re-triggering."""
    # Echoing 'c' to sysrq-trigger panics the kernel, dumping core and rebooting
    reboot_cmd = f'ssh {host} sudo bash -c "echo c > /proc/sysrq-trigger"'
    if dry_run:
        print(f"[DRY-RUN] Would execute: {reboot_cmd}")
        for job_id in jobs:
            retrigger_cmd = f"openqa-cli api --osd -X POST jobs/{job_id}/restart"
            print(f"[DRY-RUN] Would execute: {retrigger_cmd}")
    else:
        print(f"Triggering kernel crash dump (kdump) on {host}...")
        run_cmd(reboot_cmd, verbose=verbose)

        for job_id in jobs:
            retrigger_cmd = f"openqa-cli api --osd -X POST jobs/{job_id}/restart"
            print(f"Retriggering job {job_id}...")
            run_cmd(retrigger_cmd, verbose=verbose)


def handle_host(host: str, *, dry_run: bool, verbose: bool) -> None:
    """Check a single host for zombies and take action if found."""
    if verbose:
        print(f"Checking {host} for zombies...")

    # Discover zombie qemu processes
    zombie_pids_str = run_cmd(f"ssh {host} pgrep -r Z qemu-system-s39", check=False, verbose=verbose)
    if not zombie_pids_str:
        if verbose:
            print(f"Host {host} is clean.")
        return

    # Snapshot process identities (PID + start time + state) to detect PID reuse
    pids = ",".join(zombie_pids_str.split())
    id_cmd = f"ssh {host} ps -o pid,lstart,state -p {pids} --no-headers"
    initial_identities = run_cmd(id_cmd, check=False, verbose=verbose)

    print(f"Detected potential zombies on {host}, waiting 10s to verify persistence...")
    time.sleep(10)

    # Re-verify identities. Only processes that match exactly are confirmed as persistent zombies.
    verified_identities = run_cmd(id_cmd, check=False, verbose=verbose)
    stuck_identities = set(initial_identities.splitlines()).intersection(verified_identities.splitlines())

    if not stuck_identities:
        if verbose:
            print(f"Zombies on {host} were transient or PIDs were reused.")
        return

    # Extract PIDs for logging
    stuck_pids = [line.split()[0] for line in stuck_identities]
    print(f"!!! CRITICAL: Found persistent zombie processes on {host}: {', '.join(stuck_pids)}")

    print(f"Identifying jobs using {host}...")
    jobs = get_running_jobs(host, verbose=verbose)
    if jobs:
        print(f"Affected jobs to be retriggered: {', '.join(map(str, jobs))}")
    else:
        print(f"No active jobs found using {host}.")

    trigger_actions(host, jobs, dry_run=dry_run, verbose=verbose)


@app.command()
def reap(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be done without executing")] = False,  # noqa: FBT002
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,  # noqa: FBT002
) -> None:
    """Check hypervisors for zombies and reboot/retrigger jobs if found."""
    for host in HYPERVISORS:
        handle_host(host, dry_run=dry_run, verbose=verbose)


if __name__ == "__main__":
    app()
