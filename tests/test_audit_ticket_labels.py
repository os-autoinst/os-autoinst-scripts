# Copyright SUSE LLC
"""Unit tests for audit_ticket_labels."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from scripts.benchmarking import audit_ticket_labels as audit
from scripts.benchmarking.audit_ticket_labels import (
    Ticket,
    audit_references,
    collect_references,
    fetch_bugzilla_tickets,
    fetch_redmine_tickets,
    load_cached,
    load_redmine_token,
    main,
    parse_reference,
    propose_label,
    store_cached,
    write_json,
)

if TYPE_CHECKING:
    import pathlib

Handler = Callable[[httpx.Request], httpx.Response]


def client_for(handler: Handler) -> httpx.Client:
    """Build a client answering from `handler` instead of the network."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def ticket_facts(
    reference: str = "poo#209382",
    tracker: str = "redmine",
    state: str = "In Progress",
    summary: str = "",
    comments: tuple[str, ...] = (),
    description: str = "",
) -> Ticket:
    """Build the ticket facts a label proposal needs."""
    return Ticket(
        reference=reference,
        tracker=tracker,
        state=state,
        summary=summary,
        area="openQA Tests",
        description=description,
        comments=comments,
        url="https://example.test/ticket",
    )


BUGZILLA_BUG = {
    "id": 1288617,
    "summary": "SELinux denials block the service",
    "status": "RESOLVED",
    "resolution": "FIXED",
    "product": "openSUSE Tumbleweed",
    "component": "SELinux",
}


REFERENCE_CASES = [
    {
        "desc": "boo reference resolves to the openSUSE Bugzilla tracker",
        "raw": "boo#1289464",
        "expected": ("bugzilla", "boo", 1289464),
    },
    {
        "desc": "bsc reference keeps its case-insensitive tracker",
        "raw": "BSC#1290026",
        "expected": ("bugzilla", "bsc", 1290026),
    },
    {
        "desc": "poo reference resolves to Redmine",
        "raw": "poo#209382",
        "expected": ("redmine", "poo", 209382),
    },
    {
        "desc": "GitHub reference has no tracker this audit can read",
        "raw": "gh#os-autoinst/openQA#7974",
        "expected": ("other", None, None),
    },
    {
        "desc": "label-shaped reference is not a ticket",
        "raw": "bootloader_uefi#1",
        "expected": ("other", None, None),
    },
]


@pytest.mark.parametrize("case", REFERENCE_CASES, ids=[case["desc"] for case in REFERENCE_CASES])
def test_parse_reference(case: dict[str, Any]) -> None:
    reference = parse_reference(case["raw"])
    assert (reference.tracker, reference.tracker_id, reference.ticket_id) == case["expected"]


LABEL_CASES = [
    {
        "desc": "line-move in a test module is a certain test issue",
        "ticket": ticket_facts(
            reference="poo#209382",
            summary="Bug at os-autoinst-distri-opensuse's lib/bootloader_setup.pm:562",
        ),
        "expected": ("test", "certain"),
    },
    {
        "desc": "product behaviour change the test must absorb is a test issue",
        "ticket": ticket_facts(
            reference="boo#1289464",
            tracker="bugzilla",
            state="IN_PROGRESS",
            summary="openQA test fails in boot_to_desktop, the esc key does not exit the menu",
            comments=("the test needs to press it differently",),
        ),
        "expected": ("test", "plausible"),
    },
    {
        "desc": "fixed product defect is a certain product bug",
        "ticket": ticket_facts(
            reference="boo#1288617",
            tracker="bugzilla",
            state="RESOLVED/FIXED",
            summary="SELinux denials block the service",
        ),
        "expected": ("product", "certain"),
    },
    {
        "desc": "in-progress product defect stays a certain product bug",
        "ticket": ticket_facts(
            reference="boo#1246013",
            tracker="bugzilla",
            state="IN_PROGRESS",
            summary="MicroOS setup-fips with BLS leaves setup incomplete",
        ),
        "expected": ("product", "certain"),
    },
    {
        "desc": "WONTFIX closure without a conclusion is unscored",
        "ticket": ticket_facts(
            reference="boo#1289038",
            tracker="bugzilla",
            state="RESOLVED/WONTFIX",
            summary="AutoYaST clone fails, no logs available",
        ),
        "expected": ("unclear", "unclear"),
    },
    {
        "desc": "rejected Redmine ticket is unscored",
        "ticket": ticket_facts(state="Rejected", summary="worker outage"),
        "expected": ("unclear", "unclear"),
    },
    {
        "desc": "only an infrastructure fault is an infrastructure problem",
        "ticket": ticket_facts(state="New", summary="network outage on the worker, DNS lookup returns nothing"),
        "expected": ("infra", "plausible"),
    },
    {
        "desc": "an infrastructure hint next to a product defect does not win",
        "ticket": ticket_facts(
            tracker="bugzilla",
            state="IN_PROGRESS",
            summary="worker killed the job while the tested system panicked",
        ),
        "expected": ("product", "certain"),
    },
    {
        "desc": "content naming neither defect class is unscored",
        "ticket": ticket_facts(state="New", summary="test fails in opensuse_welcome"),
        "expected": ("unclear", "unclear"),
    },
    {
        "desc": "reference that is not a ticket is unscored",
        "ticket": ticket_facts(reference="kde#502830", tracker="other"),
        "expected": ("unclear", "unclear"),
    },
]


@pytest.mark.parametrize("case", LABEL_CASES, ids=[case["desc"] for case in LABEL_CASES])
def test_propose_label(case: dict[str, Any]) -> None:
    proposal = propose_label(case["ticket"])

    assert (proposal.label, proposal.confidence) == case["expected"]
    assert proposal.rationale
    assert proposal.reference == case["ticket"].reference


def test_cache_roundtrip(tmp_path: pathlib.Path) -> None:
    ticket = ticket_facts(summary="cached ticket", comments=("first note",))
    reference = parse_reference(ticket.reference)

    assert load_cached(tmp_path, reference) is None

    store_cached(tmp_path, reference, ticket)

    assert load_cached(tmp_path, reference) == ticket


def test_load_cached_ignores_an_older_record_format(tmp_path: pathlib.Path) -> None:
    reference = parse_reference("boo#1")
    (tmp_path / "boo_1.json").write_text(json.dumps({"reference": "boo#1", "tracker": "bugzilla"}), encoding="utf-8")

    assert load_cached(tmp_path, reference) is None


def test_fetch_bugzilla_tickets_batches_reuses_and_refreshes(tmp_path: pathlib.Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/rest/bug":
            return httpx.Response(200, json={"bugs": [BUGZILLA_BUG]})
        return httpx.Response(
            200,
            json={"bugs": {"1288617": {"comments": [{"text": f"note {index}"} for index in range(5)]}}},
        )

    reference = "boo#1288617"
    parsed = [parse_reference(reference)]
    with client_for(handler) as client:
        resolved = fetch_bugzilla_tickets(client, parsed, tmp_path)

    fetched, source = resolved[reference]
    assert source == "api"
    assert (fetched.state, fetched.area) == ("RESOLVED/FIXED", "openSUSE Tumbleweed/SELinux")
    assert fetched.comments == ("note 0", "note 1", "note 2")
    assert [request.url.path for request in requests] == ["/rest/bug", "/rest/bug/1288617/comment"]

    with client_for(handler) as client:
        cached = fetch_bugzilla_tickets(client, parsed, tmp_path)

    assert cached[reference] == (fetched, "cache")
    assert len(requests) == 2

    with client_for(handler) as client:
        refreshed = fetch_bugzilla_tickets(client, parsed, tmp_path, refresh=True)

    assert refreshed[reference][1] == "api"
    assert len(requests) == 4


def test_fetch_bugzilla_tickets_skips_ids_the_tracker_does_not_know(tmp_path: pathlib.Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bugs": []})

    with client_for(handler) as client:
        resolved = fetch_bugzilla_tickets(client, [parse_reference("boo#1")], tmp_path)

    assert resolved == {}


def test_fetch_redmine_tickets_sends_the_token_and_drops_empty_notes(tmp_path: pathlib.Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "issue": {
                    "id": 209382,
                    "subject": "Bug at lib/bootloader_setup.pm:562",
                    "status": {"name": "In Progress"},
                    "project": {"name": "openQA Tests"},
                    "description": "the line move broke grub",
                    "journals": [{"notes": ""}, {"notes": "pull request 27293 fixes it"}],
                }
            },
        )

    with client_for(handler) as client:
        resolved = fetch_redmine_tickets(client, [parse_reference("poo#209382")], tmp_path, "secret-token")

    ticket, source = resolved["poo#209382"]
    assert source == "api"
    assert (ticket.state, ticket.area) == ("In Progress", "openQA Tests")
    assert ticket.description == "the line move broke grub"
    assert ticket.comments == ("pull request 27293 fixes it",)
    assert seen[0].headers["x-redmine-api-key"] == "secret-token"
    assert seen[0].url.path == "/issues/209382.json"

    with client_for(handler) as client:
        cached = fetch_redmine_tickets(client, [parse_reference("poo#209382")], tmp_path, "secret-token")

    assert cached["poo#209382"] == (ticket, "cache")
    assert len(seen) == 1


def test_fetch_redmine_tickets_without_a_token(tmp_path: pathlib.Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-redmine-api-key" not in request.headers
        return httpx.Response(200, json={"issue": {"id": 209751, "status": {"name": "New"}}})

    with client_for(handler) as client:
        resolved = fetch_redmine_tickets(client, [parse_reference("poo#209751")], tmp_path, None)

    assert resolved["poo#209751"][0].state == "New"


TOKEN_CASES = [
    {
        "desc": "environment variable wins over the config file",
        "env": "from-env",
        "config": "redmine-token=from-file\n",
        "expected": "from-env",
    },
    {
        "desc": "config file supplies the token without the environment",
        "env": None,
        "config": "redmine-host=https://progress.opensuse.org\nredmine-token=from-file\n",
        "expected": "from-file",
    },
    {"desc": "missing config file yields no token", "env": None, "config": None, "expected": None},
    {
        "desc": "config file without a token yields none",
        "env": None,
        "config": "redmine-host=x\n",
        "expected": None,
    },
]


@pytest.mark.parametrize("case", TOKEN_CASES, ids=[case["desc"] for case in TOKEN_CASES])
def test_load_redmine_token(case: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if case["env"] is None:
        monkeypatch.delenv("REDMINE_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("REDMINE_API_TOKEN", case["env"])
    config = tmp_path / "query_redminerc"
    if case["config"] is not None:
        config.write_text(case["config"], encoding="utf-8")

    assert load_redmine_token(config) == case["expected"]


def test_collect_references_deduplicates_across_sources(tmp_path: pathlib.Path) -> None:
    dataset = tmp_path / "eval_dataset.json"
    dataset.write_text(
        json.dumps({"entries": [{"reference": "poo#209382"}, {"reference": "boo#1289464"}, {"job_id": 1}]}),
        encoding="utf-8",
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"references": {"boo#1289464": 21, "poo#206028": 40}}), encoding="utf-8")

    references = collect_references(
        ["poo#209382", " ", "poo#206028"],
        dataset_paths=[dataset],
        inventory_path=inventory,
    )

    assert references == ["poo#209382", "poo#206028", "boo#1289464"]


def test_audit_references_keeps_order_and_marks_unreadable_ones(tmp_path: pathlib.Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/bug":
            return httpx.Response(200, json={"bugs": [BUGZILLA_BUG]})
        if request.url.path == "/rest/bug/1288617/comment":
            return httpx.Response(200, json={"bugs": {"1288617": {"comments": []}}})
        if request.url.path == "/issues/209382.json":
            return httpx.Response(
                200,
                json={"issue": {"status": {"name": "In Progress"}, "subject": "lib/bootloader_setup.pm:562"}},
            )
        return httpx.Response(404)

    references = ["poo#209382", "boo#1288617", "kde#502830", "boo#1289038"]
    with client_for(handler) as client:
        proposals = audit_references(
            references,
            client=client,
            cache_dir=tmp_path,
            token="secret-token",
        )

    assert [proposal.reference for proposal in proposals] == references
    assert [proposal.label for proposal in proposals] == ["test", "product", "unclear", "unclear"]
    assert proposals[2].rationale == "reference is not a ticket this audit can read"
    assert proposals[3].source == "missing"
    assert "secret-token" not in json.dumps([proposal.__dict__ for proposal in proposals])


def test_write_json_writes_a_file_or_stdout(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    proposals = [propose_label(ticket_facts())]
    output = tmp_path / "proposals.json"

    write_json(proposals, output)
    write_json(proposals, None)

    assert json.loads(output.read_text(encoding="utf-8"))[0]["reference"] == "poo#209382"
    assert json.loads(capsys.readouterr().out)[0]["label"] == "unclear"


def test_main_writes_one_record_per_reference(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker: Any,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"issue": {"status": {"name": "In Progress"}, "subject": "lib/bootloader_setup.pm:562"}},
        )

    output = tmp_path / "proposals.json"
    monkeypatch.setenv("REDMINE_API_TOKEN", "secret-token")
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_ticket_labels.py", "--references", "poo#209382", "--output", str(output), "--cache-dir", str(tmp_path)],
    )
    mocker.patch.object(audit.httpx, "Client", return_value=client_for(handler))

    assert main() == 0

    proposals = json.loads(output.read_text(encoding="utf-8"))
    assert [proposal["reference"] for proposal in proposals] == ["poo#209382"]
    assert proposals[0]["label"] == "test"


def test_main_requires_a_reference_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["audit_ticket_labels.py"])

    with pytest.raises(SystemExit):
        main()
