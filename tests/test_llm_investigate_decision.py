# Copyright SUSE LLC
"""Tests for the class that a decision model sets in the Classification line of the summary."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from openqa_llm_investigate import decision
from tests.test_openqa_llm_investigate import CHAT_REPLIES, llm_investigate, setup_mock_client

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_URL = "http://decision/v1/systemone"
_POST = "openqa_llm_investigate.decision.httpx.post"
_HEADERS = {"User-Agent": "test"}
_VERDICT = CHAT_REPLIES[5]
_REST = _VERDICT.split("\n", 1)[1]
_DECIDED_INFRA = f"Classification: Infrastructure problem\n  (the analysis model says Product bug)\n{_REST}"
_UNANSWERED = f"Classification: Product bug\n  (no answer from the decision model)\n{_REST}"
_OPTS = llm_investigate.Options("http://llm", "m", None, 1, 50, 1000, 50, 5)


def _prompt(analysis: str) -> str:
    """Return the first LLM prompt of a job with every optional section before the failure analysis."""
    job = {
        "test": "my_test",
        "result": "failed",
        "testresults": [{"name": "isosize", "category": "installation", "result": "failed"}],
        "settings": {"BUILD": "1.0", "ARCH": "x86_64", "LLM_PROMPT_EXTENSION": "Failure analysis is welcome."},
    }
    stats = {
        "investigation_info": "needles_diff_stat: 1 file changed",
        "investigation": {"test_log": "0123abc Fix isosize\n tests/installation/isosize.pm | 1 +\n"},
        "submission_info": {1234: ["bash"]},
    }
    return llm_investigate._build_prompt(job, analysis, stats, _OPTS)[0]


def _answers(
    env: float | str, product: float | str = 0.5, test: float | str = 0.3, infra: float = 0.2
) -> dict[str, Any]:
    """Return the answers of a decision server: the class probabilities, and `env` for yes."""
    choice = {"type": "choice", "probabilities": {"product": product, "test": test, "infra": infra}}
    return {"guide": choice, "env": {"type": "noul", "noul": env}}


def _server(mocker: MockerFixture, answers: Any) -> Mock:
    """Patch the request to the decision server to reply with `answers`; return the mock of the request."""
    return mocker.patch(_POST, return_value=Mock(json=Mock(return_value={"answers": answers})))


def test_state_of_keeps_the_job_header_and_the_failure_analysis() -> None:
    reviews = "Earlier reviews of runs failing in the same module: #122 bsc#1."
    prompt = _prompt(f"a line of the log\n{reviews}\nFailure analysis (twice) and the last line\n")
    sections = ("Additional instructions", "Submissions under test", "Investigation info", "0123abc", reviews)
    assert all(section in prompt for section in sections)

    assert decision.state_of(prompt) == (
        "You are analyzing an openQA test run failure.\n"
        "Job details:\n"
        "- Test: my_test\n"
        "- Failed module(s): isosize\n"
        "- Group: unknown\n"
        "- Build: 1.0\n"
        "- Distri: unknown\n"
        "- Version: unknown\n"
        "- Arch: x86_64\n"
        "- Machine: unknown\n"
        "- Flavor: unknown\n"
        "- Result: failed\n"
        "\n"
        "Failure analysis (context window of 50 lines):\n"
        "a line of the log\n"
        "Failure analysis (twice) and the last line"
    )


def test_state_of_keeps_the_start_of_a_long_failure_analysis() -> None:
    state = decision.state_of(_prompt(("x" * 99 + "\n") * 400))
    assert len(state) == decision.STATE_CHARS
    assert state.startswith("You are analyzing an openQA test run failure.")


def test_questions_ask_for_the_class_and_for_the_environment() -> None:
    asked = decision.questions()
    assert [(name, q["type"]) for name, q in asked.items()] == [("guide", "choice"), ("env", "noul")]
    assert list(asked["guide"]["criteria"]) == ["product", "test", "infra"]
    assert all(set(c) == {"definition", "not_for", "examples"} for c in asked["guide"]["criteria"].values())


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        pytest.param(_answers(0.3), "infra", id="environment-at-the-threshold"),
        pytest.param(_answers(0.29), "product", id="environment-below-the-threshold"),
        pytest.param(_answers(0.9, infra=0.0), "infra", id="only-the-yes-no-question-decides-on-infra"),
        pytest.param(_answers(0.0, product=0.1, test=0.2, infra=0.7), "test", id="the-class-question-never-says-infra"),
        pytest.param(_answers(0.0, product=0.4, test=0.4), "product", id="a-tie-is-a-product-bug"),
        pytest.param(_answers("0.3"), "infra", id="environment-as-text"),
        pytest.param(_answers(0.0, product="1e-05", test="0.2"), "test", id="probabilities-as-text"),
    ],
)
def test_decide(answers: dict[str, Any], expected: str) -> None:
    assert decision.decide(answers) == expected


@pytest.mark.parametrize(
    ("verdict", "name", "expected"),
    [
        pytest.param(_VERDICT, "Product bug", _VERDICT, id="same-class"),
        pytest.param(_VERDICT, "Infrastructure problem", _DECIDED_INFRA, id="other-class"),
        pytest.param(
            "1. **Classification:** test issue.\nBISECT: NO",
            "Product bug",
            "Classification: Product bug\n  (the analysis model says test issue)\nBISECT: NO",
            id="emphasized-line",
        ),
        pytest.param(
            "classification: product bug.\nBISECT: NO",
            "Product bug",
            "Classification: Product bug\nBISECT: NO",
            id="same-class-in-other-case",
        ),
        pytest.param(
            "Classification: Product bug (kernel panic)\nBISECT: NO",
            "Product bug",
            "Classification: Product bug\n  (the analysis model says Product bug (kernel panic))\nBISECT: NO",
            id="class-with-more-words",
        ),
        pytest.param(
            "Classification: Product bug | Test issue | Infrastructure problem\nBISECT: NO",
            "Test issue",
            "Classification: Test issue\n"
            "  (the analysis model says Product bug | Test issue | Infrastructure problem)\nBISECT: NO",
            id="copied-template",
        ),
        pytest.param(
            "**Classification** - Product bug\nBISECT: NO",
            "Test issue",
            "Classification: Test issue\n  (the analysis model says Product bug)\nBISECT: NO",
            id="dash-for-the-colon",
        ),
        pytest.param(
            "Classification \u2014 Product bug\nBISECT: NO",
            "Test issue",
            "Classification: Test issue\n  (the analysis model says Product bug)\nBISECT: NO",
            id="em-dash-for-the-colon",
        ),
        pytest.param(
            "Classification : Product bug\nBISECT: NO",
            "Test issue",
            "Classification: Test issue\n  (the analysis model says Product bug)\nBISECT: NO",
            id="space-before-the-colon",
        ),
        pytest.param(
            "Classification\uff1aProduct bug\nBISECT: NO",
            "Test issue",
            "Classification: Test issue\n  (the analysis model says Product bug)\nBISECT: NO",
            id="full-width-colon",
        ),
        pytest.param(
            "Classification-based on the log: unclear.\nBISECT: NO",
            "Test issue",
            "Classification: Test issue\n  (no class from the analysis model)\n"
            "Classification-based on the log: unclear.\nBISECT: NO",
            id="other-prose-is-no-class-line",
        ),
        pytest.param(
            "Classification:\nBISECT: NO",
            "Test issue",
            "Classification: Test issue\n  (no class from the analysis model)\nBISECT: NO",
            id="empty-class-line",
        ),
        pytest.param(
            "BISECT: NO",
            "Test issue",
            "Classification: Test issue\n  (no class from the analysis model)\nBISECT: NO",
            id="no-class-line",
        ),
        pytest.param(
            "Classification: Test issue\nOr rather:\nClassification: Product bug\nBISECT: NO",
            "Infrastructure problem",
            "Classification: Test issue\nOr rather:\nClassification: Infrastructure problem\n"
            "  (the analysis model says Product bug)\nBISECT: NO",
            id="last-of-several-class-lines",
        ),
        pytest.param(
            "The classification: unclear.\nBISECT: NO",
            "Test issue",
            "Classification: Test issue\n  (no class from the analysis model)\n"
            "The classification: unclear.\nBISECT: NO",
            id="prose-is-no-class-line",
        ),
    ],
)
def test_with_class(verdict: str, name: str, expected: str) -> None:
    assert decision._with_class(verdict, name) == expected


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        pytest.param(_VERDICT, _UNANSWERED, id="below-the-class-line"),
        pytest.param(
            "Classification: Test issue\nOr rather:\n1. **Classification:** product bug.\nBISECT: NO",
            "Classification: Test issue\nOr rather:\n1. **Classification:** product bug.\n"
            "  (no answer from the decision model)\nBISECT: NO",
            id="below-the-last-of-several-class-lines",
        ),
        pytest.param("BISECT: NO", "  (no answer from the decision model)\nBISECT: NO", id="no-class-line"),
    ],
)
def test_without_answer(verdict: str, expected: str) -> None:
    assert decision._without_answer(verdict) == expected


def test_decided_verdict_asks_once_for_the_state_of_the_prompt(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8))
    prompt = _prompt("a line of the log\n")

    decided = decision.decided_verdict(_URL, prompt, _VERDICT, headers=_HEADERS, timeout=7)

    assert decided == _DECIDED_INFRA
    body = {"state": decision.state_of(prompt), "questions": decision.questions()}
    post.assert_called_once_with(_URL, json=body, headers=_HEADERS, timeout=7)


def test_decided_verdict_caps_the_timeout_at_max_seconds(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8))
    decision.decided_verdict(_URL, _prompt("a line\n"), _VERDICT, headers=_HEADERS, timeout=600)
    assert post.call_args.kwargs["timeout"] == decision.MAX_SECONDS == 300


def test_decided_verdict_asks_nothing_without_a_url(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8))
    assert decision.decided_verdict("", _prompt("a line\n"), _VERDICT, headers=_HEADERS, timeout=7) == _VERDICT
    post.assert_not_called()


def _status_500(*_: Any, **__: Any) -> Mock:
    request = httpx.Request("POST", _URL)
    error = httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
    return Mock(raise_for_status=Mock(side_effect=error))


@pytest.mark.parametrize(
    "server",
    [
        pytest.param({"side_effect": httpx.ConnectError("refused")}, id="connection-refused"),
        pytest.param({"side_effect": httpx.ReadTimeout("slow")}, id="timeout"),
        pytest.param({"side_effect": httpx.InvalidURL("no host")}, id="invalid-url"),
        pytest.param({"side_effect": _status_500}, id="http-500"),
        pytest.param({"return_value": Mock(json=Mock(side_effect=ValueError("no JSON")))}, id="no-json"),
        pytest.param({"return_value": Mock(json=Mock(side_effect=RecursionError("deep")))}, id="deeply-nested-json"),
        pytest.param({"return_value": Mock(json=Mock(return_value=["answers"]))}, id="json-list"),
        pytest.param({"return_value": Mock(json=Mock(return_value={"error": "busy"}))}, id="no-answers"),
        pytest.param({"return_value": Mock(json=Mock(return_value={"answers": None}))}, id="null-answers"),
        pytest.param(
            {"return_value": Mock(json=Mock(return_value={"answers": _answers(0.0) | {"env": {"type": "noul"}}}))},
            id="no-probability",
        ),
        pytest.param(
            {"return_value": Mock(json=Mock(return_value={"answers": _answers(0.0) | {"env": {"noul": None}}}))},
            id="null-probability",
        ),
        pytest.param(
            {"return_value": Mock(json=Mock(return_value={"answers": _answers("yes")}))},
            id="probability-no-number",
        ),
        pytest.param(
            {"return_value": Mock(json=Mock(return_value={"answers": _answers(10**400)}))},
            id="probability-too-large",
        ),
        pytest.param(
            {"return_value": Mock(json=Mock(return_value={"answers": {"env": {"type": "noul", "noul": 0.0}}}))},
            id="no-class-answers",
        ),
    ],
)
def test_decided_verdict_keeps_the_class_of_the_llm_without_a_usable_answer(
    mocker: MockerFixture, caplog: pytest.LogCaptureFixture, server: dict[str, Any]
) -> None:
    mocker.patch(_POST, **server)
    with caplog.at_level(logging.WARNING):
        decided = decision.decided_verdict(_URL, _prompt("a line\n"), _VERDICT, headers=_HEADERS, timeout=7)
    assert decided == _UNANSWERED
    assert f"No class from the decision model at {_URL}" in caplog.text


def _comment(mocker: MockerFixture, **options: Any) -> str:
    """Investigate job 123 against mocked openQA and LLM servers and return the comment posted on the job."""
    mock_post = mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker)
    llm_investigate.investigate("123", **options)
    assert client.post.call_count == 3  # the decision model is no fourth question to the LLM
    mock_post.assert_called_once()
    return mock_post.call_args[0][2]


def test_investigate_posts_the_class_of_the_decision_model(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8))

    comment = _comment(mocker, decision_url=_URL, timeout=7)

    assert f"\n\n{_DECIDED_INFRA}\n\n" in comment  # BISECT and Restart stay the LLM's
    post.assert_called_once()
    assert post.call_args.args == (_URL,)
    assert post.call_args.kwargs["json"]["state"].startswith("You are analyzing an openQA test run failure.")
    assert post.call_args.kwargs["json"]["questions"] == decision.questions()
    assert post.call_args.kwargs["headers"] == {"User-Agent": llm_investigate.USER_AGENT}
    assert post.call_args.kwargs["timeout"] == 7


def test_investigate_says_so_when_the_decision_server_fails(mocker: MockerFixture) -> None:
    without_decision_model = _comment(mocker)
    assert without_decision_model.count(_VERDICT) == 1
    post = mocker.patch(_POST, side_effect=httpx.ConnectError("refused"))

    assert _comment(mocker, decision_url=_URL) == without_decision_model.replace(_VERDICT, _UNANSWERED)
    post.assert_called_once()


def test_investigate_asks_no_decision_model_by_default(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8))
    assert f"\n\n{_VERDICT}\n\n" in _comment(mocker)
    post.assert_not_called()


def test_investigate_dry_run_prints_the_class_of_the_decision_model(mocker: MockerFixture) -> None:
    _server(mocker, _answers(0.8))
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    setup_mock_client(mocker)

    llm_investigate.investigate("123", dry=True, decision_url=_URL)

    mock_post.assert_not_called()
    assert mock_print.call_args[0][0].endswith(f"\n\n{_DECIDED_INFRA}")


@pytest.mark.parametrize(
    ("options", "reply"),
    [
        pytest.param({"print_prompt": True}, _VERDICT, id="print-prompt"),
        pytest.param({}, "Classification: Product bug", id="no-summary-without-a-bisect-verdict"),
    ],
)
def test_investigate_asks_no_decision_model_without_a_summary(
    mocker: MockerFixture, options: dict[str, Any], reply: str
) -> None:
    post = _server(mocker, _answers(0.8))
    mock_post = mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    mocker.patch.dict(CHAT_REPLIES, {5: reply})
    setup_mock_client(mocker)

    llm_investigate.investigate("123", decision_url=_URL, **options)

    post.assert_not_called()
    mock_post.assert_not_called()


@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        pytest.param([], {}, "", id="off-by-default"),
        pytest.param([], {"LLM_DECISION_URL": _URL}, _URL, id="environment"),
        pytest.param(["--decision-url", _URL], {"LLM_DECISION_URL": "http://other"}, _URL, id="option-wins"),
    ],
)
def test_decision_url_option(mocker: MockerFixture, args: list[str], env: dict[str, str], expected: str) -> None:
    perform = mocker.patch("llm_investigate._perform_investigation")
    result = CliRunner().invoke(llm_investigate.app, ["123", *args], env={"LLM_DECISION_URL": None} | env)
    assert result.exit_code == 0, result.output
    assert perform.call_args[0][3].decision_url == expected
