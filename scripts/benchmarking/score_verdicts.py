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
import re
import statistics
import sys
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


@dataclass
class ClassMetrics:
    """Precision, recall and F1 of one verdict class, in percent."""

    name: str
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1_score: float


@dataclass
class ScoreReport:
    """Accuracy, confusion matrix and latency statistics over the scored jobs."""

    total: int
    matches: int
    accuracy_pct: float
    confusion_matrix: dict[str, dict[str, int]]
    class_metrics: dict[str, ClassMetrics]
    median_latency: float | None
    p90_latency: float | None


def _class_metrics(matrix: dict[str, dict[str, int]]) -> dict[str, ClassMetrics]:
    class_metrics: dict[str, ClassMetrics] = {}
    for c in CLASSES:
        tp = matrix[c][c]
        fp = sum(matrix[other][c] for other in CLASSES if other != c)
        fn = sum(matrix[c][other] for other in CLASSES if other != c)

        prec = (tp / (tp + fp) * 100.0) if (tp + fp) > 0 else 0.0
        rec = (tp / (tp + fn) * 100.0) if (tp + fn) > 0 else 0.0
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

        class_metrics[c] = ClassMetrics(
            name=c,
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            precision=round(prec, 1),
            recall=round(rec, 1),
            f1_score=round(f1, 1),
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
    """Calculate confusion matrix, precision/recall per class, and latency stats."""
    matrix: dict[str, dict[str, int]] = {gt: dict.fromkeys(CLASSES, 0) for gt in CLASSES}

    valid_entries = [e for e in eval_entries if e.get(ground_truth_key) in CLASSES and e.get(prediction_key) in CLASSES]

    total = len(valid_entries)
    matches = 0
    latencies: list[float] = []

    for entry in valid_entries:
        gt = entry[ground_truth_key]
        pred = entry[prediction_key]

        matrix[gt][pred] += 1
        if gt == pred:
            matches += 1

        latency = entry.get("duration_sec") or entry.get("latency_sec")
        if latency is not None:
            latencies.append(float(latency))

    accuracy = (matches / total * 100.0) if total > 0 else 0.0
    median_lat = statistics.median(latencies) if latencies else None
    p90_lat = _p90_latency(latencies)

    return ScoreReport(
        total=total,
        matches=matches,
        accuracy_pct=round(accuracy, 1),
        confusion_matrix=matrix,
        class_metrics=_class_metrics(matrix),
        median_latency=round(median_lat, 2) if median_lat is not None else None,
        p90_latency=round(p90_lat, 2) if p90_lat is not None else None,
    )


def format_markdown_report(report: ScoreReport, title: str = "Evaluation Report") -> str:
    """Format evaluation statistics into a clean Markdown document."""
    lines = [
        f"### {title}",
        "",
        f"- **Total Evaluated Jobs**: {report.total}",
        f"- **Overall Accuracy**: **{report.accuracy_pct}%** ({report.matches}/{report.total})",
    ]

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
            f"| {m.precision}% | {m.recall}% | {m.f1_score}% |"
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Score LLM investigation predictions against ground truth.")
    parser.add_argument("dataset", help="Path to evaluation JSON dataset")
    parser.add_argument(
        "--prediction-key",
        default="llm_verdict",
        help="Field name containing model prediction (default: llm_verdict)",
    )
    parser.add_argument(
        "--title",
        default="Model Benchmark Verdict Score",
        help="Title for the Markdown report",
    )
    args = parser.parse_args()

    with pathlib.Path(args.dataset).open(encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("entries") if isinstance(data, dict) and "entries" in data else data
    if not isinstance(entries, list):
        logger.error("Invalid dataset format: expected array of job objects.")
        return 1

    report = compute_metrics(entries, prediction_key=args.prediction_key)
    md = format_markdown_report(report, title=args.title)
    # The Markdown report is the command's stdout output, consumed by callers.
    print(md)  # ruff: ignore[print]
    return 0


if __name__ == "__main__":
    sys.exit(main())
