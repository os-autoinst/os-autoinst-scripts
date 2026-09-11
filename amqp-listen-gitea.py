#!/usr/bin/env python3
import pika
import sys
import os
import argparse
import json
import subprocess
import requests
import re
import logging

USER_AGENT = 'amqp-listen-gitea.py (https://github.com/os-autoinst/scripts)'
dry_run=False

logging.basicConfig()
log = logging.getLogger(sys.argv[0] if __name__ == "__main__" else __name__)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="AMQP URL", default="amqps://opensuse:opensuse@rabbit.opensuse.org")
    parser.add_argument("--prefix", help="Event prefix to collect", default="opensuse.src.")
    parser.add_argument("--event-type", help="Event type to collect", default="pull_request_review_request.review_requested")
    parser.add_argument("--myself", help="Username of bot", default="qam-openqa")
    parser.add_argument("--openqa-host", help="OpenQA instance url", default="http://localhost:9526")
    parser.add_argument("--verbose", help="Verbosity", default="1", type=int, choices=[0, 1, 2, 3])
    parser.add_argument("--simulate-review-requested-event", help="Behave as if a pull_request_review_request.review_requested was received")
    args = parser.parse_args()
    return args


def listen(args):
    connection = pika.BlockingConnection(pika.URLParameters(args.url))
    channel = connection.channel()
    channel.exchange_declare(exchange='pubsub', exchange_type='topic', passive=True, durable=True)
    result = channel.queue_declare("", exclusive=True)
    queue_name = result.method.queue
    channel.queue_bind(exchange='pubsub', queue=queue_name,routing_key='#')

    def cb(ch, method, properties, body):
        callback(ch, method, properties, body, args)
    channel.basic_consume(queue_name, cb, auto_ack=True)

    print('[*] Waiting for logs. To exit press CTRL+C')
    channel.start_consuming()


def callback(ch, method, properties, body, args):
    # opensuse.src.someuser.pull_request_review_request.review_requested
    if not method.routing_key.startswith(args.prefix):
        if args.verbose >= 3:
            print("  [ ] %r" % (method.routing_key))
        return
    if args.event_type not in method.routing_key:
        if args.verbose >= 2:
            print("    [ ] %r" % (method.routing_key))
        return
    if args.verbose >= 2:
        print("      [x] %r" % (method.routing_key))
    data = json.loads(body)
    handle_review_request(data, args)


def simulate(args):
    print('================= simulate')
    # json_file = 'tests/data/gitea-amqp/minimal-payload-gitea-review-requested.json'
    json_file = args.simulate_review_requested_event
    with open(json_file, 'r') as f:
        content = f.read()
    data = json.loads(content)
    handle_review_request(data, args)


def handle_review_request(data, args):
    print("============== handle_review_request")
    myself = args.myself
    requested_reviewer = data['requested_reviewer']['username']
    if args.verbose >= 1:
        print("      [x] Requested review from %r" % (requested_reviewer))
    if requested_reviewer != myself:
        return
    pull_request = data['pull_request']
    job_params = {
        'id': pull_request['id'],
        'label': pull_request['head']['label'],
        'branch': pull_request['head']['ref'],
        'sha': pull_request['head']['sha'],
        'pr_html_url': pull_request['html_url'],
        'clone_url': pull_request['head']['repo']['clone_url'],
        'repo_name': pull_request['head']['repo']['name'],
        'repo_api_url': data['repository']['url'],
        'repo_html_url': data['repository']['html_url'],
    }
    params = create_openqa_job_params(args, job_params)
    job_url = openqa_schedule(args, params)
    print(job_url)
    gitea_post_status(job_params, job_url)


def gitea_post_status(job_params, job_url):
    print("============== gitea_post_status")
    sha = job_params['sha']
    statuses_url = job_params['repo_api_url'] + '/statuses/' + job_params['sha'];
    token = os.environ.get("GITEA_TOKEN")
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json', 'Authorization': 'token ' + token}
    payload = {
        'context': 'qam-openqa',
        'description': "openQA check",
        'state': "pending",
        'target_url': job_url,
    }
    request_post(statuses_url, headers, payload)


def request_post(url, headers, payload):
    print("============== request_post")
    print(payload)
    try:
        content = requests.post(url, headers=headers, data=payload)
        content.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.error("Error while fetching %s: %s" % (url, str(e)))
        raise (e)


def create_openqa_job_params(args, job_params):
    print("============== create_openqa_job_params")
    raw_url = job_params['repo_html_url'] + '/raw/branch/' + job_params['sha'];
    statuses_url = job_params['repo_api_url'] + '/statuses/' + job_params['sha'];
    params = {
        'BUILD': job_params['repo_name'] + '#' + job_params['sha'],
        'CASEDIR': job_params['clone_url'] + '#' + job_params['sha'],
        '_GROUP_ID': '0',
        'PRIO': '100',
        'NEEDLES_DIR': '%%CASEDIR%%/needles',

        # set the URL for the scenario definitions YAML file so the Minion job will download it from GitHub
        'SCENARIO_DEFINITIONS_YAML_FILE': raw_url + '/' + 'scenario-definitions.yaml',

        # add "target URL" for the "Details" button of the CI status
        'CI_TARGET_URL': args.openqa_host,

        # set Gitea parameters so the Minion job will be able to report the status back to Gitea
        'GITEA_REPO': job_params['repo_name'],
        'GITEA_SHA': job_params['sha'],
        'GITEA_STATUSES_URL': statuses_url,
        'GITEA_PR_URL': job_params['pr_html_url'],
        'webhook_id': 'gitea:pr:' + str(job_params['id']),
    }
    return params


def openqa_cli(host, subcommand, cmds, dry_run=False):
    print("============== openqa_cli")
    client_args = [
        "openqa-cli",
        subcommand,
        "--host",
        host,
    ] + cmds
    log.debug("openqa_cli: %s %s" % (subcommand, client_args))
    res = subprocess.run(
        (["echo", "Simulating: "] if dry_run else []) + client_args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if len(res.stderr):
        log.warning(f"openqa_cli() {subcommand} stderr: {res.stderr}")
    res.check_returncode()
    return res.stdout.decode("utf-8");


def openqa_schedule(args, params):
    print("============== openqa_schedule")
    scenario_url = 'https://raw.githubusercontent.com/os-autoinst/os-autoinst-distri-openQA/refs/heads/master/scenario-definitions.yaml'
    scenario_yaml = fetch_url(scenario_url, request_type="text")
    yaml_file = "/tmp/distri-openqa-scenario.yaml"
    with open(yaml_file, 'w') as f:
        f.write(scenario_yaml.decode("utf-8"))
    cmd_args = [
        "--param-file",
        "SCENARIO_DEFINITIONS_YAML=" + yaml_file,
        "VERSION=Tumbleweed",
        "DISTRI=openqa",
        "FLAVOR=dev",
        "ARCH=x86_64",
        "HDD_1=opensuse-Tumbleweed-x86_64-20250920-minimalx@uefi.qcow2",
    ]
    for key in params:
        cmd_args.append(key + '=' + params[key])
    output = openqa_cli(args.openqa_host, 'schedule', cmd_args, dry_run)
    pattern = re.compile(r".*?(?P<url>https?://\S+)", re.DOTALL)
    search = pattern.match(output)
    if search:
        url = search.group("url")
    else:
        raise Exception('openqa-cli failed', 'Output did not contain a url. ' + output)
    return url


def fetch_url(url, request_type="text"):
    print("============== fetch_url")
    try:
        content = requests.get(url, headers={'User-Agent': USER_AGENT})
        content.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.error("Error while fetching %s: %s" % (url, str(e)))
        raise (e)
    raw = content.content
    if request_type == "json":
        try:
            content = content.json()
        except json.decoder.JSONDecodeError as e:
            log.error(
                "Error while decoding JSON from %s -> >>%s<<: %s"
                % (url, raw, str(e))
            )
            raise (e)
    else:
        content = raw
    return content


if __name__ == "__main__":
    args = parse_args()
    if args.simulate_review_requested_event:
        simulate(args)
    else:
        listen(args)
