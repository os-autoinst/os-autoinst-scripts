#!/usr/bin/python3
# Copyright SUSE LLC
"""Verify qe-lsg NetBox machines with non-excluded statuses are not reachable via ping.

Exits 1 if any such machine responds to ping, indicating it is unexpectedly powered on.
By default machines with status active, unused, or staged are excluded from the check.
"""

from __future__ import annotations

import argparse
import logging
import operator
import os
import sys
from functools import reduce

import pynetbox
from sh import ErrorReturnCode, ping

ping_args = {"-c1", "-W1"}


def check_ping(destination: str | pynetbox.models.ipam.IpAddresses) -> bool:
    """Signal (True) that a machine is reachable when it should not be."""
    destination = str(destination).split("/")[0]
    try:
        _ = ping(destination, *ping_args)
    except ErrorReturnCode:
        return False
    log.warning("Ping to destination %s was successfull, this will fail the script!", destination)
    return True


def check_machine(machine: pynetbox.models.dcim.Devices) -> bool:
    """Detect reachability across all known addresses of a machine to avoid false negatives."""
    attributes_to_check = {"oob_ip", "primary_ip", "primary_ip4", "primary_ip6"}
    machine_attributes = [getattr(machine, attr) for attr in attributes_to_check if getattr(machine, attr) is not None]
    log.debug(
        "Going to ping the attributes %s (values: %s) from machine %s", attributes_to_check, machine_attributes, machine
    )
    return any([check_ping(x) for x in machine_attributes] + [check_ping(machine.name + ".")])


def main(args: argparse.Namespace) -> int:
    """Fail if any non-excluded qe-lsg machine is still reachable, indicating it was not properly powered off."""
    nb = pynetbox.api(args.netbox_url, token=args.netbox_token)

    excluded_states = reduce(operator.add, args.exclude_status)
    machine_filters = {
        "tag": "qe-lsg",
        "status__n": set(excluded_states),
    }
    our_machines = nb.dcim.devices.filter(**machine_filters)
    return int(any(check_machine(machine) for machine in our_machines))


def loglevel_to_int(loglevel: str) -> int:
    """Allow LOGLEVEL env var to set the default verbosity as if the user had passed that many -v flags."""
    loglevel_step = logging.CRITICAL - logging.ERROR
    max_loglevel = logging.CRITICAL / loglevel_step
    return max_loglevel - int(getattr(logging, loglevel.upper(), None) / loglevel_step)


log = logging.getLogger(sys.argv[0] if __name__ == "__main__" else __name__)
if __name__ == "__main__":
    logging.basicConfig()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v",
        "--verbose",
        help="Increase verbosity level, specify multiple times to increase verbosity",
        action="count",
        default=loglevel_to_int(os.environ.get("LOGLEVEL", "ERROR").upper()),
    )
    parser.add_argument(
        "--netbox-url", help="Netbox instance to use", default=os.environ.get("NETBOX_URL", "https://netbox.suse.de")
    )
    parser.add_argument(
        "--netbox-token", help="API token used to fetch entries from netbox", default=os.environ.get("NETBOX_TOKEN", "")
    )
    parser.add_argument("--exclude-status", action="append", nargs="+", default=[["active", "unused", "staged"]])
    args = parser.parse_args()
    verbose_to_log = {
        0: logging.CRITICAL,
        1: logging.ERROR,
        2: logging.WARNING,
        3: logging.INFO,
        4: logging.DEBUG,
    }
    log.setLevel(logging.DEBUG if args.verbose > max(verbose_to_log) else verbose_to_log[args.verbose])
    log.debug(args)
    sys.exit(main(args))
