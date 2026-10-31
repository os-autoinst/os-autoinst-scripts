# Copyright SUSE LLC
"""Tests for the wording of the openqa-llm-investigate prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openqa_llm_investigate.text import format_step_summary_line
from tests.test_openqa_llm_investigate import llm_investigate, setup_mock_client

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_DIED_STEP = {"result": "fail", "text_data": "# Test died: no candidate needle with tag(s) 'gdm-crash' matched"}


def test_prompts_require_evidence_and_treat_needle_tags_as_expectations(mocker: MockerFixture) -> None:
    mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate._build_failures", return_value=(153, 0))
    client = setup_mock_client(mocker)

    llm_investigate.investigate("123")

    intro, brief, verdict = (m["content"] for m in client.post.call_args.kwargs["json"]["messages"][0:5:2])
    assert "no match for tags:" in format_step_summary_line({"num": 3, "result": "fail", "tags": ["gdm-crash"]})
    assert "needle with tag(s)" in format_step_summary_line(_DIED_STEP)
    assert "Needle tags in `no match for tags:` lines or in `needle with tag(s)` name screens" in intro
    assert "the test waited for and did not see." in intro
    assert "`Test died` message are guesses, not findings" in intro
    assert "If no log line shows a cause, REASON only says which expected screen or command failed." in intro
    assert "Likely cause" not in brief
    assert "Suggested actions" not in brief
    assert "state only what the\nanalysis shows" in brief
    assert "153 failures across this build alone do not show a backend infrastructure outage" in verdict
    assert "only if the analysis shows\nan error of the worker, its network or an external server" in verdict
    assert "not of the tested system itself." in verdict
