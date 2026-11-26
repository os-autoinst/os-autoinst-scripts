#!/usr/bin/env python3
# Copyright SUSE LLC
"""Audit the tickets behind the verdict dataset and propose their label class.

The v1 dataset derives every label from the tracker a bug reference lives in. This tool
resolves the referenced Bugzilla and Redmine tickets read-only, caches every answer so a
re-run does not hit the servers, and proposes the class that
`docs/research/verdict_label_policy.md` prescribes, with a confidence and a short rationale.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

import httpx

logger = logging.getLogger("audit_ticket_labels")

BUGZILLA_HOSTS = {
    "boo": "https://bugzilla.opensuse.org",
    "bsc": "https://bugzilla.suse.com",
}
REDMINE_HOST = "https://progress.opensuse.org"
REDMINE_CONFIG = pathlib.Path.home() / ".query_redminerc"
CACHE_DIR = pathlib.Path.home() / ".cache" / "o3-config" / "audit_ticket_labels"
USER_AGENT = "o3-llm-eval-label-audit/1.0"
REQUEST_TIMEOUT = 30.0
REQUEST_DELAY = 1.0
RETRY_STATUS = frozenset({429, 503})
RETRY_DELAY = 5.0
MAX_ATTEMPTS = 5
MAX_RETRY_DELAY = 60.0
COMMENT_LIMIT = 3
COMMENT_CHARS = 1500
BUGZILLA_FIELDS = "id,summary,status,resolution,product,component"

NON_TICKET_TRACKERS = frozenset({"github", "other"})
DEFAULT_MAX_PER_TICKET = 5

REFERENCE_RE = re.compile(r"^(?P<tracker>boo|bsc|poo)#(?P<id>\d+)$", re.IGNORECASE)
GITHUB_RE = re.compile(r"^gh#(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$", re.IGNORECASE)

CLOSED_WITHOUT_CONCLUSION = frozenset({
    "RESOLVED/WONTFIX",
    "RESOLVED/INVALID",
    "RESOLVED/NOTABUG",
    "RESOLVED/DUPLICATE",
    "RESOLVED/MOVED",
    "CLOSED/WONTFIX",
    "REJECTED",
})
CONFIRMED_STATES = frozenset({"NEW", "ASSIGNED", "IN_PROGRESS", "IN PROGRESS", "CONFIRMED", "RESOLVED/FIXED"})

INFRA_PATTERN = re.compile(
    r"\b(?:worker|openqaworker(?:\d+)?|network outage|network down|dns|mirror|"
    r"external (?:server|service)|power supply|infrastructure)\b",
    re.IGNORECASE,
)
TEST_FILE_PATTERN = re.compile(r"\b[\w/-]+\.(?:pm|py)\b|\b(?:schedule|data)/[\w./-]+\.yaml\b", re.IGNORECASE)
TEST_CODE_PATTERN = re.compile(
    r"(?:\b[\w/-]+\.(?:pm|py)\b|\b(?:schedule|data)/[\w./-]+\.yaml\b|\bneedles?\b|\bmatch_has_tag\b|"
    r"test code|test data|wait_screen_change|send_key|os-autoinst-distri|bootloader_setup)",
    re.IGNORECASE,
)
ADAPT_PATTERN = re.compile(
    r"(?:the test (?:needs|must|has) to|test needs to|needs to (?:press|click|adapt)|"
    r"test has to (?:press|click|adapt)|press (?:it|the key) differently|"
    r"(?:test )?module needs work|needs work to|adapt (?:the )?(?:test|code)|"
    r"test module needs|update the test|test needs (?:updating|adjusting)|"
    r"(?:is|are) no longer (?:to )?(?:be )?(?:pre-?installed|available|shipped|the default)|"
    r"no longer (?:to )?(?:be )?(?:pre-?installed|preinstalled|available|shipped|supported)|"
    r"no longer (?:to )?(?:detect|find|match|detects|finds|matches))",
    re.IGNORECASE,
)
UNCERTAINTY_PATTERN = re.compile(
    r"(?:needs? to be investigated|not (?:yet )?clear|unclear|no idea|unknown cause|"
    r"may be a (?:product|worker|infra)|could be (?:a )?(?:product|worker|infra))",
    re.IGNORECASE,
)
PRODUCT_PATTERN = re.compile(
    r"\b(?:kernel panic|panic(?:s|ked)?|segfault|crash(?:es|ed)?|regression|broken|corrupt(?:ed)?|"
    r"fails? to|does not (?:work|exit|boot|start)|incomplete|denials?|timeout|hang(?:s)?|"
    r"signal \d+|terminates on|failed to load|avc denied|audit denied|conflicts? with|"
    r"helper failed|cannot be provided)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Reference:
    """A bug reference as a job comment carries it."""

    raw: str
    tracker: str
    tracker_id: str | None
    ticket_id: int | None


@dataclass(frozen=True)
class Ticket:
    """The read-only ticket facts the label policy needs."""

    reference: str
    tracker: str
    state: str
    summary: str
    area: str
    description: str
    comments: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class Proposal:
    """The policy's class for one reference, with its evidence."""

    reference: str
    tracker: str
    state: str
    summary: str
    area: str
    label: str
    confidence: str
    rationale: str
    source: str


def parse_reference(raw: str) -> Reference:
    """Split a reference into tracker and ticket id; unknown shapes stay unresolved."""
    text = raw.strip()
    match = REFERENCE_RE.match(text)
    if match is None:
        github = GITHUB_RE.match(text)
        tracker = "github" if github else "other"
        return Reference(raw=text, tracker=tracker, tracker_id=None, ticket_id=None)
    tracker_id = match.group("tracker").lower()
    return Reference(
        raw=text,
        tracker="bugzilla" if tracker_id in BUGZILLA_HOSTS else "redmine",
        tracker_id=tracker_id,
        ticket_id=int(match.group("id")),
    )


def _proposal(ticket: Ticket, label: str, confidence: str, rationale: str, source: str = "api") -> Proposal:
    return Proposal(
        reference=ticket.reference,
        tracker=ticket.tracker,
        state=ticket.state,
        summary=ticket.summary,
        area=ticket.area,
        label=label,
        confidence=confidence,
        rationale=rationale,
        source=source,
    )


def propose_label(ticket: Ticket, source: str = "api") -> Proposal:
    """Apply the written policy to one ticket's content and state."""
    label, confidence, rationale = _classify(ticket)
    return _proposal(ticket, label, confidence, rationale, source)


def _unclear_reason(ticket: Ticket) -> str | None:
    """Return why the policy cannot classify the ticket at all, or None."""
    if ticket.tracker == "github":
        return "GitHub issue, while the audit reads Bugzilla and Redmine only"
    if ticket.tracker == "other":
        return "reference is not a ticket this audit can read"
    if ticket.state.upper() in CLOSED_WITHOUT_CONCLUSION:
        return f"closed {ticket.state} without a product or test conclusion"
    return None


def _test_verdict(evidence: str) -> tuple[str, str, str] | None:
    """Return the test issue verdict when the ticket names test code or a behaviour the test must absorb."""
    test_code = TEST_CODE_PATTERN.search(evidence)
    if test_code:
        confidence = "certain" if TEST_FILE_PATTERN.search(evidence) else "plausible"
        return "test", confidence, f"names test code or test data: {test_code.group(0)}"
    adapt = ADAPT_PATTERN.search(evidence)
    if adapt:
        return "test", "plausible", f"product behaviour change the test suite must absorb: {adapt.group(0)}"
    return None


def _classify(ticket: Ticket) -> tuple[str, str, str]:
    """Decide label, confidence and rationale by the policy's rule order."""
    if reason := _unclear_reason(ticket):
        return "unclear", "unclear", reason

    state = ticket.state.upper()
    evidence = " ".join((ticket.summary, ticket.description, *ticket.comments))
    infra = INFRA_PATTERN.search(evidence)
    test_verdict = _test_verdict(evidence)
    product = PRODUCT_PATTERN.search(evidence)
    if uncertainty := UNCERTAINTY_PATTERN.search(evidence):
        return "unclear", "unclear", f"the ticket leaves the cause open: {uncertainty.group(0)}"
    if infra and not (test_verdict or product):
        return "infra", "plausible", f"names only an infrastructure fault: {infra.group(0)}"
    if test_verdict:
        return test_verdict
    if product:
        confidence = "certain" if state in CONFIRMED_STATES else "plausible"
        return "product", confidence, f"names a product defect: {product.group(0)} (state {ticket.state})"
    return "unclear", "unclear", "ticket content names neither a product nor a test defect"


def load_redmine_token(config: pathlib.Path = REDMINE_CONFIG) -> str | None:
    """Read the Redmine API token from the environment or the fetcher config."""
    token = os.environ.get("REDMINE_API_TOKEN")
    if token:
        return token
    if not config.exists():
        return None
    for line in config.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "redmine-token" and value.strip():
            return value.strip()
    return None


def _cache_path(directory: pathlib.Path, reference: Reference) -> pathlib.Path:
    return directory / f"{reference.tracker_id}_{reference.ticket_id}.json"


def load_cached(directory: pathlib.Path, reference: Reference) -> Ticket | None:
    """Return the cached ticket of a reference, if the audit already resolved it."""
    path = _cache_path(directory, reference)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        return Ticket(
            reference=str(payload["reference"]),
            tracker=str(payload["tracker"]),
            state=str(payload["state"]),
            summary=str(payload["summary"]),
            area=str(payload["area"]),
            description=str(payload["description"]),
            comments=tuple(str(note) for note in payload["comments"]),
            url=str(payload["url"]),
        )
    except KeyError:
        logger.warning("Ignoring the cache entry %s: it predates the current record format", path.name)
        return None


def store_cached(directory: pathlib.Path, reference: Reference, ticket: Ticket) -> None:
    """Write one ticket's facts to the cache so a re-run does not query the servers."""
    directory.mkdir(parents=True, exist_ok=True)
    _cache_path(directory, reference).write_text(json.dumps(asdict(ticket), indent=1), encoding="utf-8")


def _bugzilla_state(bug: dict[str, Any]) -> str:
    status = str(bug.get("status") or "UNKNOWN")
    resolution = str(bug.get("resolution") or "")
    return f"{status}/{resolution}" if resolution else status


def _short_comments(texts: list[str]) -> tuple[str, ...]:
    return tuple(text.strip()[:COMMENT_CHARS] for text in texts[:COMMENT_LIMIT] if text.strip())


def _bugzilla_comments(client: httpx.Client, host: str, ticket_id: int) -> tuple[str, ...]:
    payload = request_json(
        client,
        f"{host}/rest/bug/{ticket_id}/comment",
        params={"include_fields": "id,text,creator,creation_time"},
        headers={"User-Agent": USER_AGENT},
    )
    time.sleep(REQUEST_DELAY)
    comments = payload.get("bugs", {}).get(str(ticket_id), {}).get("comments", [])
    return _short_comments([str(comment.get("text") or "") for comment in comments])


def fetch_bugzilla_tickets(
    client: httpx.Client,
    refs: list[Reference],
    cache_dir: pathlib.Path,
    *,
    refresh: bool = False,
) -> dict[str, tuple[Ticket, str]]:
    """Resolve Bugzilla references with one batched bug request and one comment request each."""
    resolved: dict[str, tuple[Ticket, str]] = {}
    pending: dict[str, list[Reference]] = {}
    for ref in refs:
        cached = None if refresh else load_cached(cache_dir, ref)
        if cached is not None:
            resolved[ref.raw] = (cached, "cache")
        else:
            pending.setdefault(str(ref.tracker_id), []).append(ref)

    for tracker_id, host_refs in pending.items():
        host = BUGZILLA_HOSTS[tracker_id]
        ids = ",".join(str(ref.ticket_id) for ref in host_refs)
        payload = request_json(
            client,
            f"{host}/rest/bug",
            params={"id": ids, "include_fields": BUGZILLA_FIELDS},
            headers={"User-Agent": USER_AGENT},
        )
        bugs = {int(bug["id"]): bug for bug in payload.get("bugs", [])}
        for ref in host_refs:
            bug = bugs.get(ref.ticket_id)
            if bug is None:
                logger.warning("Bugzilla has no ticket for %s", ref.raw)
                continue
            ticket = Ticket(
                reference=ref.raw,
                tracker="bugzilla",
                state=_bugzilla_state(bug),
                summary=str(bug.get("summary") or ""),
                area="/".join(part for part in (bug.get("product"), bug.get("component")) if part),
                description="",
                comments=_bugzilla_comments(client, host, int(bug["id"])),
                url=f"{host}/show_bug.cgi?id={ref.ticket_id}",
            )
            store_cached(cache_dir, ref, ticket)
            resolved[ref.raw] = (ticket, "api")
    return resolved


def request_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Get a URL as JSON, backing off on the tracker's rate-limit and busy answers."""
    delay = RETRY_DELAY
    attempt = 1
    while True:
        response = client.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code not in RETRY_STATUS:
            response.raise_for_status()
            return response.json()
        if attempt == MAX_ATTEMPTS:
            msg = f"{url} still answers {response.status_code} after {MAX_ATTEMPTS} attempts"
            raise RuntimeError(msg)
        logger.warning("%s answered %d, retrying in %.0f s", url, response.status_code, delay)
        time.sleep(delay)
        delay = min(delay * 2, MAX_RETRY_DELAY)
        attempt += 1


def _redmine_headers(token: str | None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["X-Redmine-API-Key"] = token
    return headers


def fetch_redmine_tickets(
    client: httpx.Client,
    refs: list[Reference],
    cache_dir: pathlib.Path,
    token: str | None,
    *,
    refresh: bool = False,
) -> dict[str, tuple[Ticket, str]]:
    """Resolve Redmine references with one sequential request per ticket."""
    resolved: dict[str, tuple[Ticket, str]] = {}
    for ref in refs:
        cached = None if refresh else load_cached(cache_dir, ref)
        if cached is not None:
            resolved[ref.raw] = (cached, "cache")
            continue
        payload = request_json(
            client,
            f"{REDMINE_HOST}/issues/{ref.ticket_id}.json",
            params={"include": "journals"},
            headers=_redmine_headers(token),
        )
        time.sleep(REQUEST_DELAY)
        issue = payload.get("issue", {})
        notes = [str(journal.get("notes") or "") for journal in issue.get("journals", [])]
        ticket = Ticket(
            reference=ref.raw,
            tracker="redmine",
            state=str((issue.get("status") or {}).get("name") or "unknown"),
            summary=str(issue.get("subject") or ""),
            area=str((issue.get("project") or {}).get("name") or ""),
            description=str(issue.get("description") or "")[:COMMENT_CHARS],
            comments=_short_comments(notes),
            url=f"{REDMINE_HOST}/issues/{ref.ticket_id}",
        )
        store_cached(cache_dir, ref, ticket)
        resolved[ref.raw] = (ticket, "api")
    return resolved


def collect_references(
    raw: list[str],
    *,
    dataset_paths: list[pathlib.Path] | None = None,
    inventory_path: pathlib.Path | None = None,
) -> list[str]:
    """Collect, deduplicate and order the references the audit has to resolve."""
    references = [item.strip() for item in raw if item.strip()]
    for path in dataset_paths or []:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        references.extend(str(entry["reference"]) for entry in entries if entry.get("reference"))
    if inventory_path is not None:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        references.extend(inventory.get("references", {}).keys())
    return list(dict.fromkeys(references))


def _blank_ticket(ref: Reference) -> Ticket:
    """Stand in for a reference the audit could not resolve."""
    return Ticket(
        reference=ref.raw,
        tracker=ref.tracker,
        state="unknown",
        summary="",
        area="",
        description="",
        comments=(),
        url="",
    )


def audit_references(
    references: list[str],
    *,
    client: httpx.Client,
    cache_dir: pathlib.Path,
    token: str | None = None,
    refresh: bool = False,
) -> list[Proposal]:
    """Resolve every reference and propose its class, keeping the input order."""
    parsed = [parse_reference(item) for item in references]
    bugzilla = [ref for ref in parsed if ref.tracker == "bugzilla"]
    redmine = [ref for ref in parsed if ref.tracker == "redmine"]

    resolved = fetch_bugzilla_tickets(client, bugzilla, cache_dir, refresh=refresh)
    resolved.update(fetch_redmine_tickets(client, redmine, cache_dir, token, refresh=refresh))

    proposals: list[Proposal] = []
    for ref in parsed:
        found = resolved.get(ref.raw)
        if found is not None:
            ticket, source = found
            proposals.append(propose_label(ticket, source))
        elif ref.tracker in NON_TICKET_TRACKERS:
            proposals.append(propose_label(_blank_ticket(ref), "missing"))
        else:
            proposals.append(_proposal(_blank_ticket(ref), "unclear", "unclear", "ticket could not be read", "missing"))
    return proposals


def _row(
    entry: dict[str, Any], reference: str, proposal: Proposal | None, verdict: tuple[str, str, str]
) -> dict[str, Any]:
    label, confidence, rationale = verdict
    return {
        "job_id": int(entry["job_id"]),
        "test_name": str(entry.get("test_name") or ""),
        "reference": reference,
        "ticket_state": proposal.state if proposal else "unknown",
        "label": label,
        "confidence": confidence,
        "rationale": rationale,
        "scored": label != "unclear",
    }


def annotate_dataset(
    entries: list[dict[str, Any]],
    proposals: list[Proposal],
    *,
    max_per_ticket: int = DEFAULT_MAX_PER_TICKET,
    force_unclear: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Join the corpus rows with their ticket proposals into the v2 dataset."""
    by_reference = {proposal.reference.lower(): proposal for proposal in proposals}
    capped: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for entry in sorted(entries, key=lambda item: (str(item["reference"]).lower(), int(item["job_id"]))):
        reference = str(entry["reference"])
        key = reference.lower()
        if capped.get(key, 0) >= max_per_ticket:
            continue
        capped[key] = capped.get(key, 0) + 1
        proposal = by_reference.get(key)
        if proposal is None:
            rows.append(_row(entry, reference, None, ("unclear", "unclear", "ticket could not be read")))
        elif key in force_unclear:
            rationale = f"contradicts a sibling ticket, unscored until a human rules: {proposal.rationale}"
            rows.append(_row(entry, reference, proposal, ("unclear", "unclear", rationale)))
        else:
            rows.append(_row(entry, reference, proposal, (proposal.label, proposal.confidence, proposal.rationale)))

    labels = Counter(row["label"] for row in rows if row["scored"])
    confidences = Counter(row["confidence"] for row in rows if row["scored"])
    tickets = {row["reference"] for row in rows}
    scored_tickets = {row["reference"] for row in rows if row["scored"]}
    scored_rows = sum(labels.values())
    per_ticket = Counter(row["reference"] for row in rows)
    return {
        "metadata": {
            "rows": len(rows),
            "scored_rows": scored_rows,
            "unscored_rows": len(rows) - scored_rows,
            "tickets": len(tickets),
            "scored_tickets": len(scored_tickets),
            "max_per_ticket": max_per_ticket,
            "force_unclear": sorted(force_unclear),
            "label_counts": dict(sorted(labels.items())),
            "confidence_counts": dict(sorted(confidences.items())),
            "all_product_ceiling_pct": round(labels.get("product", 0) / scored_rows * 100.0, 1) if scored_rows else 0.0,
            "rows_per_ticket": dict(per_ticket.most_common()),
        },
        "entries": rows,
    }


def load_entries(path: pathlib.Path) -> list[dict[str, Any]]:
    """Read the entries of a corpus dataset."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("entries", []) if isinstance(payload, dict) else payload


def write_payload(payload: object, path: pathlib.Path | None) -> None:
    """Write a JSON payload to `path`, or to stdout when it is None."""
    text = json.dumps(payload, indent=1)
    if path is None:
        sys.stdout.write(text + "\n")
        return
    path.write_text(text + "\n", encoding="utf-8")


def write_json(proposals: list[Proposal], path: pathlib.Path | None) -> None:
    """Write the proposals as a JSON array to `path`, or to stdout when it is None."""
    write_payload([asdict(proposal) for proposal in proposals], path)


def main() -> int:
    """Run the audit and write one record per reference."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        description="Audit the tickets behind the verdict dataset and propose their class."
    )
    parser.add_argument("--references", nargs="*", default=[], help="Bug references such as boo#1289464 or poo#209382")
    parser.add_argument(
        "--from-dataset", action="append", type=pathlib.Path, help="Dataset JSON to take references from"
    )
    parser.add_argument("--from-inventory", type=pathlib.Path, help="Inventory JSON to take references from")
    parser.add_argument("--output", type=pathlib.Path, help="Write the proposals here instead of stdout")
    parser.add_argument(
        "--cache-dir", type=pathlib.Path, default=CACHE_DIR, help=f"API answer cache (default: {CACHE_DIR})"
    )
    parser.add_argument("--refresh", action="store_true", help="Ignore the cache and query the servers again")
    parser.add_argument(
        "--annotate-dataset",
        type=pathlib.Path,
        help="Write the audited v2 dataset for this corpus dataset instead of the bare proposals",
    )
    parser.add_argument(
        "--max-per-ticket",
        type=int,
        default=DEFAULT_MAX_PER_TICKET,
        help=f"Rows the annotated dataset keeps per ticket (default: {DEFAULT_MAX_PER_TICKET})",
    )
    parser.add_argument(
        "--force-unclear",
        action="append",
        default=[],
        help="Reference of a contradiction pair, unscored until a human rules (repeatable)",
    )
    args = parser.parse_args()

    references = collect_references(
        args.references,
        dataset_paths=args.from_dataset,
        inventory_path=args.from_inventory,
    )
    if not references:
        parser.error("no references given; use --references, --from-dataset or --from-inventory")

    with httpx.Client() as client:
        proposals = audit_references(
            references,
            client=client,
            cache_dir=args.cache_dir,
            token=load_redmine_token(),
            refresh=args.refresh,
        )
    if args.annotate_dataset is not None:
        payload = annotate_dataset(
            load_entries(args.annotate_dataset),
            proposals,
            max_per_ticket=args.max_per_ticket,
            force_unclear=frozenset(reference.lower() for reference in args.force_unclear),
        )
        write_payload(payload, args.output)
        logger.info("Annotated dataset: %s", payload["metadata"])
        return 0

    write_json(proposals, args.output)

    counts = {
        label: sum(proposal.label == label for proposal in proposals)
        for label in ("product", "test", "infra", "unclear")
    }
    logger.info("Audited %d references: %s", len(proposals), counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
