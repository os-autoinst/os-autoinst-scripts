#!/usr/bin/python3
# Copyright SUSE LLC
import argparse
import configparser
import json
import logging
import os
import subprocess  # noqa: S404
from pathlib import Path

import requests

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(levelname)s: %(message)s")
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)

machine_list_idle = []
machine_list_offline = []
machine_list_broken = []
machine_list_busy = []
machines_to_power_on = []

jobs_worker_classes = []

config_file = Path(os.environ.get("OPENQA_CONFIG", "/etc/openqa")).joinpath("openqa.ini")
config = configparser.ConfigParser()
config.read(config_file)

openqa_server = "http://localhost"

# Manage cmdline options
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--osd", action="store_true")
    parser.add_argument("--o3", action="store_true")
    args = parser.parse_args()
    if args.config is not None and len(args.config):
        config_file = args.config
    if args.host is not None and len(args.host):
        openqa_server = args.host
    elif args.osd:
        openqa_server = "https://openqa.suse.de"
    elif args.o3:
        openqa_server = "https://openqa.opensuse.org"

logger.info("Using openQA server: %s", openqa_server)
logger.info("Using config file: %s", config_file)
if args.dry_run:
    logger.info("Dry run mode")
logger.info("")

# Scheduled/blocked jobs
scheduled_list_file = requests.get(openqa_server + "/tests/list_scheduled_ajax", timeout=60).content
scheduled_list_data = json.loads(scheduled_list_file)
logger.info(
    "Processing %s job(s) in scheduled/blocked state... (will take about %s seconds)",
    len(scheduled_list_data["data"]),
    int(len(scheduled_list_data["data"]) * 0.2),
)

# Create list of WORKER_CLASS needed
for job in scheduled_list_data["data"]:
    response = requests.get(openqa_server + "/api/v1/jobs/" + str(job["id"]), timeout=60)
    job_data = json.loads(response.content)
    jobs_worker_classes.append(job_data["job"]["settings"]["WORKER_CLASS"])

jobs_worker_classes = sorted(set(jobs_worker_classes))
logger.info(
    "Found %s different WORKER_CLASS in scheduled jobs: %s",
    len(jobs_worker_classes),
    jobs_worker_classes,
)


# Workers
workers_list_file = requests.get(openqa_server + "/api/v1/workers", timeout=60).content
workers_list_data = json.loads(workers_list_file)

# Create list of hosts which may need to powered up/down
for worker in workers_list_data["workers"]:
    if worker["status"] == "idle":
        machine_list_idle.append(worker["host"])
    elif worker["status"] == "dead":  # Looks like 'dead' means 'offline'
        machine_list_offline.append(worker["host"])
    elif worker["status"] == "running":  # Looks like 'running' means 'working'
        machine_list_busy.append(worker["host"])
    elif worker["status"] == "broken":
        machine_list_broken.append(worker["host"])
    else:
        logger.info("Unhandle worker status: %s", worker["status"])

# Clean-up the lists
machine_list_idle = sorted(set(machine_list_idle))
machine_list_offline = sorted(set(machine_list_offline))
machine_list_broken = sorted(set(machine_list_broken))
machine_list_busy = sorted(set(machine_list_busy))

# Remove the machine from idle/offline lists if at least 1 worker is busy
for machine in machine_list_busy:
    if machine in machine_list_idle:
        machine_list_idle.remove(machine)
    if machine in machine_list_offline:
        machine_list_offline.remove(machine)
# Remove the machine from offline list if at least 1 worker is idle
for machine in machine_list_idle:
    if machine in machine_list_offline:
        machine_list_offline.remove(machine)

# Print an overview
logger.info("%s workers listed fully idle: %s", len(machine_list_idle), machine_list_idle)
logger.info("%s workers listed offline/dead: %s", len(machine_list_offline), machine_list_offline)
logger.info("%s workers listed broken: %s", len(machine_list_broken), machine_list_broken)
logger.info("%s workers listed busy: %s", len(machine_list_busy), machine_list_busy)

# Get WORKER_CLASS for each workers of each machines (idle and offline) and compare to WORKER_CLASS required by
# scheduled/blocked jobs
for worker in workers_list_data["workers"]:
    if worker["host"] in machine_list_offline:
        machines_to_power_on.extend([
            worker["host"]
            for classes in jobs_worker_classes
            if set(classes.split(",")).issubset(worker["properties"]["WORKER_CLASS"].split(","))
        ])

    if worker["host"] in machine_list_idle and worker["properties"]["WORKER_CLASS"] in jobs_worker_classes:
        # Warning: scheduled (blocked?) job could be run on idle machine!
        logger.info("Warning: scheduled (blocked?) job could be run on idle machine!")

# Power on machines which can run scheduled jobs
for machine in sorted(set(machines_to_power_on)):
    if machine in machine_list_broken:
        logger.info("Removing '%s' from the list to power ON since some workers are broken there", machine)
    elif args.dry_run:
        logger.info("Would power ON '%s' - Dry run mode", machine)
    elif "power_management" in config and config["power_management"].get(machine + "_POWER_ON"):
        logger.info("Powering ON: %s", machine)
        subprocess.run(config["power_management"][machine + "_POWER_ON"], shell=True, check=True)  # noqa: S602
    else:
        logger.info("Unable to power ON '%s' - No command for that", machine)

# Power off machines which are idle or broken (TODO: add a threshold, e.g. idle since more than 15 minutes.
# Does API provide this information?)
for machine in machine_list_idle + machine_list_broken:
    if args.dry_run:
        logger.info("Would power OFF '%s' - Dry run mode", machine)
    elif "power_management" in config and config["power_management"].get(machine + "_POWER_OFF"):
        logger.info("Powering OFF: %s", machine)
        subprocess.run(config["power_management"][machine + "_POWER_OFF"], shell=True, check=True)  # noqa: S602
    else:
        logger.info("Unable to power OFF '%s' - No command for that", machine)
