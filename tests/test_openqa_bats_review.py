# Copyright SUSE LLC
"""Unit & integration tests for openqa-bats-review (JUnit XML based)."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import pathlib
import sys
from typing import Any
from unittest.mock import Mock

import pytest
from requests.exceptions import RequestException

# Load the script as module "bats_review" (the file is named `openqa-bats-review`)
rootpath = pathlib.Path(__file__).parent.parent.resolve()
loader = importlib.machinery.SourceFileLoader("bats_review", f"{rootpath}/openqa-bats-review")
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
bats_review = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = bats_review
loader.exec_module(bats_review)


#
# Unit tests
#


def test_get_file_success(mocker: pytest.MockerFixture) -> None:
    mock_session = mocker.patch("bats_review.session")
    resp = Mock()
    resp.text = "hello"
    resp.raise_for_status = Mock()
    mock_session.get.return_value = resp

    got = bats_review.get_file("http://example.com/foo.xml")
    assert got == "hello"
    mock_session.get.assert_called_once_with(
        "http://example.com/foo.xml",
        headers={"User-Agent": bats_review.USER_AGENT},
        timeout=bats_review.TIMEOUT,
    )
    resp.raise_for_status.assert_called_once()


def test_get_file_request_exception(mocker: pytest.MockerFixture) -> None:
    mock_session = mocker.patch("bats_review.session")
    mock_log = mocker.patch("bats_review.log")
    mock_session.get.side_effect = RequestException("network")
    with pytest.raises(SystemExit) as exc:
        bats_review.get_file("http://example.com/foo.xml")
    assert exc.value.code == 1
    mock_log.exception.assert_called_once()


def test_get_job_success(mocker: pytest.MockerFixture) -> None:
    with contextlib.suppress(Exception):
        bats_review.get_job.cache_clear()
    mock_session = mocker.patch("bats_review.session")
    resp = Mock()
    resp.json.return_value = {"job": {"id": 123, "state": "done"}}
    resp.raise_for_status = Mock()
    mock_session.get.return_value = resp

    job = bats_review.get_job("http://host/api/v1/jobs/123")
    assert job == {"id": 123, "state": "done"}
    mock_session.get.assert_called_once_with(
        "http://host/api/v1/jobs/123",
        headers={"User-Agent": bats_review.USER_AGENT},
        timeout=bats_review.TIMEOUT,
    )


def test_get_job_request_exception(mocker: pytest.MockerFixture) -> None:
    with contextlib.suppress(Exception):
        bats_review.get_job.cache_clear()
    mock_session = mocker.patch("bats_review.session")
    mock_log = mocker.patch("bats_review.log")
    mock_session.get.side_effect = RequestException("boom")
    with pytest.raises(SystemExit) as exc:
        bats_review.get_job("http://host/api/v1/jobs/123")
    assert exc.value.code == 1
    mock_log.exception.assert_called_once()


def test_grep_failures_success(mocker: pytest.MockerFixture) -> None:
    mock_get_file = mocker.patch("bats_review.get_file")
    # one passing, one failing testcase (with classname)
    mock_get_file.return_value = """
    <testsuite>
      <testcase classname="suite1" name="ok"/>
      <testcase classname="suite1" name="failing_test">
        <failure>some failure</failure>
      </testcase>
    </testsuite>
    """
    result = bats_review.grep_failures("http://example.com/test.xml")
    assert result == {"suite1:failing_test"}


def test_grep_failures_malformed(mocker: pytest.MockerFixture) -> None:
    mock_get_file = mocker.patch("bats_review.get_file")
    mock_log = mocker.patch("bats_review.log")
    mock_get_file.return_value = "<this is not xml"
    # script currently exits with code 1 on parse errors
    with pytest.raises(SystemExit) as exc:
        bats_review.grep_failures("http://example.com/test.xml")
    assert exc.value.code == 1
    mock_log.exception.assert_called_once()


def test_process_logs_single_file(mocker: pytest.MockerFixture) -> None:
    mock_grep = mocker.patch("bats_review.grep_failures")
    mock_grep.return_value = {"a", "b"}
    res = bats_review.process_logs(["http://example.com/a.xml"])
    assert res == {"a", "b"}
    mock_grep.assert_called_once_with("http://example.com/a.xml")


def test_process_logs_multiple_files(mocker: pytest.MockerFixture) -> None:
    mock_executor_class = mocker.patch("bats_review.ThreadPoolExecutor")
    # build fake executor that returns map -> iterator of sets
    fake_executor = Mock()
    fake_executor.map.return_value = iter([{"f1"}, {"f2"}])
    mock_executor_class.return_value.__enter__.return_value = fake_executor

    files = ["one.xml", "two.xml"]
    res = bats_review.process_logs(files)
    assert res == {"f1", "f2"}
    mock_executor_class.assert_called_once_with(max_workers=2)
    fake_executor.map.assert_called_once()


def test_resolve_clone_chain_single(mocker: pytest.MockerFixture) -> None:
    with contextlib.suppress(Exception):
        bats_review.get_job.cache_clear()
    mock_get_job = mocker.patch("bats_review.get_job")
    mock_get_job.return_value = {"id": 123}
    chain = bats_review.resolve_clone_chain("http://openqa", 123)
    assert chain == [123]
    mock_get_job.assert_called_once_with("http://openqa/api/v1/jobs/123/details")


def test_resolve_clone_chain_multiple(mocker: pytest.MockerFixture) -> None:
    with contextlib.suppress(Exception):
        bats_review.get_job.cache_clear()
    mock_get_job = mocker.patch("bats_review.get_job")

    def side(url: str) -> dict[str, Any] | None:
        jid = int(url.split("/")[-2])
        if jid == 123:
            return {"id": 123, "origin_id": 122}
        if jid == 122:
            return {"id": 122, "origin_id": 121}
        if jid == 121:
            return {"id": 121}
        return None

    mock_get_job.side_effect = side
    chain = bats_review.resolve_clone_chain("http://openqa", 123)
    assert chain == [123, 122, 121]


# main


def test_main_no_clones(mocker: pytest.MockerFixture) -> None:
    with contextlib.suppress(Exception):
        bats_review.get_job.cache_clear()
    mock_resolve = mocker.patch("bats_review.resolve_clone_chain")
    mock_log = mocker.patch("bats_review.log")
    mock_resolve.return_value = [123]  # single element -> "No clones"
    with pytest.raises(SystemExit) as exc:
        bats_review.main("http://openqa.example.com/tests/123", dry_run=True)
    assert exc.value.code == 0
    mock_log.info.assert_called_with("No clones. Exiting")


def test_main_no_common_failures(mocker: pytest.MockerFixture) -> None:
    """Two jobs in chain; each produces different failures -> no common failures.

    main should log Tagging as PASSED.
    """
    with contextlib.suppress(Exception):
        bats_review.get_job.cache_clear()
    mock_resolve = mocker.patch("bats_review.resolve_clone_chain")
    mock_get_job = mocker.patch("bats_review.get_job")
    mock_process_logs = mocker.patch("bats_review.process_logs")
    mock_log = mocker.patch("bats_review.log")
    mock_resolve.return_value = [123, 122]

    def job_resp(url: str) -> dict[str, Any]:
        jid = int(url.split("/")[-2])
        return {
            "id": jid,
            "settings": {"TEST": "aardvark_testsuite", "DISTRI": "opensuse"},
            "ulogs": ["test.xml"],
        }

    mock_get_job.side_effect = job_resp
    # different failure sets for each job -> empty intersection
    mock_process_logs.side_effect = [{"a"}, {"b"}]
    res = bats_review.main("http://openqa.example.com/tests/123", dry_run=True)
    assert res is None
    mock_log.info.assert_called_with("No common failures across clone chain. Tagging as PASSED.")


def test_main_insufficient_logs(mocker: pytest.MockerFixture) -> None:
    """If jobs do not have the expected number of logs (e.g. podman expects 4 but provides 2).

    main should log the 'only X logs' messages for each job and eventually exit(0).
    """
    with contextlib.suppress(Exception):
        bats_review.get_job.cache_clear()
    mock_resolve = mocker.patch("bats_review.resolve_clone_chain")
    mock_get_job = mocker.patch("bats_review.get_job")
    mock_log = mocker.patch("bats_review.log")
    mock_resolve.return_value = [123, 122]

    def job_resp(url: str) -> dict[str, Any]:
        jid = int(url.split("/")[-2])
        return {
            "id": jid,
            "settings": {"TEST": "podman_testsuite", "DISTRI": "opensuse"},
            "ulogs": ["a.xml", "b.xml"],  # only 2, but podman expects 4
        }

    mock_get_job.side_effect = job_resp
    with pytest.raises(SystemExit) as exc:
        bats_review.main("http://openqa.example.com/tests/123", dry_run=True)
    assert exc.value.code == 0
    mock_log.info.assert_any_call("No logs found in chain. Exiting")


def test_parse_args_success(mocker: pytest.MockerFixture) -> None:
    mocker.patch("sys.argv", ["script.py", "http://example.com/tests/123"])
    args = bats_review.parse_args()
    assert args.url == "http://example.com/tests/123"


def test_parse_args_missing_url(mocker: pytest.MockerFixture) -> None:
    mocker.patch("sys.argv", ["script.py"])
    with pytest.raises(SystemExit):
        bats_review.parse_args()


# Integration test


def test_full_workflow_no_common_failures(mocker: pytest.MockerFixture) -> None:
    """Patch session.get to simulate two jobs each with a different failing testcase.

    Asserts that the script decides to tag as PASSED (dry_run) when there are no common failures.
    """
    mock_log = mocker.patch("bats_review.log")

    def fake_get(url: str, **_kwargs: Any) -> Mock:
        m = Mock()
        if "/api/v1/jobs/123/details" in url:
            m.json.return_value = {
                "job": {
                    "id": 123,
                    "settings": {
                        "TEST": "aardvark_testsuite",
                        "DISTRI": "opensuse",
                    },
                    "origin_id": 122,
                    "ulogs": ["test.xml"],
                }
            }
        elif "/api/v1/jobs/122/details" in url:
            m.json.return_value = {
                "job": {
                    "id": 122,
                    "settings": {
                        "TEST": "aardvark_testsuite",
                        "DISTRI": "opensuse",
                    },
                    "ulogs": ["test.xml"],
                }
            }
        elif "/tests/123/file/test.xml" in url:
            m.text = """
                <testsuite>
                  <testcase classname="c" name="ok"/>
                  <testcase classname="c" name="failA"><failure>err</failure></testcase>
                </testsuite>
            """
        elif "/tests/122/file/test.xml" in url:
            m.text = """
                <testsuite>
                  <testcase classname="c" name="ok"/>
                  <testcase classname="c" name="failB"><failure>err</failure></testcase>
                </testsuite>
            """
        else:
            # default (should not happen in this test)
            m.json.return_value = {"job": {"id": 999, "ulogs": []}}
        return m

    # patch the session used by the module
    bats_review.session.get = fake_get
    # run main and assert successful path (no SystemExit)
    res = bats_review.main("http://openqa.example.com/tests/123", dry_run=True)
    assert res is None
    mock_log.info.assert_called_with("No common failures across clone chain. Tagging as PASSED.")
