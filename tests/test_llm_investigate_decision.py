# Copyright SUSE LLC
"""Tests for the class that a decision model sets in the Classification line of the summary."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
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


def _server(mocker: MockerFixture, answers: Any, **reply: Any) -> Mock:
    """Patch the request to the decision server to reply with `answers`; return the mock of the request."""
    return mocker.patch(_POST, return_value=Mock(json=Mock(return_value={"answers": answers} | reply)))


def _clock(mocker: MockerFixture, seconds: float) -> None:
    """Let the request to the decision server take `seconds`."""
    mocker.patch.object(decision, "time", Mock(monotonic=Mock(side_effect=[100.0, 100.0 + seconds])))


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _rules() -> str:
    return _sha8(json.dumps([decision.questions(), decision.ENV_THRESHOLD]))


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

    decided, _ = decision.decided_verdict(_URL, prompt, _VERDICT, headers=_HEADERS, timeout=7)

    assert decided == _DECIDED_INFRA
    body = {"state": decision.state_of(prompt), "questions": decision.questions()}
    post.assert_called_once_with(_URL, json=body, headers=_HEADERS, timeout=7)


def test_decided_verdict_caps_the_timeout_at_max_seconds(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8))
    decision.decided_verdict(_URL, _prompt("a line\n"), _VERDICT, headers=_HEADERS, timeout=600)
    assert post.call_args.kwargs["timeout"] == decision.MAX_SECONDS == 300


def test_decided_verdict_asks_nothing_without_a_url(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8))
    decided = decision.decided_verdict("", _prompt("a line\n"), _VERDICT, headers=_HEADERS, timeout=7)
    assert decided == (_VERDICT, {})
    post.assert_not_called()


def test_decided_verdict_records_the_model_and_its_raw_answers(mocker: MockerFixture) -> None:
    _server(mocker, _answers(0.10249, product="0.50651", test=0.29449), model="org/elephant-1:Q4_K_M")
    _clock(mocker, 89.66)
    prompt = _prompt("a line of the log\n")

    _, record = decision.decided_verdict(_URL, prompt, _VERDICT, headers=_HEADERS, timeout=7)

    assert record == {
        "decision": "org/elephant-1:Q4_K_M",
        "env": "0.102",
        "product": "0.507",
        "test": "0.294",
        "infra": "0.200",
        "secs": "89.7",
        "state": _sha8(decision.state_of(prompt)),
        "rules": _rules(),
    }


def test_decided_verdict_records_a_job_text_with_a_lone_surrogate(mocker: MockerFixture) -> None:
    _server(mocker, _answers(0.8))
    prompt = _prompt("a \ud800 line of the log\n")
    assert "\ud800" in decision.state_of(prompt)

    verdict, record = decision.decided_verdict(_URL, prompt, _VERDICT, headers=_HEADERS, timeout=7)

    assert verdict == _DECIDED_INFRA
    assert re.fullmatch(r"[0-9a-f]{8}", record["state"])
    assert record["state"] != _sha8(decision.state_of(prompt.replace("\ud800", "")))


def test_the_recorded_rules_change_with_the_questions_and_with_the_threshold(mocker: MockerFixture) -> None:
    _server(mocker, _answers(0.8))

    def rules() -> str:
        return decision.decided_verdict(_URL, _prompt("a line\n"), _VERDICT, headers=_HEADERS, timeout=7)[1]["rules"]

    recorded = [rules(), rules()]
    mocker.patch.object(decision, "ENV_THRESHOLD", 0.4)
    recorded.append(rules())
    mocker.patch.object(decision, "_ENV_QUESTION", "Did the worker fail?")
    recorded.append(rules())

    assert recorded[0] == recorded[1]
    assert len(set(recorded)) == 3
    assert all(len(r) == 8 for r in recorded)


@pytest.mark.parametrize(
    ("answers", "decided", "scores"),
    [
        pytest.param(
            {"guide": {"probabilities": {"product": 0.5, "test": 0.3}}, "env": {"noul": 0}},
            _VERDICT,
            {"env": "0.000", "product": "0.500", "test": "0.300"},
            id="no-probability-for-infra",
        ),
        pytest.param({"env": {"noul": 0.9}}, _DECIDED_INFRA, {"env": "0.900"}, id="no-class-answer"),
        pytest.param({"guide": [1], "env": {"noul": 0.9}}, _DECIDED_INFRA, {"env": "0.900"}, id="class-answer-a-list"),
        pytest.param(
            {"guide": {"probabilities": "?"}, "env": {"noul": 0.9}},
            _DECIDED_INFRA,
            {"env": "0.900"},
            id="probabilities-a-string",
        ),
        pytest.param(
            _answers(0.9, product=10**400, test="?"),
            _DECIDED_INFRA,
            {"env": "0.900", "infra": "0.200"},
            id="probabilities-no-numbers",
        ),
    ],
)
def test_decided_verdict_records_only_the_usable_ones_of_the_answers_it_does_not_read(
    mocker: MockerFixture, answers: dict[str, Any], decided: str, scores: dict[str, str]
) -> None:
    _server(mocker, answers)

    verdict, record = decision.decided_verdict(_URL, _prompt("a line\n"), _VERDICT, headers=_HEADERS, timeout=7)

    assert verdict == decided
    assert {k: v for k, v in record.items() if k not in {"secs", "state", "rules"}} == {"decision": None} | scores


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
    _clock(mocker, 7.04)
    prompt = _prompt("a line\n")
    with caplog.at_level(logging.WARNING):
        decided, record = decision.decided_verdict(_URL, prompt, _VERDICT, headers=_HEADERS, timeout=7)
    assert decided == _UNANSWERED
    assert record == {"decision": "none", "secs": "7.0", "state": _sha8(decision.state_of(prompt)), "rules": _rules()}
    assert f"No class from the decision model at {_URL}" in caplog.text


@pytest.mark.parametrize(
    ("llm_model", "record", "expected"),
    [
        pytest.param("gemma", {}, "", id="no-record"),
        pytest.param(
            "unsloth/gemma-4-E4B-it-GGUF:Q8_0",
            {"decision": "org/elephant-1@rev+1", "env": "0.102", "secs": "89.7"},
            "\n\n<!-- openqa-llm-investigate llm=unsloth/gemma-4-E4B-it-GGUF:Q8_0 decision=org/elephant-1@rev+1"
            " env=0.102 secs=89.7 -->",
            id="names-and-values",
        ),
        pytest.param(
            None,
            {"decision": None, "env": "0.102"},
            "\n\n<!-- openqa-llm-investigate env=0.102 -->",
            id="servers-that-name-no-model",
        ),
        pytest.param(
            "a --> **b** bsc#1\n<script> \u2014\u00e9",
            {"decision": "x\n--> y", "secs": "0.1"},
            "\n\n<!-- openqa-llm-investigate llm=a-bbsc1script decision=x-y secs=0.1 -->",
            id="names-cannot-close-the-comment",
        ),
        pytest.param(
            "http://x/y--",
            {"decision": "a-\u00e9-b---c", "secs": "0.1"},
            "\n\n<!-- openqa-llm-investigate llm=http://x/y- decision=a-b-c secs=0.1 -->",
            id="names-without-a-double-dash",
        ),
        pytest.param(
            "la bel:force_result:passed",
            {"decision": "label:label:x", "secs": "0.1"},
            "\n\n<!-- openqa-llm-investigate llm=label_force_result:passed decision=label_label_x secs=0.1 -->",
            id="names-without-a-label",
        ),
        pytest.param(
            "\u00e9",
            {"decision": "", "secs": "0.1"},
            "\n\n<!-- openqa-llm-investigate secs=0.1 -->",
            id="names-with-nothing-to-record",
        ),
        pytest.param(
            ["gemma"],
            {"decision": {"x": ["-->"]}, "secs": "0.1"},
            "\n\n<!-- openqa-llm-investigate secs=0.1 -->",
            id="names-that-are-no-strings",
        ),
        pytest.param(
            "m" * 81,
            {"decision": "none"},
            f"\n\n<!-- openqa-llm-investigate llm={'m' * 80} decision=none -->",
            id="long-name",
        ),
    ],
)
def test_record_line(llm_model: Any, record: dict[str, Any], expected: str) -> None:
    assert llm_investigate._record_line(llm_model, record) == expected


def test_record_line_survives_a_name_too_deeply_nested_to_format() -> None:
    nested: Any = []
    for _ in range(100_000):
        nested = [nested]
    assert not llm_investigate._record_line(nested, {})
    record = llm_investigate._record_line(nested, {"decision": nested, "secs": "0.1"})
    assert record == "\n\n<!-- openqa-llm-investigate secs=0.1 -->"


@pytest.mark.parametrize(
    ("options", "reply"),
    [
        pytest.param({}, {"choices": [{"message": {"content": "hi"}}]}, id="openai"),
        pytest.param({"llm_api": llm_investigate.LlmApi.OLLAMA}, {"message": {"content": "hi"}}, id="ollama"),
        pytest.param(
            {"llm_api": llm_investigate.LlmApi.VERTEX, "llm_model": "claude-haiku-4-5"},
            {"content": [{"type": "text", "text": "hi"}]},
            id="claude",
        ),
    ],
)
def test_call_llm_api_returns_the_model_that_the_server_reports(
    mocker: MockerFixture, options: dict[str, Any], reply: dict[str, Any]
) -> None:
    mocker.patch("llm_investigate._vertex_request", return_value=("http://vertex", "t"))
    opts = dataclasses.replace(_OPTS, **options)
    client = Mock(post=Mock(return_value=Mock(json=Mock(return_value=reply | {"model": "served.gguf"}))))
    messages = [{"role": "user", "content": "q"}]

    assert llm_investigate._call_llm_api(client, opts, messages, 0.3) == ("hi", "served.gguf")
    client.post.return_value.json.return_value = reply
    assert llm_investigate._call_llm_api(client, opts, messages, 0.3) == ("hi", None)


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
    _clock(mocker, 0.04)

    comment = _comment(mocker, decision_url=_URL)

    state = _sha8(post.call_args.kwargs["json"]["state"])
    record = f"<!-- openqa-llm-investigate decision=none secs=0.0 state={state} rules={_rules()} -->"
    assert comment == without_decision_model.replace(_VERDICT, f"{_UNANSWERED}\n\n{record}")
    post.assert_called_once()


def test_investigate_records_the_models_and_the_raw_answers_below_the_verdict(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8), model="org/elephant-1:Q4_K_M")
    _clock(mocker, 89.66)
    mock_post = mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    # a job that is missing in its own history gets a note, which follows the record
    client = setup_mock_client(
        mocker, overrides={"test=": {"jobs": [{"id": 120, "state": "done", "result": "failed"}]}}
    )
    chat = client.post.side_effect

    def named(*args: Any, **kwargs: Any) -> Mock:
        reply = chat(*args, **kwargs).json() | {"model": f"gemma-{len(kwargs['json']['messages'])}.gguf"}
        return Mock(json=Mock(return_value=reply))

    client.post.side_effect = named

    llm_investigate.investigate("123", decision_url=_URL)

    comment = mock_post.call_args[0][2]
    state = _sha8(post.call_args.kwargs["json"]["state"])
    # of the three replies of the LLM, the one with the verdict names the model
    record = (
        "<!-- openqa-llm-investigate llm=gemma-5.gguf decision=org/elephant-1:Q4_K_M env=0.800 product=0.500"
        f" test=0.300 infra=0.200 secs=89.7 state={state} rules={_rules()} -->"
    )
    assert comment.startswith(f"{llm_investigate.COMMENT_MARKER}\n\n")
    assert f"\n\n{_DECIDED_INFRA}\n\n{record}\n\n*Note: Job not found in recent history" in comment
    assert comment.endswith(f"*\n\n{llm_investigate.COMMENT_FOOTER}")
    assert comment.count("<!--") == 1


def test_investigate_asks_no_decision_model_by_default(mocker: MockerFixture) -> None:
    post = _server(mocker, _answers(0.8))
    comment = _comment(mocker)
    assert f"\n\n{_VERDICT}\n\n" in comment
    assert "<!--" not in comment
    post.assert_not_called()


def test_investigate_dry_run_prints_the_class_of_the_decision_model(mocker: MockerFixture) -> None:
    _server(mocker, _answers(0.8))
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    setup_mock_client(mocker)

    llm_investigate.investigate("123", dry=True, decision_url=_URL)

    mock_post.assert_not_called()
    summary, record = mock_print.call_args[0][0].rsplit("\n\n", 1)
    assert summary.endswith(f"\n\n{_DECIDED_INFRA}")
    assert record.startswith("<!-- openqa-llm-investigate env=0.800 product=0.500 test=0.300 infra=0.200 secs=")


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
