# Copyright SUSE LLC
"""Tests for the comment notes on what the LLM did not see."""

from __future__ import annotations

import dataclasses
import math

from tests.test_openqa_llm_investigate import llm_investigate

_OPTS = llm_investigate.Options("http://llm", "m", None, 1, 50, 2, 50, 5)


def test_build_prompt_notes_test_commits_left_out_while_the_analysis_fits() -> None:
    job = {"testresults": [{"name": "isosize", "category": "installation", "result": "failed"}]}
    log = "".join(f"{i:07x} Fix isosize {'x' * 60}\n tests/installation/isosize.pm | 1 +\n" for i in range(10))
    stats = {"investigation_info": "", "investigation": {"test_log": log}}
    analysis = "a\nb\nc\n"
    with_commits, _ = llm_investigate._build_prompt(job, analysis, stats, _OPTS)
    without_commits, _ = llm_investigate._build_prompt(job, analysis, {"investigation_info": ""}, _OPTS)
    halfway = (len(with_commits) + len(without_commits)) // 2
    tokens = llm_investigate.RESPONSE_RESERVE_TOKENS + math.ceil(halfway / llm_investigate.CHARS_PER_TOKEN)

    _, notes = llm_investigate._build_prompt(job, analysis, stats, dataclasses.replace(_OPTS, context_tokens=tokens))

    assert notes == [
        f"The test commits since the last good job were left out of the prompt to fit {tokens} tokens.",
        "The failure analysis was cut to its first 2 lines; the LLM did not see its end.",
    ]


def test_comment_notes_keep_the_cut_notes_after_the_history_note() -> None:
    notes = llm_investigate._comment_notes(["The failure analysis was cut."], _OPTS, job_found=False)

    assert notes == (
        "\n\n*Note: Job not found in recent history (limit 50). Streak count may be inaccurate.*"
        "\n\n*Note: The failure analysis was cut.*"
    )
