# Copyright SUSE LLC
"""Unit tests for compare_spec_outputs."""

from __future__ import annotations

import argparse
import io
import json
import logging
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.benchmarking.compare_spec_outputs import (
    BASELINE_URL,
    SPECULATIVE_URL,
    RequestSettings,
    Sample,
    aggregate,
    build_report,
    collect,
    compare_texts,
    format_markdown,
    load_prompts,
    main,
    positive_int,
    request_completion,
)

MODEL = "gemma-4-26B-A4B-it"
TIMINGS = {
    "timings": {
        "predicted_n": 64,
        "predicted_per_second": 40.0,
    }
}
URLOPEN = "scripts.benchmarking.compare_spec_outputs.urllib.request.urlopen"


def response(payload: dict, status: int = 200) -> MagicMock:
    handle = MagicMock()
    handle.read.return_value = json.dumps(payload).encode()
    handle.__enter__.return_value = handle
    handle.__exit__.return_value = False
    handle.status = status
    return handle


def completion(text: str) -> dict:
    return {"choices": [{"text": text}], **TIMINGS}


def make_samples(baseline_rate: float | None, speculative_rate: float) -> list[Sample]:
    return [
        Sample("baseline", 0, 0, "product bug", 2, 4.0, 64, baseline_rate),
        Sample("baseline", 1, 0, "test issue", 2, 6.0, 64, baseline_rate),
        Sample("speculative", 0, 0, "product bug", 2, 2.0, 64, speculative_rate),
        Sample("speculative", 1, 0, "test issue", 2, 3.0, 64, speculative_rate),
    ]


class TestCompareTexts(unittest.TestCase):
    """Equality and the first divergent token are reported for each generated text pair."""

    def test_equality_and_divergence(self) -> None:
        cases: list[dict[str, Any]] = [
            {
                "desc": "identical text is token-for-token equal",
                "baseline": "product bug because zypper timed out",
                "speculative": "product bug because zypper timed out",
                "identical": True,
                "common": 6,
                "divergence": None,
            },
            {
                "desc": "divergence reports the first differing token pair",
                "baseline": "product bug because zypper timed out",
                "speculative": "product bug because curl timed out",
                "identical": False,
                "common": 3,
                "divergence": {"baseline": "zypper", "speculative": "curl"},
            },
            {
                "desc": "a shorter speculative answer diverges at the end",
                "baseline": "test issue in needle matching",
                "speculative": "test issue",
                "identical": False,
                "common": 2,
                "divergence": {"baseline": "in", "speculative": "<end>"},
            },
            {
                "desc": "whitespace-only differences still count as divergent",
                "baseline": "product bug",
                "speculative": "product  bug",
                "identical": False,
                "common": 2,
                "divergence": {"baseline": "<whitespace>", "speculative": "<whitespace>"},
            },
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                compared = compare_texts(case["baseline"], case["speculative"])
                assert compared["identical"] == case["identical"]
                assert compared["common_prefix_tokens"] == case["common"]
                assert compared["first_divergence"] == case["divergence"]


class TestPositiveInt(unittest.TestCase):
    """Only strictly positive integers are accepted as CLI counts."""

    def test_values(self) -> None:
        cases: list[dict[str, Any]] = [
            {"desc": "positive number is accepted", "value": "256", "expected": 256},
            {"desc": "zero is rejected", "value": "0", "raises": True},
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                if case.get("raises"):
                    with pytest.raises(argparse.ArgumentTypeError):
                        positive_int(case["value"])
                else:
                    assert positive_int(case["value"]) == case["expected"]


class TestRequestCompletion(unittest.TestCase):
    """A completion request is greedy and seeded, and transport failures become errors."""

    @staticmethod
    def test_greedy_request_and_timings() -> None:
        with patch(URLOPEN, return_value=response(completion("test issue"))) as mock_urlopen:
            text, wall_sec, predicted_n, predicted_per_second = request_completion(
                BASELINE_URL, MODEL, "prompt", 256, 60
            )
        body = json.loads(mock_urlopen.call_args.args[0].data)
        assert body["temperature"] == pytest.approx(0.0)
        assert body["seed"] == 42
        assert not body["stream"]
        assert body["max_tokens"] == 256
        assert body["model"] == MODEL
        assert text == "test issue"
        assert wall_sec >= 0.0
        assert predicted_n == 64
        assert predicted_per_second == pytest.approx(40.0)

    @staticmethod
    def test_decode_rate_falls_back_to_the_wall_clock() -> None:
        payload = {"choices": [{"text": "x"}], "usage": {"completion_tokens": 50}}
        with (
            patch(URLOPEN, return_value=response(payload)),
            patch(
                "scripts.benchmarking.compare_spec_outputs.time.monotonic",
                side_effect=[0.0, 5.0],
            ),
        ):
            _, wall_sec, predicted_n, predicted_per_second = request_completion(
                SPECULATIVE_URL, MODEL, "prompt", 64, 60
            )
        assert wall_sec == pytest.approx(5.0)
        assert predicted_n == 50
        assert predicted_per_second == pytest.approx(10.0)

    @staticmethod
    def test_model_field_is_omitted_when_unset() -> None:
        with patch(URLOPEN, return_value=response(completion("x"))) as mock_urlopen:
            request_completion(SPECULATIVE_URL, "", "prompt", 64, 60)
        assert "model" not in json.loads(mock_urlopen.call_args.args[0].data)

    @staticmethod
    def test_http_error_is_reported_with_body() -> None:
        error = urllib.error.HTTPError(
            BASELINE_URL, 503, "busy", Message(), io.BytesIO(b"server busy, please try again")
        )
        with patch(URLOPEN, side_effect=error), pytest.raises(RuntimeError) as caught:
            request_completion(BASELINE_URL, MODEL, "prompt", 64, 60)
        assert "HTTP 503" in str(caught.value)
        assert "server busy" in str(caught.value)

    @staticmethod
    def test_connection_error_is_reported() -> None:
        with (
            patch(URLOPEN, side_effect=urllib.error.URLError("connection refused")),
            pytest.raises(RuntimeError) as caught,
        ):
            request_completion(SPECULATIVE_URL, MODEL, "prompt", 64, 60)
        assert "connection refused" in str(caught.value)


class TestCollect(unittest.TestCase):
    """Collection covers every endpoint, prompt and repetition exactly once."""

    @staticmethod
    def test_every_endpoint_prompt_and_repetition_is_sampled() -> None:
        with patch(URLOPEN, return_value=response(completion("product bug"))):
            samples = collect(
                {"baseline": BASELINE_URL, "speculative": SPECULATIVE_URL},
                ["first", "second"],
                2,
                RequestSettings(MODEL, 64, 60),
            )
        assert len(samples) == 8
        assert {(s.endpoint, s.prompt_index, s.repeat) for s in samples} == {
            (endpoint, index, repeat)
            for endpoint in ("baseline", "speculative")
            for index in (0, 1)
            for repeat in (0, 1)
        }
        assert {s.tokens for s in samples} == {2}


class TestLoadPrompts(unittest.TestCase):
    """A prompt file yields its non-blank lines; an empty file is rejected."""

    @staticmethod
    def test_prompt_file_and_builtin_default() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "prompts.txt")
            pathlib.Path(path).write_text("first prompt\n\n  second prompt  \n", encoding="utf-8")
            assert load_prompts(path) == ["first prompt", "second prompt"]
            empty = str(pathlib.Path(tmp) / "empty.txt")
            pathlib.Path(empty).write_text("\n\n", encoding="utf-8")
            with pytest.raises(ValueError, match="contains no prompts"):
                load_prompts(empty)
        assert len(load_prompts(None)) == 3


class TestReporting(unittest.TestCase):
    """Reports compare per-endpoint timings, the speed ratio and the first differing token."""

    @staticmethod
    def test_aggregation_is_per_endpoint() -> None:
        samples = make_samples(20.0, 45.0)
        assert aggregate(samples, "baseline") == {
            "samples": 2,
            "median_wall_sec": 5.0,
            "max_wall_sec": 6.0,
            "median_decode_tok_s": 20.0,
        }
        assert aggregate(samples, "speculative")["median_decode_tok_s"] == pytest.approx(45.0)

    @staticmethod
    def test_report_states_equality_and_the_speed_ratio() -> None:
        report = build_report(
            make_samples(20.0, 45.0),
            {"baseline": BASELINE_URL, "speculative": SPECULATIVE_URL},
            MODEL,
        )
        assert report["all_identical"]
        assert report["speed_ratio"] == pytest.approx(2.25)
        assert len(report["comparisons"]) == 2
        assert "Verdict: **identical**" in format_markdown(report)

    @staticmethod
    def test_report_names_the_first_differing_token() -> None:
        samples = make_samples(20.0, 45.0)
        samples[2] = Sample("speculative", 0, 0, "product failure", 2, 2.0, 64, 45.0)
        report = build_report(samples, {"baseline": BASELINE_URL, "speculative": SPECULATIVE_URL}, MODEL)
        markdown = format_markdown(report)
        assert not report["all_identical"]
        assert "Verdict: **DIVERGENT**" in markdown
        assert "`bug` vs `failure`" in markdown

    @staticmethod
    def test_missing_decode_rate_yields_no_ratio() -> None:
        report = build_report(
            make_samples(None, 45.0),
            {"baseline": BASELINE_URL, "speculative": SPECULATIVE_URL},
            MODEL,
        )
        assert report["speed_ratio"] is None
        assert "speed ratio n/a" in format_markdown(report)


class TestMain(unittest.TestCase):
    """The command writes the JSON report and prints the Markdown table to stdout."""

    def test_main_writes_report_from_two_endpoints(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        answers = {BASELINE_URL: "product bug", SPECULATIVE_URL: "test issue"}

        def fake_urlopen(request: urllib.request.Request, **_kwargs: object) -> MagicMock:
            return response(completion(answers[request.full_url]))

        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "report.json")
            with (
                patch(URLOPEN, side_effect=fake_urlopen),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                code = main([
                    "--baseline-url",
                    BASELINE_URL,
                    "--speculative-url",
                    SPECULATIVE_URL,
                    "--llm-model",
                    MODEL,
                    "--max-tokens",
                    "64",
                    "--repeat",
                    "2",
                    "--output",
                    output,
                ])
            assert code == 0
            assert "| Endpoint | Samples |" in stdout.getvalue()
            with pathlib.Path(output).open(encoding="utf-8") as handle:
                report = json.load(handle)
            assert report["llm_model"] == MODEL
            assert not report["all_identical"]
            assert len(report["comparisons"]) == 6
            assert report["timings"]["baseline"]["samples"] == 6


if __name__ == "__main__":
    unittest.main()
