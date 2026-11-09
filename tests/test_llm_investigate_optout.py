# Copyright SUSE LLC
"""Tests for opting a job out of the LLM investigation with the openQA variable NO_LLM_INVESTIGATION=1."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.test_openqa_llm_investigate import llm_investigate, setup_mock_client

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_URL = "https://openqa.opensuse.org/tests/123"


def _job(value: str) -> dict[str, Any]:
    """Return a failed job reply with the setting NO_LLM_INVESTIGATION=`value`."""
    settings = {"BUILD": "1.0", "DISTRI": "opensuse", "VERSION": "Tumbleweed", "ARCH": "x86_64", "FLAVOR": "DVD"}
    settings["NO_LLM_INVESTIGATION"] = value
    return {
        "api/v1/jobs": {
            "job": {
                "id": 123,
                "result": "failed",
                "test": "my_test",
                "testresults": [{"name": "isosize", "result": "failed"}],
                "settings": settings,
            }
        }
    }


@pytest.mark.parametrize(
    ("value", "investigates"),
    [
        pytest.param("1", False, id="1-opts-out"),
        pytest.param("0", True, id="0-is-not-an-opt-out"),
    ],
)
def test_only_1_opts_the_job_out(mocker: MockerFixture, *, value: str, investigates: bool) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mock_print = mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides=_job(value))

    llm_investigate.investigate("123")

    mock_print.assert_called_once_with(_URL)  # handed over to the standard investigation either way
    assert client.post.called is investigates  # the LLM was asked
    assert mock_post.called is investigates  # the summary was commented


def test_force_ignores_the_opt_out(mocker: MockerFixture) -> None:
    mock_post = mocker.patch("llm_investigate.post_comment")
    mocker.patch("llm_investigate.typer.echo")
    client = setup_mock_client(mocker, overrides=_job("1"))

    llm_investigate.investigate("123", force=True)

    assert client.post.called
    mock_post.assert_called_once()
