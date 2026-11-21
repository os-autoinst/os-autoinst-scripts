# Copyright SUSE LLC
"""Unit tests for run_investigate_eval."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts.benchmarking.run_investigate_eval import (
    DEFAULT_DATASET,
    InvestigateSettings,
    investigate_job,
    load_dataset,
    load_existing_results,
    main,
    parse_verdict_output,
    run_dataset,
    slice_entries,
    summarize,
    write_report,
)

HEADER = "**LLM Investigation summary:** {label}\n\n* **console/boot:** failed\n\nAn explanation."
UPSTREAM_COMMENT = (
    "**LLM Investigation summary:**\n\n* **console/boot:** failed\n\nAn explanation.\n\n"
    "Classification: {label}\nBISECT: NO\nRestart: NO"
)
SCRIPT = "/opt/openqa-llm-investigate"
LLM_URL = "http://localhost:8082/v1/chat/completions"
LLM_MODEL = "gemma-4-26B-A4B-it"
SETTINGS = InvestigateSettings(SCRIPT, LLM_URL, LLM_MODEL)
JOB_RESULT = {
    "job_id": 6315020,
    "test_name": "jeos-selinux@aarch64-HD20G",
    "ground_truth": "test",
    "baseline_verdict": "test",
    "verdict": "product",
    "is_match": False,
    "baseline_match": True,
    "duration_sec": 122.3,
    "summary": "* **firstrun:** dialogs",
    "error": None,
}


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[SCRIPT], returncode=returncode, stdout=stdout, stderr=stderr)


class TestParseVerdictOutput(unittest.TestCase):
    """Both comment layouts yield the normalized class; the header class takes precedence."""

    def test_verdict_and_summary_are_extracted(self) -> None:
        cases: list[dict[str, Any]] = [
            {
                "desc": "product bug header",
                "stdout": HEADER.format(label="Product bug"),
                "expected": "product",
            },
            {
                "desc": "test issue header",
                "stdout": HEADER.format(label="Test issue"),
                "expected": "test",
            },
            {
                "desc": "infrastructure problem header",
                "stdout": HEADER.format(label="Infrastructure problem"),
                "expected": "infra",
            },
            {
                "desc": "job URL line printed before the comment header",
                "stdout": "https://openqa.opensuse.org/tests/1\n" + HEADER.format(label="Test issue"),
                "expected": "test",
            },
            {
                "desc": "lowercase label is matched case-insensitively",
                "stdout": HEADER.format(label="product bug"),
                "expected": "product",
            },
            {
                "desc": "classification only in a log line is not a verdict",
                "stdout": "Classification: Product bug\n",
                "expected": None,
            },
            {"desc": "empty output yields no verdict", "stdout": "", "expected": None},
            {
                "desc": "unknown label yields no verdict",
                "stdout": "**LLM Investigation summary:** Unknown",
                "expected": None,
            },
            {
                "desc": "upstream layout with product bug classification line",
                "stdout": UPSTREAM_COMMENT.format(label="Product bug"),
                "expected": "product",
            },
            {
                "desc": "upstream layout with test issue classification line",
                "stdout": UPSTREAM_COMMENT.format(label="Test issue"),
                "expected": "test",
            },
            {
                "desc": "upstream layout with infrastructure problem classification line",
                "stdout": UPSTREAM_COMMENT.format(label="Infrastructure problem"),
                "expected": "infra",
            },
            {
                "desc": "conflicting header and classification labels yield no verdict",
                "stdout": HEADER.format(label="Test issue") + "\nClassification: Product bug",
                "expected": None,
            },
            {
                "desc": "two classification lines with different labels yield no verdict",
                "stdout": "**LLM Investigation summary:**\n\nClassification: Product bug\nClassification: Test issue",
                "expected": None,
            },
            {
                "desc": "echoed choice list is not a verdict",
                "stdout": (
                    "**LLM Investigation summary:**\n\n"
                    "Classification: Product bug | Test issue | Infrastructure problem"
                ),
                "expected": None,
            },
            {
                "desc": "header line containing the choice list is not a verdict",
                "stdout": "**LLM Investigation summary:** Product bug | Test issue | Infrastructure problem",
                "expected": None,
            },
            {
                "desc": "quoted classification line above the real one is ignored",
                "stdout": (
                    "**LLM Investigation summary:**\n\n  log: Classification: Product bug (quoted)\n"
                    "Classification: Test issue"
                ),
                "expected": "test",
            },
            {
                "desc": "label on the line after the marker does not cross the newline",
                "stdout": (
                    "**LLM Investigation summary:**\nTest issue is what the analysis shows.\n\n"
                    "Classification: Product bug"
                ),
                "expected": "product",
            },
            {
                "desc": "bold classification line with emphasis markers",
                "stdout": "**LLM Investigation summary:**\n\n**Classification:** Product bug",
                "expected": "product",
            },
            {
                "desc": "classification only inside a fenced code block is not a verdict",
                "stdout": "**LLM Investigation summary:**\n\n```\nClassification: Product bug\n```",
                "expected": None,
            },
            {
                "desc": "classification only inside an unterminated fence is not a verdict",
                "stdout": "**LLM Investigation summary:**\n\n```\nClassification: Product bug",
                "expected": None,
            },
            {
                "desc": "classification only in a blockquote is not a verdict",
                "stdout": "**LLM Investigation summary:**\n\n> Classification: Product bug",
                "expected": None,
            },
            {
                "desc": "classification only in an indented block is not a verdict",
                "stdout": "**LLM Investigation summary:**\n\n    Classification: Product bug",
                "expected": None,
            },
            {
                "desc": "fenced classification does not hide the real line after it",
                "stdout": (
                    "**LLM Investigation summary:**\n\n```\nClassification: Product bug\n```\n\n"
                    "Classification: Test issue"
                ),
                "expected": "test",
            },
            {
                "desc": "blockquoted classification does not hide the real line",
                "stdout": (
                    "**LLM Investigation summary:**\n\n> Classification: Product bug\n\nClassification: Test issue"
                ),
                "expected": "test",
            },
            {
                "desc": "indented classification does not hide the real line",
                "stdout": (
                    "**LLM Investigation summary:**\n\n    Classification: Product bug\n\nClassification: Test issue"
                ),
                "expected": "test",
            },
            {
                "desc": "inline-code classification does not hide the real line",
                "stdout": (
                    "**LLM Investigation summary:**\n\n`Classification: Product bug`\n\nClassification: Test issue"
                ),
                "expected": "test",
            },
            {
                "desc": "header class agreeing with a classification line parses",
                "stdout": HEADER.format(label="Product bug") + "\nClassification: Product bug",
                "expected": "product",
            },
            {
                "desc": "classification line before the marker is not a verdict",
                "stdout": "Classification: Test issue\n\n**LLM Investigation summary:**\n\nNo class.",
                "expected": None,
            },
            {
                "desc": "upstream layout without a classification line yields no verdict",
                "stdout": "**LLM Investigation summary:**\n\nNo class.",
                "expected": None,
            },
        ]
        for case in cases:
            with self.subTest(desc=case["desc"]):
                verdict, _summary = parse_verdict_output(case["stdout"])
                assert verdict == case["expected"]

    @staticmethod
    def test_summary_is_the_comment_text_after_the_header() -> None:
        verdict, summary = parse_verdict_output("preamble\n" + HEADER.format(label="Product bug"))
        assert verdict == "product"
        assert summary == "* **console/boot:** failed\n\nAn explanation."
        assert "LLM Investigation summary" not in summary

    @staticmethod
    def test_summary_for_upstream_layout_is_the_comment_text_after_the_marker() -> None:
        verdict, summary = parse_verdict_output(UPSTREAM_COMMENT.format(label="Infrastructure problem"))
        assert verdict == "infra"
        assert (
            summary == "* **console/boot:** failed\n\nAn explanation.\n\n"
            "Classification: Infrastructure problem\nBISECT: NO\nRestart: NO"
        )


class TestInvestigateJob(unittest.TestCase):
    """One job runs the tool with the expected arguments and failures become errors."""

    @staticmethod
    def test_success_returns_verdict_summary_and_duration() -> None:
        with patch("subprocess.run", return_value=completed(stdout=HEADER.format(label="Infrastructure problem"))):
            verdict, summary, duration, error = investigate_job(123, SETTINGS)
        assert verdict == "infra"
        assert "console/boot" in summary
        assert duration >= 0.0
        assert error is None

    @staticmethod
    def test_command_uses_dry_force_and_tests_url() -> None:
        with patch("subprocess.run", return_value=completed(stdout=HEADER.format(label="Product bug"))) as mock_run:
            investigate_job(123, SETTINGS)
        cmd = mock_run.call_args.args[0]
        assert cmd[0] == SCRIPT
        assert cmd[1] == "https://openqa.opensuse.org/tests/123"
        assert cmd[2:] == [
            "--dry",
            "--force",
            "--llm-url",
            LLM_URL,
            "--llm-model",
            LLM_MODEL,
            "--timeout",
            "600",
            "--retries",
            "1",
        ]

    @staticmethod
    def test_llm_budget_is_passed_through() -> None:
        settings = InvestigateSettings(SCRIPT, LLM_URL, LLM_MODEL, job_timeout_sec=120, llm_timeout_sec=90, retries=3)
        with patch("subprocess.run", return_value=completed(stdout=HEADER.format(label="Product bug"))) as mock_run:
            investigate_job(123, settings)
        cmd = mock_run.call_args.args[0]
        assert cmd[-4:] == ["--timeout", "90", "--retries", "3"]
        assert mock_run.call_args.kwargs["timeout"] == 180

    def test_failure_modes_are_reported_as_errors(self) -> None:
        cases: list[dict[str, Any]] = [
            {
                "desc": "timeout is reported with its limit",
                "side_effect": subprocess.TimeoutExpired(cmd=SCRIPT, timeout=10),
                "return_value": None,
                "error_contains": "timed out after 5s",
            },
            {
                "desc": "missing executable is reported as execution failure",
                "side_effect": OSError("No such file"),
                "return_value": None,
                "error_contains": "execution failed",
            },
            {
                "desc": "non-zero exit reports the last stderr line",
                "side_effect": None,
                "return_value": completed(returncode=2, stderr="boom\nLLM unreachable"),
                "error_contains": "exit code 2: LLM unreachable",
            },
            {
                "desc": "zero exit without classification is an error",
                "side_effect": None,
                "return_value": completed(stdout="nothing useful"),
                "error_contains": "no classification",
            },
        ]
        settings = InvestigateSettings(SCRIPT, LLM_URL, LLM_MODEL, job_timeout_sec=5)
        with patch("subprocess.run") as mock_run:
            for case in cases:
                with self.subTest(desc=case["desc"]):
                    mock_run.reset_mock(side_effect=True, return_value=True)
                    mock_run.side_effect = case["side_effect"]
                    mock_run.return_value = case["return_value"]
                    verdict, summary, _duration, error = investigate_job(123, settings)
                    assert verdict is None
                    assert not summary
                    assert error is not None
                    assert case["error_contains"] in error


class TestLoadExistingResults(unittest.TestCase):
    """Earlier reports resume a run; unreadable ones stop it without touching the file."""

    @staticmethod
    def test_partial_report_is_loadable() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "results.json")
            write_report(
                output,
                {"metadata": {}, "summary": {}, "jobs": [JOB_RESULT]},
            )
            results = load_existing_results(output)
        assert len(results) == 1
        assert results[0].job_id == 6315020
        assert results[0].verdict == "product"
        assert not results[0].is_match

    @staticmethod
    def test_missing_report_yields_no_results() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "results.json")
            assert load_existing_results(output) == []

    def test_unreadable_report_is_an_error_naming_the_path(self) -> None:
        cases = [
            {"desc": "truncated JSON", "content": '{"jobs": [{"job_id": 1'},
            {"desc": "valid JSON without jobs", "content": '{"summary": {}}'},
            {"desc": "job entry missing the required result fields", "content": '{"jobs": [{"job_id": 1}]}'},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for case in cases:
                with self.subTest(desc=case["desc"]):
                    output = str(Path(tmp) / "results.json")
                    Path(output).write_text(case["content"], encoding="utf-8")
                    with pytest.raises(ValueError, match="cannot resume from") as caught:
                        load_existing_results(output)
                    assert output in str(caught.value)


class TestWriteReport(unittest.TestCase):
    """Reports are written atomically and a failed write leaves the previous report intact."""

    @staticmethod
    def test_report_is_written_as_json_with_a_trailing_newline() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "results.json")
            write_report(output, {"summary": {"matches": 1}})
            content = Path(output).read_text(encoding="utf-8")
        assert content.endswith("\n")
        assert json.loads(content) == {"summary": {"matches": 1}}

    @staticmethod
    def test_failed_write_keeps_the_previous_report_and_removes_its_temp_file() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "results.json")
            write_report(output, {"summary": {"matches": 1}})
            with (
                patch(
                    "scripts.benchmarking.run_investigate_eval.os.replace",
                    side_effect=OSError("disk full"),
                ),
                pytest.raises(OSError, match="disk full"),
            ):
                write_report(output, {"summary": {"matches": 2}})
            remaining = sorted(p.name for p in Path(tmp).iterdir())
            with Path(output).open(encoding="utf-8") as handle:
                content = json.load(handle)
        assert remaining == ["results.json"]
        assert content == {"summary": {"matches": 1}}


class TestSliceEntries(unittest.TestCase):
    """The window follows Python slice semantics over the dataset order."""

    def test_window_selection(self) -> None:
        entries = [{"job_id": job_id} for job_id in range(1, 6)]
        cases: list[dict[str, Any]] = [
            {
                "desc": "no window keeps every entry",
                "start": 0,
                "limit": None,
                "expected": [1, 2, 3, 4, 5],
            },
            {
                "desc": "limit alone takes the first N",
                "start": 0,
                "limit": 2,
                "expected": [1, 2],
            },
            {
                "desc": "start alone skips the first N",
                "start": 3,
                "limit": None,
                "expected": [4, 5],
            },
            {
                "desc": "start and limit select the second half",
                "start": 2,
                "limit": 2,
                "expected": [3, 4],
            },
            {
                "desc": "start beyond the end yields nothing",
                "start": 9,
                "limit": 2,
                "expected": [],
            },
            {
                "desc": "limit beyond the end is clamped",
                "start": 3,
                "limit": 99,
                "expected": [4, 5],
            },
            {
                "desc": "negative start counts from the end like a slice",
                "start": -2,
                "limit": None,
                "expected": [4, 5],
            },
        ]
        for case in cases:
            with self.subTest(desc=case["desc"]):
                selected = slice_entries(entries, case["start"], case["limit"])
                assert [entry["job_id"] for entry in selected] == case["expected"]

    @staticmethod
    def test_dataset_order_is_preserved_and_input_untouched() -> None:
        entries = [{"job_id": job_id} for job_id in (7, 3, 9)]
        selected = slice_entries(entries, 1, 2)
        assert [entry["job_id"] for entry in selected] == [3, 9]
        assert [entry["job_id"] for entry in entries] == [7, 3, 9]


class TestRunDataset(unittest.TestCase):
    """A dataset run records every job, including failed ones, in order."""

    @staticmethod
    def test_failing_job_does_not_stop_the_run() -> None:
        entries = [
            {
                "job_id": 1,
                "test_name": "a",
                "ground_truth": "product",
                "llm_verdict": "product",
            },
            {
                "job_id": 2,
                "test_name": "b",
                "ground_truth": "test",
                "llm_verdict": "infra",
            },
            {
                "job_id": 3,
                "test_name": "c",
                "ground_truth": "infra",
                "baseline_verdict": "infra",
            },
        ]
        responses = [
            completed(stdout=HEADER.format(label="Product bug")),
            subprocess.TimeoutExpired(cmd=SCRIPT, timeout=10),
            completed(stdout=HEADER.format(label="Test issue")),
        ]
        settings = InvestigateSettings(SCRIPT, LLM_URL, LLM_MODEL, job_timeout_sec=5)
        with patch("subprocess.run", side_effect=responses):
            results = run_dataset(entries, settings)

        assert [r.job_id for r in results] == [1, 2, 3]
        assert [r.verdict for r in results] == ["product", None, "test"]
        assert [r.is_match for r in results] == [True, False, False]
        assert results[1].error is not None
        assert results[2].error is None

    @staticmethod
    def test_baseline_falls_back_to_llm_verdict_when_no_baseline_key() -> None:
        entries = [{"job_id": 1, "ground_truth": "product", "llm_verdict": "product"}]
        with patch(
            "subprocess.run",
            return_value=completed(stdout=HEADER.format(label="Test issue")),
        ):
            results = run_dataset(entries, SETTINGS)
        assert results[0].baseline_verdict == "product"
        assert results[0].baseline_match
        assert not results[0].is_match


class TestSummarize(unittest.TestCase):
    """Errors count as misses, and the baseline accuracy is reported alongside the candidate."""

    @staticmethod
    def test_errors_count_as_misses_and_baseline_is_compared() -> None:
        entries = [
            {"job_id": 1, "ground_truth": "product", "llm_verdict": "product"},
            {"job_id": 2, "ground_truth": "test", "llm_verdict": "test"},
            {"job_id": 3, "ground_truth": "infra", "llm_verdict": "product"},
            {"job_id": 4, "ground_truth": "test", "llm_verdict": "infra"},
        ]
        responses = [
            completed(stdout=HEADER.format(label="Product bug")),
            completed(stdout=HEADER.format(label="Infrastructure problem")),
            subprocess.TimeoutExpired(cmd=SCRIPT, timeout=10),
            completed(stdout=HEADER.format(label="Test issue")),
        ]
        with patch("subprocess.run", side_effect=responses):
            results = run_dataset(entries, SETTINGS)
        summary = summarize(results)

        assert summary["total_jobs"] == 4
        assert summary["errors"] == 1
        assert summary["matches"] == 2
        assert summary["accuracy_pct"] == pytest.approx(50.0)
        assert summary["baseline_matches"] == 2
        assert summary["baseline_accuracy_pct"] == pytest.approx(50.0)


class TestLoadDataset(unittest.TestCase):
    """Both dataset layouts load; entries without a ground truth or a non-list shape are rejected."""

    def test_wrapped_and_bare_layouts_are_accepted_and_invalid_rejected(self) -> None:
        job = {"job_id": 1, "ground_truth": "product"}
        cases: list[dict[str, Any]] = [
            {
                "desc": "wrapped entries object",
                "content": {"entries": [job]},
                "expected_len": 1,
            },
            {"desc": "bare list of jobs", "content": [job], "expected_len": 1},
            {
                "desc": "job without ground truth is rejected",
                "content": [{"job_id": 1}],
                "raises": True,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for case in cases:
                with self.subTest(desc=case["desc"]):
                    path = str(Path(tmp) / "dataset.json")
                    with Path(path).open("w", encoding="utf-8") as f:
                        json.dump(case["content"], f)
                    if case.get("raises"):
                        with pytest.raises(ValueError, match="lacks 'job_id'"):
                            load_dataset(path)
                    else:
                        assert len(load_dataset(path)) == case["expected_len"]

    @staticmethod
    def test_default_dataset_is_the_repository_fixture() -> None:
        entries = load_dataset(DEFAULT_DATASET)
        assert Path(DEFAULT_DATASET).is_file()
        assert entries
        assert all("ground_truth" in entry for entry in entries)

    @staticmethod
    def test_invalid_shape_raises_type_error() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "dataset.json")
            with Path(path).open("w", encoding="utf-8") as f:
                json.dump("scalar", f)
            with pytest.raises(TypeError):
                load_dataset(path)


class TestMain(unittest.TestCase):
    """The command runs the selected window, resumes from earlier reports and rejects bad arguments."""

    @staticmethod
    def test_limit_writes_json_results_for_the_subset() -> None:
        entries = [
            {
                "job_id": 1,
                "test_name": "a",
                "ground_truth": "product",
                "llm_verdict": "product",
            },
            {
                "job_id": 2,
                "test_name": "b",
                "ground_truth": "test",
                "llm_verdict": "test",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            dataset = str(Path(tmp) / "dataset.json")
            output = str(Path(tmp) / "results.json")
            with Path(dataset).open("w", encoding="utf-8") as f:
                json.dump({"entries": entries}, f)
            with (
                patch(
                    "subprocess.run",
                    return_value=completed(stdout=HEADER.format(label="Product bug")),
                ) as mock_run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rc = main([
                    "--dataset",
                    dataset,
                    "--llm-url",
                    LLM_URL,
                    "--llm-model",
                    LLM_MODEL,
                    "--output",
                    output,
                    "--limit",
                    "1",
                    "--script-path",
                    SCRIPT,
                    "--table",
                ])
            with Path(output).open(encoding="utf-8") as f:
                payload = json.load(f)

        assert rc == 0
        assert mock_run.call_count == 1
        assert payload["summary"]["total_jobs"] == 1
        assert payload["jobs"][0]["job_id"] == 1
        assert payload["metadata"]["llm_model"] == LLM_MODEL
        assert payload["metadata"]["llm_timeout"] == 600
        assert payload["metadata"]["retries"] == 1

    def test_overcommitted_llm_budget_warns(self) -> None:
        entries = [{"job_id": 1, "ground_truth": "product"}]
        with tempfile.TemporaryDirectory() as tmp:
            dataset = str(Path(tmp) / "dataset.json")
            output = str(Path(tmp) / "results.json")
            with Path(dataset).open("w", encoding="utf-8") as f:
                json.dump({"entries": entries}, f)
            with (
                patch(
                    "subprocess.run",
                    return_value=completed(stdout=HEADER.format(label="Product bug")),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertLogs("run_investigate_eval", level="WARNING") as logs,
            ):
                main([
                    "--dataset",
                    dataset,
                    "--llm-url",
                    LLM_URL,
                    "--llm-model",
                    LLM_MODEL,
                    "--output",
                    output,
                    "--llm-timeout",
                    "600",
                    "--retries",
                    "2",
                    "--job-timeout",
                    "900",
                ])
        assert "exceeds --job-timeout" in "\n".join(logs.output)

    @staticmethod
    def test_non_positive_retries_are_rejected_by_the_parser() -> None:
        with pytest.raises(SystemExit):
            main([
                "--llm-url",
                LLM_URL,
                "--llm-model",
                LLM_MODEL,
                "--retries",
                "0",
            ])

    @staticmethod
    def test_invalid_dataset_returns_error_code() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing.json")
            rc = main(["--dataset", missing, "--llm-url", LLM_URL, "--llm-model", LLM_MODEL])
        assert rc == 1

    @staticmethod
    def test_start_index_and_limit_request_only_the_window() -> None:
        entries = [
            {
                "job_id": job_id,
                "test_name": f"test-{job_id}",
                "ground_truth": "product",
                "llm_verdict": "product",
            }
            for job_id in (1, 2, 3, 4)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            dataset = str(Path(tmp) / "dataset.json")
            output = str(Path(tmp) / "results.json")
            with Path(dataset).open("w", encoding="utf-8") as handle:
                json.dump({"entries": entries}, handle)
            with (
                patch(
                    "subprocess.run",
                    return_value=completed(stdout=HEADER.format(label="Product bug")),
                ) as mock_run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rc = main([
                    "--dataset",
                    dataset,
                    "--llm-url",
                    LLM_URL,
                    "--llm-model",
                    LLM_MODEL,
                    "--output",
                    output,
                    "--start-index",
                    "2",
                    "--limit",
                    "2",
                    "--script-path",
                    SCRIPT,
                ])
            with Path(output).open(encoding="utf-8") as handle:
                payload = json.load(handle)

        assert rc == 0
        assert [call.args[0][1][-1] for call in mock_run.call_args_list] == ["3", "4"]
        assert [job["job_id"] for job in payload["jobs"]] == [3, 4]
        assert payload["summary"]["total_jobs"] == 2

    @staticmethod
    def test_report_is_usable_after_a_mid_run_failure() -> None:
        entries = [
            {
                "job_id": job_id,
                "test_name": f"test-{job_id}",
                "ground_truth": "product",
                "llm_verdict": "product",
            }
            for job_id in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            dataset = str(Path(tmp) / "dataset.json")
            output = str(Path(tmp) / "results.json")
            with Path(dataset).open("w", encoding="utf-8") as handle:
                json.dump({"entries": entries}, handle)
            with (
                patch(
                    "subprocess.run",
                    side_effect=[
                        completed(stdout=HEADER.format(label="Product bug")),
                        RuntimeError("connection reset"),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
                pytest.raises(RuntimeError),
            ):
                main([
                    "--dataset",
                    dataset,
                    "--llm-url",
                    LLM_URL,
                    "--llm-model",
                    LLM_MODEL,
                    "--output",
                    output,
                    "--script-path",
                    SCRIPT,
                ])
            with Path(output).open(encoding="utf-8") as handle:
                payload = json.load(handle)

        assert [job["job_id"] for job in payload["jobs"]] == [1]
        assert payload["summary"]["total_jobs"] == 1
        assert payload["summary"]["matches"] == 1

    @staticmethod
    def test_resume_skips_recorded_jobs_and_completes_the_report() -> None:
        entries = [
            {
                "job_id": job_id,
                "test_name": f"test-{job_id}",
                "ground_truth": "product",
                "llm_verdict": "test",
            }
            for job_id in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            dataset = str(Path(tmp) / "dataset.json")
            output = str(Path(tmp) / "results.json")
            with Path(dataset).open("w", encoding="utf-8") as handle:
                json.dump({"entries": entries}, handle)
            write_report(
                output,
                {
                    "metadata": {"partial": True},
                    "summary": {"total_jobs": 1},
                    "jobs": [{**JOB_RESULT, "job_id": 1}],
                },
            )
            with (
                patch(
                    "subprocess.run",
                    return_value=completed(stdout=HEADER.format(label="Product bug")),
                ) as mock_run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rc = main([
                    "--dataset",
                    dataset,
                    "--llm-url",
                    LLM_URL,
                    "--llm-model",
                    LLM_MODEL,
                    "--output",
                    output,
                    "--script-path",
                    SCRIPT,
                ])
            with Path(output).open(encoding="utf-8") as handle:
                payload = json.load(handle)

        assert rc == 0
        assert mock_run.call_count == 1
        assert [call.args[0][1][-1] for call in mock_run.call_args_list] == ["2"]
        assert [job["job_id"] for job in payload["jobs"]] == [1, 2]
        assert payload["summary"]["total_jobs"] == 2
        assert payload["metadata"]["llm_model"] == LLM_MODEL

    @staticmethod
    def test_unreadable_report_exits_without_touching_it() -> None:
        entries = [{"job_id": 1, "ground_truth": "product"}]
        with tempfile.TemporaryDirectory() as tmp:
            dataset = str(Path(tmp) / "dataset.json")
            output = str(Path(tmp) / "results.json")
            with Path(dataset).open("w", encoding="utf-8") as handle:
                json.dump({"entries": entries}, handle)
            Path(output).write_text('{"jobs": [{"job_id": 1', encoding="utf-8")
            with patch("subprocess.run") as mock_run:
                rc = main([
                    "--dataset",
                    dataset,
                    "--llm-url",
                    LLM_URL,
                    "--llm-model",
                    LLM_MODEL,
                    "--output",
                    output,
                    "--script-path",
                    SCRIPT,
                ])
            content = Path(output).read_text(encoding="utf-8")
        assert rc == 1
        assert mock_run.call_count == 0
        assert '{"jobs": [{"job_id": 1' in content

    @staticmethod
    def test_non_positive_limit_is_rejected_by_the_parser() -> None:
        with pytest.raises(SystemExit):
            main(["--llm-url", LLM_URL, "--llm-model", LLM_MODEL, "--limit", "0"])


if __name__ == "__main__":
    unittest.main()
