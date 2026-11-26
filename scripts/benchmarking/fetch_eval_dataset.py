#!/usr/bin/env python3
# Copyright SUSE LLC
"""Fetch openQA jobs with LLM investigation comments and determine ground truth.

Queries openQA tests/list_ajax?comment=LLM and cross-references human review
actions (Bugzilla bsc#/boo# -> product bug, Progress poo# -> test issue,
infrastructure labels/tickets -> infrastructure problem) to assemble a
ground-truth evaluation dataset for model benchmarking.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_HOST = "https://openqa.opensuse.org"
USER_AGENT = "o3-llm-eval/1.0"
REQUEST_DELAY = 0.05  # Politeness delay between API queries
MIN_COMMENTS_FOR_HEURISTIC = 2
FAILED_RESULTS = frozenset({"failed", "incomplete", "timeout_exceeded", "parallel_failed"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_eval_dataset")

# Normalised classes
CLASS_PRODUCT = "product"
CLASS_TEST = "test"
CLASS_INFRA = "infra"

LLM_CLASS_PATTERN = re.compile(
    r"^[\s*_`>-]*Classification[*_`]*:[\s*_`]*(Product bug|Test issue|Infrastructure problem)\b",
    re.IGNORECASE | re.MULTILINE,
)
LLM_RESTART_PATTERN = re.compile(
    r"^[\s*_`>-]*(?:Automatic restart|Restart)[*_`]*:[\s*_`]*(YES|NO)\b",
    re.IGNORECASE | re.MULTILINE,
)

NORM_MAP = {
    "product bug": CLASS_PRODUCT,
    "product issue": CLASS_PRODUCT,
    "product": CLASS_PRODUCT,
    "test issue": CLASS_TEST,
    "test": CLASS_TEST,
    "infrastructure problem": CLASS_INFRA,
    "infrastructure": CLASS_INFRA,
    "infra": CLASS_INFRA,
}

BUGREF_PATTERN = re.compile(
    r"\b((?:bsc|boo|poo|gh#[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LLMVerdict:
    """Classification and restart advice as parsed from an LLM comment."""

    classification: str
    restart: str | None
    raw_text: str


@dataclass(frozen=True)
class GroundTruth:
    """The class a human assigned to a job, with its source and reference."""

    ground_truth_class: str
    source: str
    reference: str
    confidence: str  # 'high' (bugzilla/ticket link) or 'medium' (heuristic comment)


@dataclass
class EvalEntry:
    """One dataset row: a job, the LLM verdict and the human ground truth."""

    job_id: int
    test_name: str
    group_id: int | None
    result: str
    finished: str | None
    llm_verdict: str | None
    llm_restart: str | None
    ground_truth: str
    ground_truth_source: str
    reference: str
    is_match: bool


def get_json(url: str, timeout: float = 30.0) -> object:
    """Perform a GET request and parse JSON with explicit User-Agent."""
    req = urllib.request.Request(  # ruff: ignore[suspicious-url-open-usage]
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # ruff: ignore[suspicious-url-open-usage]
        return json.loads(resp.read().decode("utf-8"))


def parse_llm_comment(text: str) -> LLMVerdict | None:
    """Extract LLM classification and restart advice from a comment string."""
    if not text:
        return None
    match = LLM_CLASS_PATTERN.search(text)
    if not match:
        return None
    raw_class = match.group(1).lower()
    norm_class = NORM_MAP.get(raw_class, raw_class)

    restart_match = LLM_RESTART_PATTERN.search(text)
    restart = restart_match.group(1).upper() if restart_match else None

    return LLMVerdict(
        classification=norm_class,
        restart=restart,
        raw_text=text.strip(),
    )


def classify_bugref(ref: str) -> str | None:
    """Classify a bug reference string into product, test, or infra."""
    ref_lower = ref.lower()
    if ref_lower.startswith(("bsc#", "boo#")) or "bugzilla" in ref_lower:
        return CLASS_PRODUCT
    if ref_lower.startswith("poo#"):
        return CLASS_TEST
    return None


def extract_ground_truth(job_row: dict[str, Any], comments: list[dict[str, Any]]) -> GroundTruth | None:
    """Determine ground truth from job metadata and user comments.

    Checks:
    1. Bugs array in job comment_data (Bugzilla bsc#/boo# -> Product bug).
    2. Subsequent non-bot comments adding bugrefs (bsc# -> Product, poo# -> Test).
    3. Human override labels/comments.
    """
    # 1. Check comment_data bugs array
    comment_data = job_row.get("comment_data") or {}
    bugs = comment_data.get("bugs") or []
    for bug in bugs:
        content = bug.get("content", "")
        url = bug.get("url", "")
        classified = classify_bugref(content) or classify_bugref(url)
        if classified:
            return GroundTruth(
                ground_truth_class=classified,
                source="comment_data.bugs",
                reference=content or url,
                confidence="high",
            )

    # 2. Check comments for human-added bugrefs or labels
    bot_names = {"geekotest", "openqabot"}
    for comment in comments:
        user = comment.get("userName", "")
        text = str(comment.get("text") or "")

        # Skip bot's automated LLM comment when searching for human ground truth
        if user in bot_names and ("LLM Investigation summary" in text or "Classification:" in text):
            continue

        # Look for explicit bugrefs in comment text
        matches = BUGREF_PATTERN.findall(text)
        for ref in matches:
            classified = classify_bugref(ref)
            if classified:
                return GroundTruth(
                    ground_truth_class=classified,
                    source=f"comment_by_{user}",
                    reference=ref,
                    confidence="high",
                )

        # Look for infrastructure indicators in human comments
        if "infrastructure" in text.lower() and user not in bot_names:
            return GroundTruth(
                ground_truth_class=CLASS_INFRA,
                source=f"comment_by_{user}",
                reference=text.strip()[:120],
                confidence="medium",
            )

    return None


def fetch_jobs_batch(host: str, limit: int = 500, group_id: int | None = None) -> list[dict[str, Any]]:
    """Fetch finished jobs with LLM comments from tests/list_ajax."""
    params = {"comment": "LLM", "limit": str(limit)}
    if group_id is not None:
        params["groupid"] = str(group_id)
    url = f"{host.rstrip('/')}/tests/list_ajax?{urllib.parse.urlencode(params)}"
    logger.info("Fetching jobs list: %s", url)
    res = get_json(url)
    data = res.get("data", []) if isinstance(res, dict) else []
    return [job for job in data if isinstance(job, dict)] if isinstance(data, list) else []


def _job_sort_key(job: dict[str, Any]) -> tuple[bool, int]:
    comment_data = job.get("comment_data") or {}
    return (bool(comment_data.get("bugs")), comment_data.get("comments") or 0)


def _candidate_jobs(host: str, max_jobs: int, groups: list[int] | None) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    if groups:
        for gid in groups:
            jobs.extend(fetch_jobs_batch(host, limit=max_jobs, group_id=gid))
    else:
        jobs = fetch_jobs_batch(host, limit=max_jobs)
    logger.info("Retrieved %d candidate jobs from openQA.", len(jobs))
    jobs.sort(key=_job_sort_key, reverse=True)
    return jobs


def _comments_for_job(host: str, jid: int) -> list[dict[str, Any]]:
    """Return the comments of one job, or an empty list when they cannot be fetched."""
    url = f"{host.rstrip('/')}/api/v1/jobs/{jid}/comments"
    try:
        time.sleep(REQUEST_DELAY)
        comments = get_json(url)
    except urllib.error.URLError as err:
        logger.warning("Failed to fetch comments for job %d: %s", jid, err)
        return []
    return [comment for comment in comments if isinstance(comment, dict)] if isinstance(comments, list) else []


def _first_verdict(comments: list[dict[str, Any]]) -> LLMVerdict | None:
    for comment in comments:
        verdict = parse_llm_comment(comment.get("text", ""))
        if verdict:
            return verdict
    return None


def _entry_for_job(host: str, job: dict[str, Any]) -> EvalEntry | None:
    """Return the dataset row for one job, or None when it carries no ground truth."""
    jid = job.get("id")
    result = job.get("result", "")
    comment_data = job.get("comment_data") or {}
    carries_ground_truth = (
        bool(comment_data.get("bugs")) or (comment_data.get("comments") or 0) >= MIN_COMMENTS_FOR_HEURISTIC
    )
    if not jid or result not in FAILED_RESULTS or not carries_ground_truth:
        return None

    comments = _comments_for_job(host, jid)
    llm_verdict = _first_verdict(comments)
    if not llm_verdict:
        return None
    gt = extract_ground_truth(job, comments)
    if not gt:
        return None

    return EvalEntry(
        job_id=jid,
        test_name=job.get("test", ""),
        group_id=job.get("group"),
        result=result,
        finished=job.get("finished"),
        llm_verdict=llm_verdict.classification,
        llm_restart=llm_verdict.restart,
        ground_truth=gt.ground_truth_class,
        ground_truth_source=gt.source,
        reference=gt.reference,
        is_match=(llm_verdict.classification == gt.ground_truth_class),
    )


def build_dataset(
    host: str = DEFAULT_HOST,
    max_jobs: int = 500,
    groups: list[int] | None = None,
    min_entries: int = 50,
) -> list[EvalEntry]:
    """Retrieve jobs, fetch comments, extract LLM verdicts and ground truth."""
    eval_entries: list[EvalEntry] = []
    seen_job_ids: set[int] = set()

    for job in _candidate_jobs(host, max_jobs, groups):
        jid = job.get("id")
        if not jid or jid in seen_job_ids:
            continue
        seen_job_ids.add(jid)

        entry = _entry_for_job(host, job)
        if not entry:
            continue

        eval_entries.append(entry)
        logger.info(
            "Found entry #%d: Job %d (%s) -> LLM: %s | GT: %s [%s] -> Match: %s",
            len(eval_entries),
            jid,
            entry.test_name,
            entry.llm_verdict,
            entry.ground_truth,
            entry.reference,
            entry.is_match,
        )

        if len(eval_entries) >= min_entries:
            logger.info("Reached target minimum entries (%d). Stopping.", min_entries)
            break

    return eval_entries


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract historical o3 jobs with LLM comments and ground truth.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="openQA instance URL")
    parser.add_argument(
        "--output",
        default="eval_dataset.json",
        help="Output JSON file path (default: eval_dataset.json)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max candidate jobs to inspect from list_ajax (default: 500)",
    )
    parser.add_argument(
        "--min-entries",
        type=int,
        default=50,
        help="Minimum verified entries required (default: 50)",
    )
    parser.add_argument(
        "--groups",
        type=int,
        nargs="*",
        default=[1, 3, 38, 139],
        help="Job group IDs to query (default: 1 3 38 139)",
    )
    args = parser.parse_args()

    entries = build_dataset(
        host=args.host,
        max_jobs=args.limit,
        groups=args.groups,
        min_entries=args.min_entries,
    )

    if not entries:
        logger.error("No valid evaluation entries found.")
        return 1

    matches = sum(1 for e in entries if e.is_match)
    acc = (matches / len(entries)) * 100.0
    logger.info(
        "Extracted %d validated evaluation jobs. Current baseline accuracy: %d/%d (%.1f%%)",
        len(entries),
        matches,
        len(entries),
        acc,
    )

    payload = {
        "metadata": {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": args.host,
            "total_entries": len(entries),
            "baseline_matches": matches,
            "baseline_accuracy_pct": round(acc, 2),
        },
        "entries": [asdict(e) for e in entries],
    }

    with Path(args.output).open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    logger.info("Saved dataset to %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
