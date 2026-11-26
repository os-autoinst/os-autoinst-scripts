#!/usr/bin/env python3
# Copyright SUSE LLC
"""Compare a baseline and a speculative llama.cpp endpoint for equality and speed.

Sends the same greedy prompts (temperature 0) to both endpoints, compares the generated
text token by token, and reports equality, the common prefix length, per-endpoint wall
time and decode throughput, plus the speed ratio between them. Two containers on
different ports are compared, so the server configuration under test is never modified.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("compare_spec_outputs")

BASELINE_URL = "http://127.0.0.1:8082/v1/completions"
SPECULATIVE_URL = "http://127.0.0.1:8083/v1/completions"
PROMPTS = (
    (
        "Test died: no candidate needle with tag(s) 'gnome-desktop-ready' matched after "
        "assert_screen. The autoinst log ends with 'zypper: error: Failed to download "
        "repo-oss: Curl error 28'. Name the failure class and the deciding evidence."
    ),
    (
        "Continue this autoinst log analysis and state whether the failure is bisectable:\n"
        '[  512.345678] audit: type=1400 apparmor="DENIED" operation="open" '
        'profile="/usr/sbin/libvirtd" name="/var/run/libvirt/libvirt-sock" pid=4711\n'
        "Result: failed\n"
    ),
    "Summarise in two sentences why a libvirt apparmor denial fails an openQA job.",
)


@dataclass
class Sample:
    """One request against one endpoint."""

    endpoint: str
    prompt_index: int
    repeat: int
    text: str
    tokens: int
    wall_sec: float
    predicted_tokens: int | None
    predicted_per_second: float | None


@dataclass(frozen=True)
class RequestSettings:
    """Request parameters shared by every completion sent to either endpoint."""

    model: str
    max_tokens: int
    timeout: int


def load_prompts(path: str | None) -> list[str]:
    """Return the prompts to send; a prompt file holds one prompt per non-blank line."""
    if path is None:
        return list(PROMPTS)
    with pathlib.Path(path).open(encoding="utf-8") as handle:
        prompts = [line.strip() for line in handle if line.strip()]
    if not prompts:
        message = f"prompt file {path} contains no prompts"
        raise ValueError(message)
    return prompts


def compare_texts(baseline: str, speculative: str) -> dict[str, Any]:
    """Return equality, the common token prefix and the first differing token pair.

    llama.cpp reports no token ids, so tokens are whitespace-delimited words of the
    generated text; a difference that is only whitespace still counts as divergent
    because the downstream verdict parser is sensitive to it.
    """
    baseline_tokens = baseline.split()
    speculative_tokens = speculative.split()
    common = 0
    for baseline_token, speculative_token in zip(baseline_tokens, speculative_tokens, strict=False):
        if baseline_token != speculative_token:
            break
        common += 1
    identical = baseline == speculative
    divergence: dict[str, str] | None = None
    if not identical and common == len(baseline_tokens) == len(speculative_tokens):
        divergence = {"baseline": "<whitespace>", "speculative": "<whitespace>"}
    elif not identical:
        divergence = {
            "baseline": baseline_tokens[common] if common < len(baseline_tokens) else "<end>",
            "speculative": (speculative_tokens[common] if common < len(speculative_tokens) else "<end>"),
        }
    return {
        "identical": identical,
        "common_prefix_tokens": common,
        "baseline_tokens": len(baseline_tokens),
        "speculative_tokens": len(speculative_tokens),
        "first_divergence": divergence,
    }


def parse_timings(payload: dict[str, Any], wall_sec: float) -> tuple[int | None, float | None]:
    """Return generated token count and decode rate, falling back to the wall clock."""
    timings = payload.get("timings") or {}
    usage = payload.get("usage") or {}
    predicted_n = timings.get("predicted_n", usage.get("completion_tokens"))
    predicted_per_second = timings.get("predicted_per_second")
    if predicted_per_second is None and predicted_n and wall_sec > 0:
        predicted_per_second = round(predicted_n / wall_sec, 2)
    return predicted_n, predicted_per_second


def request_completion(
    url: str, model: str, prompt: str, max_tokens: int, timeout: int
) -> tuple[str, float, int | None, float | None]:
    """POST one greedy completion and return its text plus wall time and timing."""
    body: dict[str, Any] = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "seed": 42,
        "stream": False,
        "cache_prompt": False,
    }
    if model:
        body["model"] = model
    # The endpoint URLs are operator-supplied (--baseline-url, --speculative-url), not untrusted input.
    request = urllib.request.Request(  # ruff: ignore[suspicious-url-open-usage]
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # ruff: ignore[suspicious-url-open-usage]
            raw = response.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:200]
        message = f"{url} returned HTTP {err.code}: {detail}"
        raise RuntimeError(message) from err
    except urllib.error.URLError as err:
        message = f"{url} unreachable: {err.reason}"
        raise RuntimeError(message) from err
    wall_sec = round(time.monotonic() - start, 2)
    payload = json.loads(raw)
    choices = payload.get("choices") or [{}]
    text = choices[0].get("text") or ""
    predicted_n, predicted_per_second = parse_timings(payload, wall_sec)
    return text, wall_sec, predicted_n, predicted_per_second


def collect(
    endpoints: dict[str, str],
    prompts: list[str],
    repeat: int,
    settings: RequestSettings,
) -> list[Sample]:
    """Return one sample per endpoint, prompt and repetition."""
    samples: list[Sample] = []
    for endpoint, url in endpoints.items():
        for index, prompt in enumerate(prompts):
            for iteration in range(repeat):
                logger.info("%s prompt %d repetition %d", endpoint, index, iteration + 1)
                text, wall_sec, predicted_n, predicted_per_second = request_completion(
                    url, settings.model, prompt, settings.max_tokens, settings.timeout
                )
                samples.append(
                    Sample(
                        endpoint=endpoint,
                        prompt_index=index,
                        repeat=iteration,
                        text=text,
                        tokens=len(text.split()),
                        wall_sec=wall_sec,
                        predicted_tokens=predicted_n,
                        predicted_per_second=predicted_per_second,
                    )
                )
    return samples


def aggregate(samples: list[Sample], endpoint: str) -> dict[str, Any]:
    """Return median and maximum wall time and decode rate for one endpoint."""
    selected = [s for s in samples if s.endpoint == endpoint]
    walls = [s.wall_sec for s in selected]
    rates = [s.predicted_per_second for s in selected if s.predicted_per_second]
    return {
        "samples": len(selected),
        "median_wall_sec": round(statistics.median(walls), 2) if walls else None,
        "max_wall_sec": round(max(walls), 2) if walls else None,
        "median_decode_tok_s": round(statistics.median(rates), 2) if rates else None,
    }


def build_report(samples: list[Sample], urls: dict[str, str], model: str) -> dict[str, Any]:
    """Return the per-prompt comparisons, the per-endpoint timings and the speed ratio."""
    baseline = {(s.prompt_index, s.repeat): s for s in samples if s.endpoint == "baseline"}
    speculative = {(s.prompt_index, s.repeat): s for s in samples if s.endpoint == "speculative"}
    comparisons = [
        {
            "prompt_index": prompt_index,
            "repeat": repeat,
            **compare_texts(baseline[prompt_index, repeat].text, speculative[prompt_index, repeat].text),
        }
        for prompt_index, repeat in sorted(baseline)
        if (prompt_index, repeat) in speculative
    ]
    baseline_timings = aggregate(samples, "baseline")
    speculative_timings = aggregate(samples, "speculative")
    ratio = None
    if baseline_timings["median_decode_tok_s"] and speculative_timings["median_decode_tok_s"]:
        ratio = round(
            speculative_timings["median_decode_tok_s"] / baseline_timings["median_decode_tok_s"],
            2,
        )
    return {
        "llm_model": model,
        "urls": urls,
        "prompts": len({s.prompt_index for s in samples}),
        "all_identical": all(c["identical"] for c in comparisons),
        "speed_ratio": ratio,
        "timings": {"baseline": baseline_timings, "speculative": speculative_timings},
        "comparisons": comparisons,
        "samples": [asdict(s) for s in samples],
    }


def format_markdown(report: dict[str, Any]) -> str:
    """Return one table per comparison dimension plus the headline verdict."""
    headline = "identical" if report["all_identical"] else "DIVERGENT"
    ratio = report["speed_ratio"]
    lines = [
        "### Output equality and speed",
        "",
        (
            f"Verdict: **{headline}** across {report['prompts']} prompts; speed ratio "
            f"{ratio if ratio is not None else 'n/a'}x "
            "(speculative decode / baseline decode)."
        ),
        "",
        ("| Prompt | Rep | Identical | Common prefix | Baseline tokens | Speculative tokens | First divergence |"),
        "|---|---|---|---|---|---|---|",
    ]
    for comparison in report["comparisons"]:
        divergence = comparison["first_divergence"]
        detail = f"`{divergence['baseline']}` vs `{divergence['speculative']}`" if divergence else "-"
        lines.append(
            f"| {comparison['prompt_index']} | {comparison['repeat']} "
            f"| {'yes' if comparison['identical'] else 'no'} "
            f"| {comparison['common_prefix_tokens']} "
            f"| {comparison['baseline_tokens']} | {comparison['speculative_tokens']} "
            f"| {detail} |"
        )
    lines += [
        "",
        "| Endpoint | Samples | Median wall (s) | Max wall (s) | Median decode tok/s |",
        "|---|---|---|---|---|",
    ]
    for endpoint, timings in report["timings"].items():
        lines.append(
            f"| {endpoint} | {timings['samples']} | {timings['median_wall_sec']} "
            f"| {timings['max_wall_sec']} | {timings['median_decode_tok_s']} |"
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
        description="Compare baseline and speculative llama.cpp endpoints for equality and speed."
    )
    parser.add_argument("--baseline-url", default=BASELINE_URL, help="Baseline completion URL")
    parser.add_argument("--speculative-url", default=SPECULATIVE_URL, help="Speculative completion URL")
    parser.add_argument("--llm-model", default="", help="Model name served by both endpoints")
    parser.add_argument("--prompt-file", default=None, help="One prompt per non-blank line (default: built-in)")
    parser.add_argument("--repeat", type=positive_int, default=1, help="Repetitions per prompt")
    parser.add_argument("--max-tokens", type=positive_int, default=256)
    parser.add_argument("--timeout", type=positive_int, default=900)
    parser.add_argument("--output", default="compare_spec_outputs.json")
    args = parser.parse_args(argv)

    prompts = load_prompts(args.prompt_file)
    urls = {"baseline": args.baseline_url, "speculative": args.speculative_url}
    settings = RequestSettings(args.llm_model, args.max_tokens, args.timeout)
    samples = collect(urls, prompts, args.repeat, settings)
    report = build_report(samples, urls, args.llm_model)
    with pathlib.Path(args.output).open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    # The Markdown report is the command's stdout output, consumed by callers.
    print(format_markdown(report))  # ruff: ignore[print]
    logger.info("Wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
