# Copyright SUSE LLC
"""Unit tests for score_verdicts."""

from __future__ import annotations

import json
import sys
import unittest
from typing import TYPE_CHECKING, Any

import pytest

from scripts.benchmarking import score_verdicts

if TYPE_CHECKING:
    import pathlib

from scripts.benchmarking.score_verdicts import (
    Bootstrap,
    ClusterKeys,
    apply_labels,
    compare_paired,
    compute_metrics,
    format_clustered_report,
    format_markdown_report,
    load_labels,
    score_clustered,
)


def _cluster_rows(ticket: str, verdicts: list[str], label: str, base: int = 0) -> list[dict[str, Any]]:
    """Build the rows of one ticket with the given predictions."""
    return [
        {"job_id": base + index, "reference": ticket, "ground_truth": label, "verdict": verdict}
        for index, verdict in enumerate(verdicts)
    ]


class TestScoreVerdicts(unittest.TestCase):
    """Accuracy and median latency follow the scored entries, and missing latencies are skipped."""

    def test_compute_metrics(self) -> None:
        cases: list[dict[str, Any]] = [
            {
                "desc": "Perfect classification across classes with latencies",
                "entries": [
                    {
                        "ground_truth": "product",
                        "llm_verdict": "product",
                        "duration_sec": 10.0,
                    },
                    {
                        "ground_truth": "test",
                        "llm_verdict": "test",
                        "duration_sec": 20.0,
                    },
                    {
                        "ground_truth": "infra",
                        "llm_verdict": "infra",
                        "duration_sec": 30.0,
                    },
                ],
                "expected_total": 3,
                "expected_matches": 3,
                "expected_accuracy": 100.0,
                "expected_median_latency": 20.0,
            },
            {
                "desc": "Mixed classification with false positives and missing latencies",
                "entries": [
                    {"ground_truth": "product", "llm_verdict": "test"},
                    {"ground_truth": "product", "llm_verdict": "infra"},
                    {"ground_truth": "test", "llm_verdict": "test"},
                    {"ground_truth": "infra", "llm_verdict": "infra"},
                ],
                "expected_total": 4,
                "expected_matches": 2,
                "expected_accuracy": 50.0,
                "expected_median_latency": None,
            },
            {
                "desc": "Empty list of entries",
                "entries": [],
                "expected_total": 0,
                "expected_matches": 0,
                "expected_accuracy": 0.0,
                "expected_median_latency": None,
            },
        ]

        for case in cases:
            with self.subTest(desc=case["desc"]):
                report = compute_metrics(case["entries"])
                assert report.total == case["expected_total"]
                assert report.matches == case["expected_matches"]
                assert report.accuracy_pct == case["expected_accuracy"]
                assert report.median_latency == case["expected_median_latency"]

    @staticmethod
    def test_p90_latency_is_interpolated_from_ten_samples() -> None:
        entries = [
            {"ground_truth": "product", "llm_verdict": "product", "duration_sec": float(seconds)}
            for seconds in range(1, 11)
        ]
        report = compute_metrics(entries)
        assert report.p90_latency == pytest.approx(9.9)

    @staticmethod
    def test_unanswered_job_counts_as_a_miss() -> None:
        report = compute_metrics([
            {"ground_truth": "product", "llm_verdict": "product"},
            {"ground_truth": "product", "llm_verdict": None},
        ])
        assert report.total == 2
        assert report.matches == 1
        assert report.unanswered == 1
        assert report.accuracy_pct == pytest.approx(50.0)
        assert report.class_metrics["product"].recall == pytest.approx(50.0)

    @staticmethod
    def test_class_without_ground_truth_is_not_applicable() -> None:
        entries = [
            {"ground_truth": "product", "llm_verdict": "product"},
            {"ground_truth": "test", "llm_verdict": "test"},
        ]
        md = format_markdown_report(compute_metrics(entries))
        assert "| **Infra** | 0 | 0 | 0 | n/a | n/a | n/a |" in md
        assert "n/a: the run has no ground-truth jobs for that class" in md

    @staticmethod
    def test_format_markdown_report() -> None:
        entries = [
            {"ground_truth": "product", "llm_verdict": "product", "duration_sec": 15.0},
            {"ground_truth": "product", "llm_verdict": "test", "duration_sec": 25.0},
        ]
        report = compute_metrics(entries)
        md = format_markdown_report(report, title="Test Benchmark")

        assert "### Test Benchmark" in md
        assert "- **Total Evaluated Jobs**: 2" in md
        assert "- **Overall Accuracy**: **50.0%** (1/2)" in md
        assert "#### Confusion Matrix" in md
        assert "#### Per-Class Performance" in md


if __name__ == "__main__":
    unittest.main()


def audited_row(job_id: int, reference: str, label: str, *, scored: bool = True) -> dict[str, Any]:
    """Build one row of an audited dataset as the ticket audit writes it."""
    return {"job_id": job_id, "reference": reference, "label": label, "confidence": "certain", "scored": scored}


def arm_job(job_id: int, verdict: str) -> dict[str, Any]:
    """Build one job of an arm run as run_investigate_eval writes it."""
    return {"job_id": job_id, "verdict": verdict, "duration_sec": 10.0}


def test_load_labels_indexes_the_audited_rows_by_job_id(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"entries": [audited_row(1, "boo#1", "test")]}), encoding="utf-8")

    assert load_labels(path) == {1: audited_row(1, "boo#1", "test")}


def test_apply_labels_attaches_the_ticket_and_drops_unscored_rows() -> None:
    labels = {
        1: audited_row(1, "boo#1", "test"),
        2: audited_row(2, "poo#2", "unclear", scored=False),
    }

    rows = apply_labels([arm_job(1, "product"), arm_job(2, "test"), arm_job(3, "test")], labels)

    assert len(rows) == 1
    assert rows[0]["ground_truth"] == "test"
    assert rows[0]["reference"] == "boo#1"
    assert rows[0]["verdict"] == "product"


def test_score_clustered_intervals_are_resampled_over_tickets() -> None:
    rows = [
        *_cluster_rows("boo#1", ["product", "product", "test", "test"], "product", base=10),
        *_cluster_rows("poo#2", ["test", "test", "test", "test"], "test", base=20),
        *_cluster_rows("boo#3", ["product", "product", "test"], "test", base=30),
    ]

    report = score_clustered(rows, ClusterKeys(), Bootstrap(resamples=200, seed=7))

    assert (report.rows, report.tickets) == (11, 3)
    assert report.rows_accuracy.point == pytest.approx(63.6)
    assert report.rows_accuracy.low <= report.rows_accuracy.point <= report.rows_accuracy.high
    assert report.ticket_matches == 2
    assert report.ticket_accuracy.point == pytest.approx(66.7)
    assert report.resamples == 200
    assert report.classes["test"].recall is not None


def test_score_clustered_is_deterministic_for_a_seed() -> None:
    rows = [
        *_cluster_rows("boo#1", ["product", "test"], "product", base=10),
        *_cluster_rows("poo#2", ["test", "test"], "test", base=20),
    ]

    first = score_clustered(rows, ClusterKeys(), Bootstrap(resamples=100, seed=11))
    again = score_clustered(rows, ClusterKeys(), Bootstrap(resamples=100, seed=11))

    assert (first.rows_accuracy.low, first.rows_accuracy.high) == (again.rows_accuracy.low, again.rows_accuracy.high)
    assert (first.ticket_accuracy.low, first.ticket_accuracy.high) == (
        again.ticket_accuracy.low,
        again.ticket_accuracy.high,
    )


def test_score_clustered_ticket_majority_breaks_ties_by_class_order() -> None:
    rows = _cluster_rows("boo#1", ["product", "test"], "product")

    report = score_clustered(rows, ClusterKeys(), Bootstrap(resamples=10, seed=3))

    assert report.ticket_matches == 1
    assert report.ticket_accuracy.point == pytest.approx(100.0)


def test_compare_paired_counts_the_discordant_rows_and_resamples_tickets() -> None:
    labels = {
        1: audited_row(1, "boo#1", "product"),
        2: audited_row(2, "boo#1", "product"),
        3: audited_row(3, "poo#2", "test"),
        4: audited_row(4, "poo#2", "test"),
    }
    arm = apply_labels(
        [arm_job(1, "product"), arm_job(2, "product"), arm_job(3, "test"), arm_job(4, "product")], labels
    )
    control = apply_labels([arm_job(1, "product"), arm_job(2, "test"), arm_job(3, "test"), arm_job(4, "test")], labels)

    paired = compare_paired(arm, control, ClusterKeys(), Bootstrap(resamples=200, seed=5))

    assert (paired.rows, paired.tickets) == (4, 2)
    assert paired.difference.point == pytest.approx(0.0)
    assert paired.arm_only_correct == 1
    assert paired.control_only_correct == 1
    assert paired.difference.low <= 0.0 <= paired.difference.high
    assert paired.arm_ticket_accuracy == pytest.approx(50.0)
    assert paired.control_ticket_accuracy == pytest.approx(100.0)


def test_compare_paired_only_scores_the_rows_both_arms_answered() -> None:
    labels = {1: audited_row(1, "boo#1", "product"), 2: audited_row(2, "poo#2", "test")}
    arm = apply_labels([arm_job(1, "product")], labels)
    control = apply_labels([arm_job(2, "test")], labels)

    paired = compare_paired(arm, control, ClusterKeys(), Bootstrap(resamples=10, seed=5))

    assert paired.rows == 0
    assert paired.difference.point == pytest.approx(0.0)


def test_format_clustered_report_prints_tickets_next_to_every_percentage() -> None:
    rows = _cluster_rows("boo#1", ["product", "test"], "product")

    report = format_clustered_report(score_clustered(rows, ClusterKeys(), Bootstrap(resamples=10, seed=3)))

    assert "Rows / tickets" in report
    assert "cluster bootstrap 95% CI" in report
    assert "ticket majority" in report
    assert "resamples tickets, not rows" in report


def test_main_scores_an_arm_against_the_audited_labels(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"entries": [audited_row(1, "boo#1", "product")]}), encoding="utf-8")
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"jobs": [arm_job(1, "product")]}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["score_verdicts.py", str(run), "--labels", str(labels), "--prediction-key", "verdict", "--resamples", "10"],
    )

    assert score_verdicts.main() == 0
    assert "Accuracy (rows)" in capsys.readouterr().out


def test_main_rejects_a_labels_join_without_a_scored_row(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"entries": [audited_row(1, "boo#1", "product", scored=False)]}), encoding="utf-8")
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"jobs": [arm_job(1, "product")]}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["score_verdicts.py", str(run), "--labels", str(labels)])

    assert score_verdicts.main() == 1


def test_rows_without_a_valid_label_are_ignored() -> None:
    rows = [{"job_id": 1, "reference": "boo#1", "ground_truth": "unclear", "verdict": "product"}]

    report = score_clustered(rows, ClusterKeys(), Bootstrap(resamples=10, seed=1))

    assert report.rows == 1
    assert report.rows_accuracy.point == pytest.approx(0.0)


def test_a_ticket_without_any_usable_prediction_has_no_majority() -> None:
    rows = [
        {"job_id": 1, "reference": "boo#1", "ground_truth": "product", "verdict": None},
        {"job_id": 2, "reference": "boo#1", "ground_truth": "product", "verdict": "garbage"},
    ]

    report = score_clustered(rows, ClusterKeys(), Bootstrap(resamples=10, seed=1))

    assert report.ticket_matches == 0
    assert report.classes["product"].recall == pytest.approx(0.0)


def test_format_clustered_report_includes_a_paired_table() -> None:
    labels = {1: audited_row(1, "boo#1", "product"), 2: audited_row(2, "poo#2", "test")}
    keys = ClusterKeys()
    arm = apply_labels([arm_job(1, "product"), arm_job(2, "test")], labels)
    control = apply_labels([arm_job(1, "test"), arm_job(2, "test")], labels)
    bootstrap = Bootstrap(resamples=10, seed=2)

    report = format_clustered_report(
        score_clustered(arm, keys, bootstrap),
        paired=compare_paired(arm, control, keys, bootstrap),
    )

    assert "Paired comparison against the control" in report
    assert "Rows only the arm got right" in report
    assert "Ticket majority" in report


def test_format_markdown_report_notes_unanswered_jobs_and_missing_classes() -> None:
    entries = [{"ground_truth": "product", "llm_verdict": None}]

    report = format_markdown_report(compute_metrics(entries))

    assert "Unanswered Jobs" in report
    assert "n/a: the run has no ground-truth jobs for that class" in report


def test_read_jobs_accepts_a_plain_array_and_rejects_other_shapes(tmp_path: pathlib.Path) -> None:
    array = tmp_path / "array.json"
    array.write_text(json.dumps([arm_job(1, "test")]), encoding="utf-8")
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"summary": {}}), encoding="utf-8")

    assert score_verdicts.read_jobs(array) == [arm_job(1, "test")]
    assert score_verdicts.read_jobs(other) == []


def test_main_scores_a_run_without_audited_labels(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = tmp_path / "run.json"
    run.write_text(
        json.dumps({"jobs": [{"job_id": 1, "verdict": "test", "ground_truth": "test"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["score_verdicts.py", str(run), "--prediction-key", "verdict"])

    assert score_verdicts.main() == 0
    assert "Overall Accuracy" in capsys.readouterr().out


def test_main_rejects_an_empty_run(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"summary": {}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["score_verdicts.py", str(run)])

    assert score_verdicts.main() == 1


def test_main_pairs_the_arm_with_a_control_run(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"entries": [audited_row(1, "boo#1", "product")]}), encoding="utf-8")
    arm = tmp_path / "arm.json"
    arm.write_text(json.dumps({"jobs": [arm_job(1, "product")]}), encoding="utf-8")
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"jobs": [arm_job(1, "test")]}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "score_verdicts.py",
            str(arm),
            "--labels",
            str(labels),
            "--control",
            str(control),
            "--prediction-key",
            "verdict",
            "--resamples",
            "10",
        ],
    )

    assert score_verdicts.main() == 0
    assert "Accuracy difference (arm - control)" in capsys.readouterr().out
