#!/usr/bin/env python3
# Copyright SUSE LLC
"""Measure llama.cpp prompt-evaluation and decode throughput on a completion endpoint.

Sends deterministic log-shaped prompts of configurable token size, once cold and then
repeatedly with a warm prefix cache, and reports the server-side `timings` values:
prompt eval tokens/second (prefill) and predicted tokens/second (decode).
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
logger = logging.getLogger("bench_throughput")

DEFAULT_URL = "http://127.0.0.1:8082/v1/completions"
CHARS_PER_TOKEN = 4
LOG_LINE = (
    '[  512.345678] audit: type=1400 apparmor="DENIED" operation="open" '
    'profile="/usr/sbin/libvirtd" name="/var/run/libvirt/libvirt-sock" pid=4711 comm="libvirtd"\n'
)
BENCH_SEED = (
    "# Test died: no candidate needle with tag(s) 'gnome-desktop-ready' matched\n"
    "## Result: failed\n"
    "zypper: error: Failed to download 'repo-oss': Curl error 28: Operation timed out\n"
)


@dataclass
class Sample:
    """One request against the endpoint."""

    mode: str
    target_tokens: int
    wall_sec: float
    prompt_tokens: int | None
    prompt_per_second: float | None
    predicted_tokens: int | None
    predicted_per_second: float | None
    estimated: bool


@dataclass(frozen=True)
class Endpoint:
    """Completion endpoint and the request settings shared by every probe."""

    url: str
    model: str
    timeout: int


def build_prompt(target_tokens: int) -> str:
    """Return deterministic log-shaped text of roughly `target_tokens` tokens."""
    target_chars = max(target_tokens, 1) * CHARS_PER_TOKEN
    body = (BENCH_SEED + LOG_LINE * (target_chars // len(LOG_LINE) + 1))[:target_chars]
    return f"{BENCH_SEED}Continue this autoinst log analysis:\n{body}"


def parse_timings(payload: dict[str, Any], wall_sec: float) -> dict[str, Any]:
    """Extract the server timings; fall back to usage counters when the server omits them.

    `timings` is llama.cpp's authoritative server-side measurement and is what the
    benchmark reports; without it the wall clock includes queueing and HTTP overhead,
    so such a sample is flagged as estimated instead of being mixed in silently.
    """
    timings = payload.get("timings") or {}
    usage = payload.get("usage") or {}
    prompt_n = timings.get("prompt_n", usage.get("prompt_tokens"))
    predicted_n = timings.get("predicted_n", usage.get("completion_tokens"))
    estimated = timings.get("prompt_per_second") is None
    prompt_per_second = timings.get("prompt_per_second")
    predicted_per_second = timings.get("predicted_per_second")
    if estimated and prompt_n and wall_sec > 0:
        prompt_per_second = round(prompt_n / wall_sec, 2)
        predicted_per_second = round(predicted_n / wall_sec, 2) if predicted_n else None
    return {
        "prompt_tokens": prompt_n,
        "prompt_per_second": prompt_per_second,
        "predicted_tokens": predicted_n,
        "predicted_per_second": predicted_per_second,
        "estimated": estimated,
    }


def run_probe(
    endpoint: Endpoint,
    prompt: str,
    max_tokens: int,
    mode: str = "cold",
    target_tokens: int = 0,
) -> Sample:
    """POST one completion request and return its throughput sample.

    A cold probe disables the server prefix cache, so its prefill rate covers the whole
    prompt; a warm probe relies on that cache and only measures the incremental tail.
    """
    body = json.dumps({
        "model": endpoint.model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
        "cache_prompt": mode != "cold",
    }).encode()
    # The endpoint URL is operator-supplied (--llm-url), not untrusted input.
    request = urllib.request.Request(endpoint.url, data=body, headers={"Content-Type": "application/json"})  # ruff: ignore[suspicious-url-open-usage]
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=endpoint.timeout) as response:  # ruff: ignore[suspicious-url-open-usage]
            raw = response.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:200]
        message = f"endpoint returned HTTP {err.code}: {detail}"
        raise RuntimeError(message) from err
    except urllib.error.URLError as err:
        message = f"endpoint unreachable: {err.reason}"
        raise RuntimeError(message) from err
    wall_sec = round(time.monotonic() - start, 2)
    timings = parse_timings(json.loads(raw), wall_sec)
    return Sample(
        mode=mode,
        target_tokens=target_tokens,
        wall_sec=wall_sec,
        prompt_tokens=timings["prompt_tokens"],
        prompt_per_second=timings["prompt_per_second"],
        predicted_tokens=timings["predicted_tokens"],
        predicted_per_second=timings["predicted_per_second"],
        estimated=timings["estimated"],
    )


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def format_markdown(samples: list[Sample], llm_model: str) -> str:
    """Return one Markdown table with a row per sample."""
    lines = [
        f"### Throughput ({llm_model})",
        "",
        "| Mode | Eval tokens | Wall (s) | Prefill tok/s | Decode tok/s | Estimated |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {s.mode} | {s.prompt_tokens if s.prompt_tokens is not None else s.target_tokens} "
        f"| {s.wall_sec} | {_fmt(s.prompt_per_second)} | {_fmt(s.predicted_per_second)} "
        f"| {'yes' if s.estimated else 'no'} |"
        for s in samples
    )
    return "\n".join(lines)


def summarize(samples: list[Sample]) -> dict[str, Any]:
    """Return the median warm prefill and decode throughput per target size."""
    summary: dict[str, Any] = {}
    targets = sorted({s.target_tokens for s in samples})
    for target in targets:
        warm = [s for s in samples if s.target_tokens == target and s.mode == "warm" and not s.estimated]
        prefill = [s.prompt_per_second for s in warm if s.prompt_per_second]
        decode = [s.predicted_per_second for s in warm if s.predicted_per_second]
        summary[str(target)] = {
            "prefill_tok_s": round(statistics.median(prefill), 2) if prefill else None,
            "decode_tok_s": round(statistics.median(decode), 2) if decode else None,
            "samples": len(warm),
        }
    return summary


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        message = "must be a positive integer"
        raise argparse.ArgumentTypeError(message)
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure llama.cpp prefill and decode throughput on a completion endpoint."
    )
    parser.add_argument("--llm-url", default=DEFAULT_URL, help="Completion endpoint URL")
    parser.add_argument("--llm-model", required=True, help="Model name served by the endpoint")
    parser.add_argument(
        "--token-target",
        type=positive_int,
        action="append",
        default=None,
        help="Prompt size in tokens, repeatable (default: 1000 5000 11000)",
    )
    parser.add_argument("--repeat", type=positive_int, default=2, help="Warm repetitions per size")
    parser.add_argument("--max-tokens", type=positive_int, default=32)
    parser.add_argument("--timeout", type=positive_int, default=900)
    parser.add_argument("--output", default="bench_throughput.json")
    args = parser.parse_args(argv)

    endpoint = Endpoint(args.llm_url, args.llm_model, args.timeout)
    targets = args.token_target or [1000, 5000, 11000]
    samples: list[Sample] = []
    for target in targets:
        prompt = build_prompt(target)
        logger.info("Measuring %d target tokens (cold)", target)
        samples.append(run_probe(endpoint, prompt, args.max_tokens, mode="cold", target_tokens=target))
        for iteration in range(args.repeat):
            logger.info("Measuring %d target tokens (warm %d)", target, iteration + 1)
            samples.append(run_probe(endpoint, prompt, args.max_tokens, mode="warm", target_tokens=target))

    report = {
        "llm_url": args.llm_url,
        "llm_model": args.llm_model,
        "summary": summarize(samples),
        "samples": [asdict(s) for s in samples],
    }
    with pathlib.Path(args.output).open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    # The Markdown table is the command's stdout output, consumed by callers.
    print(format_markdown(samples, args.llm_model))  # ruff: ignore[print]
    logger.info("Wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
