#!/usr/bin/env python3
# Copyright SUSE LLC
"""Run openqa-llm-investigate --dry over an evaluation dataset and score the verdicts.

Each job is investigated through its openQA test URL. The classification is read from the
comment that --dry prints on stdout, in either the header form or the classification-line
form, and compared with the ground truth and with the historical baseline verdict recorded
in the dataset.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from scripts.benchmarking.score_verdicts import (  # ruff: ignore[module-import-not-at-top-of-file]
    CLASS_LABEL_PATTERN,
    CLASSIFICATION_LINE_RE,
    LABEL_TO_CLASS,
    ScoreReport,
    compute_metrics,
    format_markdown_report,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_investigate_eval")

OPENQA_TEST_URL = "https://openqa.opensuse.org/tests/{job_id}"
DEFAULT_SCRIPT_PATH = str(repo_root / "openqa-llm-investigate")
DEFAULT_DATASET = str(repo_root / "tests" / "data" / "eval_dataset.json")
DEFAULT_JOB_TIMEOUT_SEC = 900
DEFAULT_LLM_TIMEOUT_SEC = 600
DEFAULT_RETRIES = 1
SUBPROCESS_GRACE_SEC = 60
COMMENT_MARKER_RE = re.compile(r"\*\*LLM Investigation summary:\*\*")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
HEADER_RE = re.compile(
    rf"^\*\*LLM Investigation summary:\*\*[ \t]*{CLASS_LABEL_PATTERN}[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class JobResult:
    """One job's verdict, compared with its ground truth and the baseline verdict."""

    job_id: int
    test_name: str
    ground_truth: str
    baseline_verdict: str | None
    verdict: str | None
    is_match: bool
    baseline_match: bool
    duration_sec: float
    summary: str
    error: str | None


@dataclass(frozen=True)
class InvestigateSettings:
    """How each job is investigated: the tool, the LLM endpoint, and the timeouts and retries."""

    script_path: str
    llm_url: str
    llm_model: str
    job_timeout_sec: int = DEFAULT_JOB_TIMEOUT_SEC
    llm_timeout_sec: int = DEFAULT_LLM_TIMEOUT_SEC
    retries: int = DEFAULT_RETRIES


def _unquoted_lines(region: str) -> str:
    """Return the lines that state text rather than quote it: no fenced code, blockquote or indented block."""
    kept: list[str] = []
    open_fence: str | None = None
    for line in region.splitlines():
        fence = FENCE_RE.match(line)
        if open_fence is not None:
            if fence and fence.group(1) == open_fence:
                open_fence = None
            continue
        if fence:
            open_fence = fence.group(1)
            continue
        if not line.startswith((">", "    ", "\t")):
            kept.append(line)
    return "\n".join(kept)


def parse_verdict_output(stdout: str) -> tuple[str | None, str]:
    """Return the class that the header and the classification lines agree on, or None if they disagree or name none."""
    marker = COMMENT_MARKER_RE.search(stdout)
    if not marker:
        return None, ""
    header = HEADER_RE.search(stdout)
    labels = {
        match.group(1).lower() for match in CLASSIFICATION_LINE_RE.finditer(_unquoted_lines(stdout[marker.end() :]))
    }
    if header:
        labels.add(header.group(1).lower())
    if len(labels) != 1:
        return None, ""
    (label,) = labels
    start = header.end() if header else marker.end()
    return LABEL_TO_CLASS[label], stdout[start:].strip()


def _elapsed(start: float) -> float:
    return round(time.monotonic() - start, 2)


def investigate_job(job_id: int, settings: InvestigateSettings) -> tuple[str | None, str, float, str | None]:
    """Run openqa-llm-investigate for one job; return (verdict, summary, duration_sec, error)."""
    cmd = [
        settings.script_path,
        OPENQA_TEST_URL.format(job_id=job_id),
        "--dry",
        "--force",
        "--llm-url",
        settings.llm_url,
        "--llm-model",
        settings.llm_model,
        "--timeout",
        str(settings.llm_timeout_sec),
        "--retries",
        str(settings.retries),
    ]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.job_timeout_sec + SUBPROCESS_GRACE_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        message = f"timed out after {settings.job_timeout_sec}s"
        return None, "", _elapsed(start), message
    except OSError as err:
        message = f"execution failed: {err}"
        return None, "", _elapsed(start), message

    duration = _elapsed(start)
    if proc.returncode != 0:
        last_line = (proc.stderr.strip().splitlines() or ["no stderr"])[-1]
        return None, "", duration, f"exit code {proc.returncode}: {last_line}"

    verdict, summary = parse_verdict_output(proc.stdout)
    if verdict is None:
        return None, "", duration, "no classification in output"
    return verdict, summary, duration, None


def load_dataset(path: str) -> list[dict[str, Any]]:
    """Return the dataset job entries, accepting either the wrapped or the bare list layout."""
    with Path(path).open(encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        message = "expected a list of job objects or an object with 'entries'"
        raise TypeError(message)
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict) or "job_id" not in entry or "ground_truth" not in entry:
            message = f"entry {idx} lacks 'job_id' or 'ground_truth'"
            raise ValueError(message)
    return entries


def write_report(path: str | Path, payload: dict[str, Any]) -> None:
    """Write the report atomically so a killed run never leaves invalid JSON behind."""
    target = Path(path)
    handle_fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    temporary = Path(temp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
        Path(temporary).replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def load_existing_results(path: str | Path) -> list[JobResult]:
    """Return the jobs of an earlier run at `path`, or nothing when the file does not exist."""
    target = Path(path)
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return [JobResult(**job) for job in payload["jobs"]]
    except (OSError, ValueError, KeyError, TypeError) as err:
        message = f"cannot resume from {target}: {err}"
        raise ValueError(message) from err


def slice_entries(entries: list[dict[str, Any]], start_index: int, limit: int | None) -> list[dict[str, Any]]:
    """Return the requested window of dataset entries, keeping the dataset order."""
    end = None if limit is None else start_index + limit
    return entries[start_index:end]


def run_dataset(
    entries: list[dict[str, Any]],
    settings: InvestigateSettings,
    on_result: Callable[[JobResult], None] | None = None,
) -> list[JobResult]:
    """Investigate every entry; a failing job is recorded with its error and does not stop the run."""
    results: list[JobResult] = []
    total = len(entries)
    for idx, entry in enumerate(entries, start=1):
        job_id = int(entry["job_id"])
        ground_truth = entry["ground_truth"]
        baseline = entry.get("baseline_verdict", entry.get("llm_verdict"))
        logger.info("[%d/%d] Investigating job %d...", idx, total, job_id)
        verdict, summary, duration, error = investigate_job(job_id, settings)
        result = JobResult(
            job_id=job_id,
            test_name=entry.get("test_name", ""),
            ground_truth=ground_truth,
            baseline_verdict=baseline,
            verdict=verdict,
            is_match=verdict == ground_truth,
            baseline_match=baseline == ground_truth,
            duration_sec=duration,
            summary=summary,
            error=error,
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
        logger.info(
            "Job %d in %.1fs: verdict=%s gt=%s error=%s",
            job_id,
            duration,
            verdict,
            ground_truth,
            error,
        )
    return results


def _pct(part: int, total: int) -> float:
    return round(part / total * 100.0, 1) if total else 0.0


def _score(results: list[JobResult]) -> ScoreReport:
    """Score the jobs once; the JSON summary and the printed report both come from this."""
    return compute_metrics([
        {
            "ground_truth": r.ground_truth,
            "llm_verdict": r.verdict,
            "duration_sec": r.duration_sec,
        }
        for r in results
    ])


def summarize(results: list[JobResult]) -> dict[str, Any]:
    """Return accuracy over all jobs (errors count as misses) and latency over the answered jobs."""
    report = _score(results)
    baseline_matches = sum(r.baseline_match for r in results)
    return {
        "total_jobs": report.total,
        "errors": sum(r.error is not None for r in results),
        "matches": report.matches,
        "accuracy_pct": report.accuracy_pct,
        "baseline_matches": baseline_matches,
        "baseline_accuracy_pct": _pct(baseline_matches, report.total),
        "median_latency_sec": report.median_latency,
        "p90_latency_sec": report.p90_latency,
    }


def build_payload(args: argparse.Namespace, results: dict[int, JobResult]) -> dict[str, Any]:
    """Return the report for every job recorded in the output file, ordered by job_id."""
    ordered = [results[job_id] for job_id in sorted(results)]
    return {
        "metadata": {
            "dataset": args.dataset,
            "llm_url": args.llm_url,
            "llm_model": args.llm_model,
            "script_path": args.script_path,
            "llm_timeout": args.llm_timeout,
            "retries": args.retries,
            "job_timeout": args.job_timeout,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "summary": summarize(ordered),
        "jobs": [asdict(result) for result in ordered],
    }


def format_comparison_table(results: list[JobResult]) -> str:
    """Return a Markdown table comparing the candidate and baseline verdicts per job."""
    lines = [
        "| Job | Test | Ground truth | Baseline | Candidate | Latency (s) | Error |",
        "|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {r.job_id} | {r.test_name} | {r.ground_truth} | {r.baseline_verdict} "
        f"| {r.verdict or '-'} | {r.duration_sec} | {r.error or ''} |"
        for r in results
    )
    return "\n".join(lines)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        message = "must be a positive integer"
        raise argparse.ArgumentTypeError(message)
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run openqa-llm-investigate --dry over a dataset and score the verdicts."
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="Path to evaluation dataset")
    parser.add_argument("--llm-url", required=True, help="LLM chat completions endpoint")
    parser.add_argument("--llm-model", required=True, help="LLM model name")
    parser.add_argument(
        "--output",
        default="investigate_eval_results.json",
        help="Output JSON results path",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="0-based offset of the first job to run; negative values count from the end",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help="Run at most N jobs, starting at --start-index",
    )
    parser.add_argument(
        "--script-path",
        default=DEFAULT_SCRIPT_PATH,
        help="Path to openqa-llm-investigate",
    )
    parser.add_argument(
        "--job-timeout",
        type=positive_int,
        default=DEFAULT_JOB_TIMEOUT_SEC,
        help="Per-job timeout in seconds",
    )
    parser.add_argument(
        "--llm-timeout",
        type=positive_int,
        default=DEFAULT_LLM_TIMEOUT_SEC,
        help="Per-LLM-request timeout in seconds, passed to openqa-llm-investigate",
    )
    parser.add_argument(
        "--retries",
        type=positive_int,
        default=DEFAULT_RETRIES,
        help="LLM request retries per prompt; 1 fails fast in benchmarks",
    )
    parser.add_argument(
        "--table",
        action="store_true",
        help="Print a per-job comparison table vs baseline",
    )
    args = parser.parse_args(argv)

    try:
        entries = load_dataset(args.dataset)
    except (OSError, TypeError, ValueError):
        logger.exception("Invalid dataset %s", args.dataset)
        return 1
    window = slice_entries(entries, args.start_index, args.limit)
    window_ids = [int(entry["job_id"]) for entry in window]
    selected = set(window_ids)
    try:
        recorded = {result.job_id: result for result in load_existing_results(args.output)}
    except ValueError:
        logger.exception("Cannot resume from %s", args.output)
        return 1
    finished = {job_id: result for job_id, result in recorded.items() if job_id in selected}
    if finished:
        logger.info(
            "Resuming %s: %d of %d selected job(s) already recorded",
            args.output,
            len(finished),
            len(window),
        )
    pending = [entry for entry in window if int(entry["job_id"]) not in finished]

    logger.info(
        "Running %d of %d selected jobs against %s (%s)",
        len(pending),
        len(window),
        args.llm_url,
        args.llm_model,
    )
    budget_sec = args.llm_timeout * args.retries
    if budget_sec > args.job_timeout:
        logger.warning(
            "LLM budget of %ds (%ds timeout x %d retries) exceeds --job-timeout %ds; "
            "slow candidates will be cut by the job timeout before the model is judged",
            budget_sec,
            args.llm_timeout,
            args.retries,
            args.job_timeout,
        )
    collected = dict(recorded)

    def record(result: JobResult) -> None:
        collected[result.job_id] = result
        write_report(args.output, build_payload(args, collected))

    settings = InvestigateSettings(
        script_path=args.script_path,
        llm_url=args.llm_url,
        llm_model=args.llm_model,
        job_timeout_sec=args.job_timeout,
        llm_timeout_sec=args.llm_timeout,
        retries=args.retries,
    )
    run_dataset(pending, settings, on_result=record)
    window_results = [collected[job_id] for job_id in window_ids if job_id in collected]
    write_report(args.output, build_payload(args, collected))
    logger.info("Saved results to %s", args.output)

    report = _score(list(collected.values()))
    # The Markdown report and table are the command's stdout output, consumed by callers.
    print(format_markdown_report(report, title=f"Investigate Eval: {args.llm_model}"))  # ruff: ignore[print]
    if args.table:
        print("\n" + format_comparison_table(window_results))  # ruff: ignore[print]
    return 0


if __name__ == "__main__":
    sys.exit(main())
