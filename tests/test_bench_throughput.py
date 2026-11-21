# Copyright SUSE LLC
"""Unit tests for bench_throughput."""

from __future__ import annotations

import argparse
import io
import json
import logging
import pathlib
import tempfile
import unittest
import urllib.error
from email.message import Message
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.benchmarking.bench_throughput import (
    Endpoint,
    Sample,
    build_prompt,
    format_markdown,
    main,
    parse_timings,
    positive_int,
    run_probe,
    summarize,
)

URL = "http://127.0.0.1:8082/v1/completions"
MODEL = "ornith"
ENDPOINT = Endpoint(URL, MODEL, 30)
TIMINGS_PAYLOAD = {
    "choices": [{"text": "product bug"}],
    "usage": {"prompt_tokens": 1234, "completion_tokens": 32},
    "timings": {
        "prompt_n": 1234,
        "prompt_per_second": 42.5,
        "predicted_n": 32,
        "predicted_per_second": 21.25,
    },
}
URLOPEN = "scripts.benchmarking.bench_throughput.urllib.request.urlopen"


def response(payload: dict, status: int = 200) -> MagicMock:
    handle = MagicMock()
    handle.read.return_value = json.dumps(payload).encode()
    handle.__enter__.return_value = handle
    handle.__exit__.return_value = False
    handle.status = status
    return handle


def sample_rows() -> list[Sample]:
    return [
        Sample("cold", 1000, 3.0, 1000, 30.0, 32, 15.0, estimated=False),
        Sample("warm", 1000, 1.0, 1000, 42.5, 32, 21.25, estimated=False),
        Sample("warm", 1000, 1.1, 1000, 40.0, 32, 20.0, estimated=False),
        Sample("warm", 5000, 2.0, 5000, 10.0, 32, 5.0, estimated=True),
    ]


class TestParseTimings(unittest.TestCase):
    """Server timings are authoritative and the usage fallback is flagged as estimated."""

    def test_server_timings_are_authoritative(self) -> None:
        cases: list[dict[str, Any]] = [
            {
                "desc": "server timings are used verbatim and not flagged",
                "payload": TIMINGS_PAYLOAD,
                "expected": {
                    "prompt_tokens": 1234,
                    "prompt_per_second": 42.5,
                    "predicted_tokens": 32,
                    "predicted_per_second": 21.25,
                    "estimated": False,
                },
            },
            {
                "desc": "missing timings falls back to usage and wall clock",
                "payload": {"usage": {"prompt_tokens": 100, "completion_tokens": 10}},
                "expected": {
                    "prompt_tokens": 100,
                    "prompt_per_second": 10.0,
                    "predicted_tokens": 10,
                    "predicted_per_second": 1.0,
                    "estimated": True,
                },
            },
            {
                "desc": "empty payload yields no measurements",
                "payload": {},
                "expected": {
                    "prompt_tokens": None,
                    "prompt_per_second": None,
                    "predicted_tokens": None,
                    "predicted_per_second": None,
                    "estimated": True,
                },
            },
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                assert parse_timings(case["payload"], wall_sec=10.0) == case["expected"]


class TestBuildPrompt(unittest.TestCase):
    """Prompts are deterministic and sized from the requested token target."""

    def test_prompt_grows_with_target_size(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "1k target yields roughly 4k characters", "target": 1000, "chars": 4000},
            {"desc": "5k target yields roughly 20k characters", "target": 5000, "chars": 20000},
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                prompt = build_prompt(case["target"])
                assert len(prompt) >= case["chars"]
                assert len(prompt) < case["chars"] * 2

    @staticmethod
    def test_prompt_is_deterministic() -> None:
        assert build_prompt(1000) == build_prompt(1000)

    @staticmethod
    def test_minimum_target_does_not_crash() -> None:
        assert "autoinst" in build_prompt(1)


class TestRunProbe(unittest.TestCase):
    """A probe turns one endpoint response into a sample and reports transport failures."""

    @staticmethod
    def test_sample_is_built_from_response() -> None:
        with patch(URLOPEN, return_value=response(TIMINGS_PAYLOAD)):
            sample = run_probe(ENDPOINT, "prompt", 32, mode="warm", target_tokens=5000)
        assert sample.mode == "warm"
        assert sample.target_tokens == 5000
        assert sample.prompt_tokens == 1234
        assert sample.prompt_per_second == pytest.approx(42.5)
        assert sample.predicted_per_second == pytest.approx(21.25)
        assert not sample.estimated
        assert sample.wall_sec >= 0.0

    def test_prefix_cache_is_disabled_for_cold_probes(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "cold probe evaluates the whole prompt", "mode": "cold", "cache": False},
            {"desc": "warm probe reuses the cached prefix", "mode": "warm", "cache": True},
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                with patch(URLOPEN, return_value=response(TIMINGS_PAYLOAD)) as mock_urlopen:
                    run_probe(ENDPOINT, "prompt", 32, mode=case["mode"])
                payload = json.loads(mock_urlopen.call_args.args[0].data)
                assert payload["cache_prompt"] == case["cache"]

    @staticmethod
    def test_http_error_is_reported_with_body() -> None:
        error = urllib.error.HTTPError(URL, 500, "boom", Message(), io.BytesIO(b"model failed to load"))
        with patch(URLOPEN, side_effect=error), pytest.raises(RuntimeError) as caught:
            run_probe(ENDPOINT, "prompt", 32)
        assert "HTTP 500" in str(caught.value)
        assert "model failed to load" in str(caught.value)

    @staticmethod
    def test_connection_error_is_reported() -> None:
        with (
            patch(URLOPEN, side_effect=urllib.error.URLError("connection refused")),
            pytest.raises(RuntimeError) as caught,
        ):
            run_probe(ENDPOINT, "prompt", 32)
        assert "connection refused" in str(caught.value)


class TestPositiveInt(unittest.TestCase):
    """Only strictly positive integers are accepted as CLI counts."""

    def test_values(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "positive number is accepted", "value": "64", "expected": 64},
            {"desc": "zero is rejected", "value": "0", "raises": True},
            {"desc": "negative number is rejected", "value": "-1", "raises": True},
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                if case.get("raises"):
                    with pytest.raises(argparse.ArgumentTypeError):
                        positive_int(case["value"])
                else:
                    assert positive_int(case["value"]) == case["expected"]


class TestReporting(unittest.TestCase):
    """Markdown and summary output use every sample but medians only the warm, measured ones."""

    @staticmethod
    def test_markdown_table_lists_every_sample() -> None:
        table = format_markdown(sample_rows(), MODEL)
        assert "| Mode | Eval tokens |" in table
        assert f"({MODEL})" in table
        assert table.count("\n| cold ") == 1
        assert table.count("\n| warm ") == 3
        assert "| warm | 5000 | 2.0 | 10.00 | 5.00 | yes |" in table
        assert "| 1.0 | 42.50 | 21.25 | no |" in table

    @staticmethod
    def test_summary_uses_median_of_warm_samples_only() -> None:
        summary = summarize(sample_rows())
        assert summary["1000"] == {"prefill_tok_s": 41.25, "decode_tok_s": 20.62, "samples": 2}
        assert summary["5000"] == {"prefill_tok_s": None, "decode_tok_s": None, "samples": 0}

    def test_main_writes_report_and_prints_table(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "out.json")
            with (
                patch(URLOPEN, return_value=response(TIMINGS_PAYLOAD)),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                code = main([
                    "--llm-url",
                    URL,
                    "--llm-model",
                    MODEL,
                    "--token-target",
                    "1000",
                    "--repeat",
                    "1",
                    "--output",
                    output,
                ])
            assert code == 0
            assert "| Mode |" in stdout.getvalue()
            with pathlib.Path(output).open(encoding="utf-8") as handle:
                report = json.load(handle)
            assert report["llm_model"] == MODEL
            assert len(report["samples"]) == 2
            assert report["summary"]["1000"]["samples"] == 1


if __name__ == "__main__":
    unittest.main()
