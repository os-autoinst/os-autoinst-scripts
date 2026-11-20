# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.verdict."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from openqa_llm_investigate import verdict
from tests.test_openqa_llm_investigate import CHAT_REPLIES, llm_investigate, setup_mock_client

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_REPLY = "Classification: Product bug\n**BISECT: YES** \u2014 isolated\nRestart: YES \u2014 flaky network"


def _both_no(streak: int) -> str:
    reason = f"failed at least {streak} times in a row"
    return "\n".join([
        "Classification: Product bug",
        f"BISECT: NO \u2014 {reason}, so not a new regression",
        f"Restart: NO \u2014 {reason}, so a rerun is likely to fail",
    ])


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Classification: Test issue\n**BISECT: YES** \u2014 isolated", True),
        ("- bisect: yes \u2014 isolated", True),
        ("BISECT: YES?\nBISECT: NO", False),
        ("`BISECT: NO` \u2014 widespread\n", False),
        ("**BISECT:** YES \u2014 isolated", True),
        ("**BISECT**: NO", False),
        ("BISECT: **YES**", True),
        ("__BISECT__: _YES_", True),
        ("### BISECT: YES", True),
        ("1. **BISECT:** NO \u2014 known", False),
        ("BISECT: NOT sure", None),
        ("Step 3. BISECT: YES", None),
        ("Classification: Test issue\nRestart: NO", None),
        ("Other jobs: BISECT: YES would be wrong.", None),
        ("", None),
    ],
)
def test_bisect_verdict_reads_the_last_bisect_line(reply: str, *, expected: bool | None) -> None:
    assert verdict.bisect_verdict(reply) is expected


@pytest.mark.parametrize(
    ("streak", "expected"),
    [(95, _both_no(95)), (3, _both_no(3)), (1, _REPLY), (2, _REPLY), (None, _REPLY)],
    ids=["long-streak", "three-failures", "first-failure", "two-failures", "not-in-history"],
)
def test_enforce_history_rules(streak: int | None, expected: str) -> None:
    assert verdict.enforce_history_rules(_REPLY, streak) == expected


def test_enforce_history_rules_keeps_the_reason_of_a_no() -> None:
    reply = "BISECT: NO \u2014 1 other job(s) in this build fail in the same module\nRestart: NO \u2014 needle mismatch"
    assert verdict.enforce_history_rules(reply, 25) == reply


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "Classification: Product bug\nBISECT: NO \u2014 widespread\nRestart: NO \u2014 deterministic",
            "Classification: Product bug\nBISECT: NO \u2014 widespread\nRestart: NO \u2014 deterministic",
        ),
        (
            "Classification: Product bug BISECT: NO \u2014 widespread Restart: NO \u2014 deterministic",
            "Classification: Product bug\nBISECT: NO \u2014 widespread\nRestart: NO \u2014 deterministic",
        ),
        ("**BISECT:** YES \u2014 isolated", "BISECT: YES \u2014 isolated"),
        (
            "1. **BISECT:** NO \u2014 known\n2. Restart: YES \u2014 flaky",
            "BISECT: NO \u2014 known\nRestart: YES \u2014 flaky",
        ),
        ("Other jobs: BISECT: YES would be wrong.\nBISECT: NO \u2014 widespread", "BISECT: NO \u2014 widespread"),
        ("BISECT: YES\nBISECT: NO", "BISECT: NO"),
        ("Hope this helps.", "Hope this helps."),
        (
            "Classification: Product bug\nBISECT: NO \u2014 widespread\nRestart: YES\n\nYours, the assistant",
            "Classification: Product bug\nBISECT: NO \u2014 widespread\nRestart: YES",
        ),
        (
            "Classification: Product bug\nBISECT: NO \u2014 widespread\n\nBISECT: YES",
            "Classification: Product bug\nBISECT: YES",
        ),
        ("BISECT:\nRestart: NO \u2014 deterministic", "BISECT: \nRestart: NO \u2014 deterministic"),
        ("BISECT: YES | NO \u2014 your own short reason", "BISECT: "),
        ("Classification: Product bug | Test issue", "Classification: "),
        ("BISECT: NO \u2014 failed in test_module_foo", "BISECT: NO \u2014 failed in test_module_foo"),
        ("BISECT: NO \u2014 the log shows a|b mismatch", "BISECT: NO \u2014 the log shows a|b mismatch"),
        ("BISECT: YES/NO \u2014 your own short reason", "BISECT: "),
        ("BISECT: YES or NO \u2014 your own short reason", "BISECT: "),
        (
            "BISECT: NO \u2014 widespread\nNote: **BISECT: YES** needs an isolated failure",
            "BISECT: NO \u2014 widespread",
        ),
        ("Reason: \nBISECT: YES \u2014 isolated\nRestart: NO", "BISECT: YES \u2014 isolated\nRestart: NO"),
        ("BISECT: NO \u2014 widespread\nIf unsure, BISECT: YES is safer.", "BISECT: NO \u2014 widespread"),
        ("BISECT: NO \u2014 Product bug or Test issue", "BISECT: NO \u2014 Product bug or Test issue"),
        (
            "BISECT: NO \u2014 widespread, BISECT: YES would need an isolated failure",
            "BISECT: NO \u2014 widespread,",
        ),
        ("BISECT: NO \u2014 `foo` died", "BISECT: NO \u2014 `foo` died"),
    ],
    ids=[
        "canonical",
        "merged-in-one-line",
        "emphasized-label",
        "numbered",
        "label-in-sentence",
        "last-answer-wins",
        "no-label",
        "trailing-signature-dropped",
        "repeated-label-on-its-own-line",
        "empty-answer-keeps-own-line",
        "template-echo",
        "classification-echo",
        "underscore-in-reason",
        "pipe-in-reason",
        "slash-echo",
        "or-echo",
        "markup-quoted-label",
        "colon-ends-the-previous-line",
        "label-in-trailing-prose",
        "choices-named-in-a-reason",
        "repeated-label-in-a-reason",
        "code-span-in-a-reason",
    ],
)
def test_normalize_verdict_returns_one_canonical_line_per_label(reply: str, expected: str) -> None:
    assert verdict.normalize_verdict(reply) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Classification: Product bug\nBISECT: NO \u2014 widespread", "Product bug"),
        ("BISECT: NO \u2014 widespread", None),
        ("Classification: \nBISECT: NO \u2014 widespread", None),
    ],
    ids=["classification", "no-classification", "empty-classification"],
)
def test_classification_reads_the_classification_line(text: str, expected: str | None) -> None:
    assert verdict.classification(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("BISECT: YES \u2014 isolated\nRestart: NO", "* **BISECT:** YES \u2014 isolated\n* **Restart:** NO"),
        ("Classification: Product bug\nBISECT: NO", "* **BISECT:** NO"),
        ("Hope this helps.", "* Hope this helps."),
        ("Classification: Product bug\nRestart: ", ""),
        ("BISECT: NO \u2014 \nRestart: NO", "* **BISECT:** NO\n* **Restart:** NO"),
    ],
    ids=["labels-bolded", "classification-dropped", "label-less-prose", "nothing-to-render", "dangling-separator"],
)
def test_render_verdict_lines_bolds_labels_and_drops_classification(text: str, expected: str) -> None:
    assert verdict.render_verdict_lines(text, "") == expected


def test_render_verdict_lines_appends_the_legend_only_for_a_verdict_line() -> None:
    legend = "\n\n*BISECT gates an automatic bisect run.*"
    assert (
        verdict.render_verdict_lines("BISECT: YES \u2014 isolated", legend)
        == "* **BISECT:** YES \u2014 isolated" + legend
    )
    assert not verdict.render_verdict_lines("Classification: Product bug", legend)


@pytest.mark.parametrize(
    ("bisect", "restart"),
    [
        ("**BISECT:** YES \u2014 isolated", "**Restart:** YES"),
        ("**BISECT**: YES", "**Restart**: YES"),
        ("BISECT: **YES**", "Restart: **YES** \u2014 flaky"),
        ("__BISECT__: _YES_", "_Restart_: _YES_"),
        ("### BISECT: YES", "2. Restart: YES"),
    ],
)
def test_enforce_history_rules_rewrites_emphasized_lines(bisect: str, restart: str) -> None:
    out = verdict.enforce_history_rules(f"{bisect}\n{restart}", 3)
    assert out == _both_no(3).split("\n", 1)[1]


def test_investigate_without_a_bisect_line_skips_the_check_before_posting(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    reviewed = mocker.patch("llm_investigate.is_reviewed", side_effect=[False, True])
    mocker.patch.dict(CHAT_REPLIES, {5: "Classification: Product bug"})
    setup_mock_client(mocker)
    llm_investigate.investigate("123")
    mock_print.assert_called_once_with("https://openqa.opensuse.org/tests/123")
    mock_post.assert_not_called()
    assert reviewed.call_count == 1


def test_investigate_forces_no_on_a_copied_template_when_other_jobs_fail_in_the_same_module(
    mocker: MockerFixture,
) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    mocker.patch("llm_investigate._build_failures", return_value=(3, 2))
    mocker.patch.dict(CHAT_REPLIES, {5: "Classification: Test issue\nBISECT: YES | NO \u2014 your own short reason"})
    setup_mock_client(mocker)
    llm_investigate.investigate("123")
    mock_print.assert_not_called()
    assert "* **BISECT:** NO \u2014 2 other job(s) in this build fail in the same module" in mock_post.call_args[0][2]
