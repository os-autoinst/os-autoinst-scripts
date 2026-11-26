#!/usr/bin/env python3
# Copyright SUSE LLC
"""Score LLM investigation verdicts against ground truth.

Computes accuracy, confusion matrix, per-class precision/recall/F1, and latency
statistics across benchmark evaluations. Generates formatted Markdown summaries.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import random
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("score_verdicts")

CLASSES = ["product", "test", "infra"]
CLASS_LABEL_PATTERN = r"(Product bug|Test issue|Infrastructure problem)"
LABEL_TO_CLASS = {
    "product bug": "product",
    "test issue": "test",
    "infrastructure problem": "infra",
}
CLASSIFICATION_LINE_RE = re.compile(
    rf"^[*_]*Classification[*_]*:[ \t*_]*{CLASS_LABEL_PATTERN}[ \t*_.]*$",
    re.IGNORECASE | re.MULTILINE,
)
P90_MIN_SAMPLES = 10
DEFAULT_RESAMPLES = 1000
DEFAULT_SEED = 20261126
CI_LEVEL = 95.0


@dataclass
class ClassMetrics:
    """Precision, recall and F1 of one verdict class, in percent; None when the run has no ground truth for it."""

    name: str
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    f1_score: float | None


@dataclass
class ScoreReport:
    """Accuracy, confusion matrix and latency statistics over the scored jobs."""

    total: int
    matches: int
    unanswered: int
    accuracy_pct: float
    confusion_matrix: dict[str, dict[str, int]]
    class_metrics: dict[str, ClassMetrics]
    median_latency: float | None
    p90_latency: float | None


@dataclass(frozen=True)
class ClusterKeys:
    """The field names a clustered score reads from one row."""

    prediction: str = "verdict"
    cluster: str = "reference"
    truth: str = "ground_truth"


@dataclass(frozen=True)
class Bootstrap:
    """Resampling budget and level of a cluster bootstrap."""

    resamples: int = DEFAULT_RESAMPLES
    seed: int = DEFAULT_SEED
    level: float = CI_LEVEL


@dataclass
class Interval:
    """A point estimate in percent with its bootstrap interval, over the given number of clusters."""

    point: float
    low: float
    high: float
    clusters: int


@dataclass
class ClusterReport:
    """Row accuracy with a ticket-level interval, and the ticket-majority accuracy."""

    rows: int
    tickets: int
    rows_accuracy: Interval
    ticket_matches: int
    ticket_accuracy: Interval
    classes: dict[str, ClassMetrics]
    resamples: int


@dataclass
class PairedReport:
    """The arm's accuracy difference to the control, paired over the same rows and tickets."""

    rows: int
    tickets: int
    difference: Interval
    arm_only_correct: int
    control_only_correct: int
    arm_ticket_accuracy: float
    control_ticket_accuracy: float


def _class_metrics(matrix: dict[str, dict[str, int]], unanswered: dict[str, int]) -> dict[str, ClassMetrics]:
    class_metrics: dict[str, ClassMetrics] = {}
    for c in CLASSES:
        tp = matrix[c][c]
        fp = sum(matrix[other][c] for other in CLASSES if other != c)
        fn = sum(matrix[c][other] for other in CLASSES if other != c) + unanswered[c]
        support = sum(matrix[c].values()) + unanswered[c]

        if support == 0:
            prec = rec = f1 = None
        else:
            prec = (tp / (tp + fp) * 100.0) if (tp + fp) > 0 else 0.0
            rec = tp / support * 100.0
            f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

        class_metrics[c] = ClassMetrics(
            name=c,
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            precision=None if prec is None else round(prec, 1),
            recall=None if rec is None else round(rec, 1),
            f1_score=None if f1 is None else round(f1, 1),
        )
    return class_metrics


def _p90_latency(latencies: list[float]) -> float | None:
    if len(latencies) >= P90_MIN_SAMPLES:
        return statistics.quantiles(latencies, n=10)[8]
    return max(latencies) if latencies else None


def compute_metrics(
    eval_entries: list[dict[str, Any]],
    prediction_key: str = "llm_verdict",
    ground_truth_key: str = "ground_truth",
) -> ScoreReport:
    """Score every entry with a valid ground truth; an entry without a valid prediction is an unanswered miss."""
    matrix: dict[str, dict[str, int]] = {gt: dict.fromkeys(CLASSES, 0) for gt in CLASSES}
    unanswered = dict.fromkeys(CLASSES, 0)
    scored = [e for e in eval_entries if e.get(ground_truth_key) in CLASSES]

    matches = 0
    latencies: list[float] = []

    for entry in scored:
        gt = entry[ground_truth_key]
        pred = entry.get(prediction_key)
        if pred not in CLASSES:
            unanswered[gt] += 1
            continue

        matrix[gt][pred] += 1
        if gt == pred:
            matches += 1

        latency = entry.get("duration_sec") or entry.get("latency_sec")
        if latency is not None:
            latencies.append(float(latency))

    total = len(scored)
    accuracy = (matches / total * 100.0) if total > 0 else 0.0
    median_lat = statistics.median(latencies) if latencies else None
    p90_lat = _p90_latency(latencies)

    return ScoreReport(
        total=total,
        matches=matches,
        unanswered=sum(unanswered.values()),
        accuracy_pct=round(accuracy, 1),
        confusion_matrix=matrix,
        class_metrics=_class_metrics(matrix, unanswered),
        median_latency=round(median_lat, 2) if median_lat is not None else None,
        p90_latency=round(p90_lat, 2) if p90_lat is not None else None,
    )


DEFAULT_KEYS = ClusterKeys()
DEFAULT_BOOTSTRAP = Bootstrap()


def load_labels(path: pathlib.Path) -> dict[int, dict[str, Any]]:
    """Read an audited dataset and index its rows by job id."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries", []) if isinstance(payload, dict) else payload
    return {int(entry["job_id"]): entry for entry in entries}


def apply_labels(jobs: list[dict[str, Any]], labels: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the audited label, ticket and scored flag to the jobs of an arm run."""
    merged: list[dict[str, Any]] = []
    for job in jobs:
        label = labels.get(int(job["job_id"]))
        if label is None:
            continue
        merged.append({
            **job,
            "ground_truth": label["label"],
            "reference": label["reference"],
            "scored": bool(label.get("scored", True)),
            "label_confidence": label.get("confidence"),
        })
    return [job for job in merged if job["scored"]]


def _matches(row: dict[str, Any], keys: ClusterKeys) -> tuple[int, int]:
    prediction = row.get(keys.prediction)
    truth = row.get(keys.truth)
    if truth not in CLASSES:
        return 0, 0
    return (1 if prediction == truth else 0), 1


def _rows_by_cluster(rows: list[dict[str, Any]], keys: ClusterKeys) -> dict[str, list[dict[str, Any]]]:
    clusters: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        clusters.setdefault(str(row[keys.cluster]), []).append(row)
    return clusters


def _accuracy(rows: list[dict[str, Any]], keys: ClusterKeys) -> float:
    matches = total = 0
    for row in rows:
        hit, counted = _matches(row, keys)
        matches += hit
        total += counted
    return matches / total * 100.0 if total else 0.0


def _percentile(values: list[float], level: float) -> float:
    ordered = sorted(values)
    index = round(level / 100.0 * (len(ordered) - 1))
    return ordered[min(len(ordered) - 1, max(0, index))]


def _interval(samples: list[float], point: float, clusters: int, level: float) -> Interval:
    tail = (100.0 - level) / 2.0
    low = _percentile(samples, tail) if samples else point
    high = _percentile(samples, 100.0 - tail) if samples else point
    return Interval(point=round(point, 1), low=round(low, 1), high=round(high, 1), clusters=clusters)


def _majority(values: list[str | None]) -> str:
    counts = Counter(value for value in values if value in CLASSES)
    if not counts:
        return "unanswered"
    best = max(counts.values())
    return next(name for name in CLASSES if counts.get(name) == best)


def _ticket_verdict(clusters: dict[str, list[dict[str, Any]]], keys: ClusterKeys) -> dict[str, tuple[str, str]]:
    """Return the majority prediction and the label of every ticket."""
    return {
        ticket: (
            _majority([row.get(keys.prediction) for row in rows]),
            _majority([row.get(keys.truth) for row in rows]),
        )
        for ticket, rows in clusters.items()
    }


def _bootstrap_rows(
    clusters: dict[str, list[dict[str, Any]]],
    keys: ClusterKeys,
    bootstrap: Bootstrap,
    rng: random.Random,
) -> list[float]:
    names = list(clusters)
    return [
        _accuracy([row for name in rng.choices(names, k=len(names)) for row in clusters[name]], keys)
        for _ in range(bootstrap.resamples)
    ]


def _bootstrap_tickets(verdicts: dict[str, tuple[str, str]], bootstrap: Bootstrap, rng: random.Random) -> list[float]:
    names = list(verdicts)
    return [
        sum(verdicts[name][0] == verdicts[name][1] for name in rng.choices(names, k=len(names))) / len(names) * 100.0
        for _ in range(bootstrap.resamples)
    ]


def score_clustered(
    rows: list[dict[str, Any]],
    keys: ClusterKeys = DEFAULT_KEYS,
    bootstrap: Bootstrap = DEFAULT_BOOTSTRAP,
) -> ClusterReport:
    """Score the rows and give every percentage a ticket-level 95% interval."""
    clusters = _rows_by_cluster(rows, keys)
    verdicts = _ticket_verdict(clusters, keys)
    rng = random.Random(bootstrap.seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage] - a seeded bootstrap is not a secret

    rows_point = _accuracy(rows, keys)
    ticket_matches = sum(prediction == truth for prediction, truth in verdicts.values())
    ticket_point = ticket_matches / len(verdicts) * 100.0 if verdicts else 0.0

    return ClusterReport(
        rows=len(rows),
        tickets=len(clusters),
        rows_accuracy=_interval(
            _bootstrap_rows(clusters, keys, bootstrap, rng), rows_point, len(clusters), bootstrap.level
        ),
        ticket_matches=ticket_matches,
        ticket_accuracy=_interval(
            _bootstrap_tickets(verdicts, bootstrap, rng), ticket_point, len(clusters), bootstrap.level
        ),
        classes=compute_metrics(rows, prediction_key=keys.prediction, ground_truth_key=keys.truth).class_metrics,
        resamples=bootstrap.resamples,
    )


def compare_paired(
    arm_rows: list[dict[str, Any]],
    control_rows: list[dict[str, Any]],
    keys: ClusterKeys = DEFAULT_KEYS,
    bootstrap: Bootstrap = DEFAULT_BOOTSTRAP,
) -> PairedReport:
    """Compare an arm with a control over the rows both answered, resampling tickets."""
    control_by_job = {int(row["job_id"]): row for row in control_rows if "job_id" in row}
    paired = [(row, control_by_job[int(row["job_id"])]) for row in arm_rows if int(row["job_id"]) in control_by_job]
    clusters: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for arm, control in paired:
        clusters.setdefault(str(arm[keys.cluster]), []).append((arm, control))

    def difference(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> float:
        return _accuracy([arm for arm, _ in rows], keys) - _accuracy([control for _, control in rows], keys)

    rng = random.Random(bootstrap.seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage] - a seeded bootstrap is not a secret
    names = list(clusters)
    samples = [
        difference([pair for name in rng.choices(names, k=len(names)) for pair in clusters[name]])
        for _ in range(bootstrap.resamples)
    ]

    arm_only = sum(1 for arm, control in paired if _matches(arm, keys)[0] and not _matches(control, keys)[0])
    control_only = sum(1 for arm, control in paired if _matches(control, keys)[0] and not _matches(arm, keys)[0])
    arm_tickets = _ticket_verdict(_rows_by_cluster([arm for arm, _ in paired], keys), keys)
    control_tickets = _ticket_verdict(_rows_by_cluster([control for _, control in paired], keys), keys)
    ticket_hits = sum(arm_tickets[name][0] == arm_tickets[name][1] for name in arm_tickets)

    return PairedReport(
        rows=len(paired),
        tickets=len(clusters),
        difference=_interval(samples, difference(paired), len(clusters), bootstrap.level),
        arm_only_correct=arm_only,
        control_only_correct=control_only,
        arm_ticket_accuracy=round(ticket_hits / len(arm_tickets) * 100.0, 1) if arm_tickets else 0.0,
        control_ticket_accuracy=round(
            sum(control_tickets[name][0] == control_tickets[name][1] for name in control_tickets)
            / len(control_tickets)
            * 100.0,
            1,
        )
        if control_tickets
        else 0.0,
    )


def format_clustered_report(
    report: ClusterReport,
    title: str = "Audited verdict score",
    paired: PairedReport | None = None,
) -> str:
    """Format a clustered score with the ticket counts next to every percentage."""
    rows = report.rows_accuracy
    tickets = report.ticket_accuracy
    lines = [
        f"### {title}",
        "",
        f"- **Rows / tickets**: {report.rows} / {report.tickets}",
        (
            f"- **Accuracy (rows)**: **{rows.point}%** — {report.resamples}-resample cluster bootstrap "
            f"{CI_LEVEL:.0f}% CI [{rows.low}%, {rows.high}%] over {rows.clusters} tickets"
        ),
        (
            f"- **Accuracy (ticket majority)**: **{tickets.point}%** ({report.ticket_matches}/{report.tickets}) "
            f"— 95% CI [{tickets.low}%, {tickets.high}%] over {tickets.clusters} tickets"
        ),
        "",
        "| Class | TP | FP | FN | Precision | Recall | F1 Score |",
        "|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| **{name.capitalize()}** | {metrics.true_positives} | {metrics.false_positives} "
        f"| {metrics.false_negatives} | {_percent(metrics.precision)} | {_percent(metrics.recall)} "
        f"| {_percent(metrics.f1_score)} |"
        for name, metrics in report.classes.items()
    )
    if paired is not None:
        diff = paired.difference
        lines.extend([
            "",
            "#### Paired comparison against the control",
            "",
            f"- **Rows / tickets**: {paired.rows} / {paired.tickets}",
            (
                f"- **Accuracy difference (arm - control)**: **{diff.point:+.1f} points** "
                f"— 95% CI [{diff.low:+.1f}, {diff.high:+.1f}] over {diff.clusters} tickets"
            ),
            f"- **Rows only the arm got right**: {paired.arm_only_correct}",
            f"- **Rows only the control got right**: {paired.control_only_correct}",
            (
                f"- **Ticket majority**: arm {paired.arm_ticket_accuracy}% "
                f"against control {paired.control_ticket_accuracy}%"
            ),
        ])
    lines.extend([
        "",
        "Every interval resamples tickets, not rows: 200 rows over 80 tickets hold 80 independent decisions, not 200.",
    ])
    return "\n".join(lines)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value}%"


def format_markdown_report(report: ScoreReport, title: str = "Evaluation Report") -> str:
    """Format evaluation statistics into a clean Markdown document."""
    lines = [
        f"### {title}",
        "",
        f"- **Total Evaluated Jobs**: {report.total}",
        f"- **Overall Accuracy**: **{report.accuracy_pct}%** ({report.matches}/{report.total})",
    ]
    if report.unanswered:
        lines.append(f"- **Unanswered Jobs**: {report.unanswered} (counted as misses)")

    if report.median_latency is not None:
        lines.append(f"- **Median Latency**: {report.median_latency}s (p90: {report.p90_latency}s)")

    lines.extend([
        "",
        "#### Confusion Matrix",
        "",
        "| Ground Truth \\ Predicted | Product | Test | Infra |",
        "|---|---|---|---|",
    ])
    for gt in CLASSES:
        row = report.confusion_matrix[gt]
        lines.append(f"| **{gt.capitalize()}** | {row['product']} | {row['test']} | {row['infra']} |")

    lines.extend([
        "",
        "#### Per-Class Performance",
        "",
        "| Class | TP | FP | FN | Precision | Recall | F1 Score |",
        "|---|---|---|---|---|---|---|",
    ])
    for c in CLASSES:
        m = report.class_metrics[c]
        lines.append(
            f"| **{c.capitalize()}** | {m.true_positives} | {m.false_positives} | {m.false_negatives} "
            f"| {_percent(m.precision)} | {_percent(m.recall)} | {_percent(m.f1_score)} |"
        )
    if any(m.recall is None for m in report.class_metrics.values()):
        lines.extend([
            "",
            "n/a: the run has no ground-truth jobs for that class, so it has no precision, recall or F1.",
        ])

    return "\n".join(lines)


def read_jobs(path: pathlib.Path) -> list[dict[str, Any]]:
    """Read the arm jobs of a run file, whether it keys them as entries or as jobs."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("entries", "jobs"):
            jobs = payload.get(key)
            if isinstance(jobs, list):
                return jobs
    return []


def _ensure_cluster(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [{**row, key: row.get(key, str(row.get("job_id")))} for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Score LLM investigation predictions against ground truth.")
    parser.add_argument("dataset", help="Path to a run file with the arm's job verdicts")
    parser.add_argument(
        "--prediction-key",
        default="llm_verdict",
        help="Field name containing the model prediction (default: llm_verdict; run files use verdict)",
    )
    parser.add_argument("--title", default="Model Benchmark Verdict Score", help="Title for the Markdown report")
    parser.add_argument("--labels", help="Audited dataset to take the labels and the ticket of every job from")
    parser.add_argument("--control", help="Run file of the control arm for the paired comparison")
    parser.add_argument("--cluster-key", default="reference", help="Field naming the ticket (default: reference)")
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES, help="Bootstrap resamples")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Bootstrap seed")
    args = parser.parse_args()

    jobs = read_jobs(pathlib.Path(args.dataset))
    if not jobs:
        logger.error("Invalid dataset format: expected an array of job objects.")
        return 1

    keys = ClusterKeys(prediction=args.prediction_key, cluster=args.cluster_key)
    bootstrap = Bootstrap(resamples=args.resamples, seed=args.seed)

    if not args.labels:
        report = compute_metrics(jobs, prediction_key=args.prediction_key)
        print(format_markdown_report(report, title=args.title))  # ruff: ignore[print]
        return 0

    labels = load_labels(pathlib.Path(args.labels))
    rows = _ensure_cluster(apply_labels(jobs, labels), keys.cluster)
    if not rows:
        logger.error("No job of the run matched a scored audited label.")
        return 1

    paired = None
    if args.control:
        control_rows = _ensure_cluster(apply_labels(read_jobs(pathlib.Path(args.control)), labels), keys.cluster)
        paired = compare_paired(rows, control_rows, keys, bootstrap)

    print(format_clustered_report(score_clustered(rows, keys, bootstrap), title=args.title, paired=paired))  # ruff: ignore[print]
    return 0


if __name__ == "__main__":
    sys.exit(main())
