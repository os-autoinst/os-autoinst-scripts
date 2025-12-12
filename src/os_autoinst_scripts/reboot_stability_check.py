#!/usr/bin/python3
# Copyright SUSE LLC
"""The script checks the reboot stability of a list of hosts."""

import sys
import time

import typer
from rich.console import Console
from sh import ErrorReturnCode, nc, ping, ssh
from tenacity import retry, stop_after_delay, wait_fixed

app = typer.Typer()
console = Console()


def check_one_host(  # noqa: PLR0917 too-many-positional-arguments
    host: str,
    run: int,
    boot_timeout: int,
    ping_count: int,
    sleep_time: int,
    dry: bool,  # noqa: FBT001 boolean-type-hint-positional-argument
) -> None:
    """Check reboot stability for a single host."""
    console.print(f"run: {run}, {host}: ping .. ", end="")

    @retry(reraise=True, stop=stop_after_delay(boot_timeout), wait=wait_fixed(1))
    def retry_ping() -> None:
        ping(f"-c{ping_count}", host)

    @retry(reraise=True, stop=stop_after_delay(boot_timeout), wait=wait_fixed(1))
    def retry_nc() -> None:
        nc("-z", "-w", "1", host, "22")

    retry_ping()
    console.print("ok, ssh .. ", end="")
    retry_nc()
    console.print("ok, uptime/reboot: ", end="")
    console.print(ssh("-o", "ConnectTimeout=10", host, f"uptime && {'echo -n would reboot' if dry else 'sudo reboot'}"))
    time.sleep(sleep_time)


@app.command()
def main(  # noqa: PLR0917 too-many-positional-arguments
    hosts: list[str] = typer.Argument(..., help="List of hosts to check reboot stability for"),
    start: int = typer.Option(1, help="The start run number"),
    runs: int = typer.Option(30, help="The total number of runs"),
    boot_timeout: int = typer.Option(600, help="Timeout for boot"),
    ping_count: int = typer.Option(30, help="Number of pings"),
    sleep_time: float = typer.Option(120, help="Sleep time between reboots"),
    dry: bool = typer.Option(False, "--dry", help="Only check ping+ssh and simulate reboots"),  # noqa: FBT001 FBT003 boolean-type-hint-positional-argument
) -> None:
    """Check the reboot stability of a list of hosts specified in HOSTS."""
    try:
        for run in range(start, runs + 1):
            for host in hosts:
                check_one_host(host, run, boot_timeout, ping_count, sleep_time, dry)
    except ErrorReturnCode as e:
        console.print(f"[bold red]Failed: {e.stderr.decode('utf-8').strip()}. Exiting.[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    app()
