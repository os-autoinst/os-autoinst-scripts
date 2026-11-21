# Copyright SUSE LLC
"""Unit tests for score_verdicts."""

from __future__ import annotations

import unittest
from typing import Any

import pytest

from scripts.benchmarking.score_verdicts import (
    compute_metrics,
    format_markdown_report,
)


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
