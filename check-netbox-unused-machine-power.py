#!/usr/bin/python3
# Copyright SUSE LLC

# ruff: noqa: T201

# This script will exit 1 if QE machines that are marked as unused
# in netbox still draw more than MAX_POWER Watts (default: 5).

import os
import re
import sys

import netsnmp
import pynetbox

verbose = os.environ.get("VERBOSE") == "1"
debug = os.environ.get("DEBUG") == "1"
netbox_token = os.environ["NETBOX_TOKEN"]
max_power = int(os.environ.get("MAX_POWER", "5"))


def snmp_get(host: str, community: str, oid: str) -> int:
    if debug:
        print(f"snmp_get({host=}, {community=}, {oid=})")
    return int(netsnmp.snmpget(oid, Version=1, DestHost=host, Community=community)[0])


def pdu_get_power(host: str, outlet: int) -> tuple[int, bool]:
    if debug:
        print(f"get_pdu_power({host=}, {outlet=})")
    if host.endswith("qe.nue2.suse.org"):
        # FC-B PDUs are type EATON and directly reachable
        community = "public"
        watts = snmp_get(host, community, f".1.3.6.1.4.1.534.6.6.7.6.5.1.3.0.{outlet}")
        relay = snmp_get(host, community, f".1.3.6.1.4.1.534.6.6.7.6.6.1.2.0.{outlet}")
    elif host.endswith("prg2.suse.org"):
        # PRG2(e) PDUs can be reached via SNMP proxy on qe-jumpy.prg2.suse.org
        snmp_proxy = "qe-jumpy.prg2.suse.org"
        short_host = host.split(".", 1)[0]
        community = f"proxy-{short_host}"
        if host.startswith("pdu-d"):
            # PRG2e-D PDUs are type Bachmann and can be reached via SNMP proxy on qe-jumpy.prg2.suse.org
            fuse = (outlet - 1) // 14
            port = (outlet - 1) % 14
            watts = snmp_get(snmp_proxy, community, f".1.3.6.1.4.1.31770.2.2.8.4.1.5.0.0.0.0.{fuse}.{port}.0.19") // 10
            # 19=on, 20=off
            relay = snmp_get(snmp_proxy, community, f".1.3.6.1.4.1.31770.2.2.9.1.1.5.0.0.0.0.{fuse}.{port}.0.0") == 19
        elif host.startswith("pdu-j"):
            # PRG2-J PDUs are type Rittal and can be reached via SNMP proxy on qe-jumpy.prg2.suse.org
            var_index_watts = 175 + (outlet - 1) * 33
            var_index_relay = 158 + (outlet - 1) * 33
            oid_prefix = ".1.3.6.1.4.1.2606.7.4.2.2.1.11.2"
            watts = snmp_get(snmp_proxy, community, f"{oid_prefix}.{var_index_watts}")
            relay = snmp_get(snmp_proxy, community, f"{oid_prefix}.{var_index_relay}")
    return (watts, bool(relay))


def red(s: str) -> str:
    return f"\x1b[31m{s}\x1b[0m"


def green(s: str) -> str:
    return f"\x1b[32m{s}\x1b[0m"


def print_device(device: pynetbox.models.dcim.Devices, dev_pdu_power: dict, watts: int) -> None:
    s = "  " if verbose else ""
    dev_pdu_power = " ".join([f"{h}:{green(p) if s else red(p)}={w}W" for (h, p), (w, s) in dev_pdu_power.items()])
    print(f"{s}{device.name} status={device.status.value} {dev_pdu_power} ∑{watts}W")


def print_no_connection(device: pynetbox.models.dcim.Devices) -> None:
    if verbose:
        print(f"No connection for {device.name} ({device.display_url})", file=sys.stderr)


# Initialize the NetBox instance
nb = pynetbox.api("https://netbox.suse.de", token=netbox_token)

# Fetch devices matching the tag and status filter, "role=server" only
devices = nb.dcim.devices.filter(tag="qe-lsg", status__n="active", location_id__n={11, 103}, role_id=24)

power_hungry_devices = []
good_devices = []

# Print the results
for device in devices:
    if debug:
        print(f"{device=}")
    power_ports = nb.dcim.power_ports.filter(device_id=device.id)
    dev_pdu_power = {}
    for p in power_ports:
        if not p.connected_endpoints:
            continue
        socket = p.connected_endpoints[0]
        pdu_host = socket.device.description
        pwr_socket = socket.name
        if "suse" not in pdu_host:
            print(f"Invalid PDU '{pdu_host}' ({socket.device.display_url}) for {device.name}", file=sys.stderr)
            continue
        if "-" in pwr_socket:
            pwr_socket = pwr_socket.split("-")[0]
        # strip all but numbers and convert to int
        pwr_socket = int(next(filter(bool, re.findall(r"\d*", pwr_socket))))
        dev_pdu_power[pdu_host, pwr_socket] = pdu_get_power(pdu_host, pwr_socket)
    if not dev_pdu_power:
        print_no_connection(device)
    else:
        dev_total_pwr = 0
        for w, _ in dev_pdu_power.values():
            dev_total_pwr += w
        dev = (device, dev_pdu_power, dev_total_pwr)
        if dev_total_pwr > max_power:
            power_hungry_devices.append(dev)
        else:
            good_devices.append(dev)

if verbose:
    print()
    print("Good:")
    for dev in good_devices:
        print_device(*dev)
    print()
    print("Powerhungry:")
for dev in power_hungry_devices:
    print_device(*dev)

sys.exit(int(len(power_hungry_devices) > 0))
