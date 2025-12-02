# Copyright SUSE LLC
"""Unit & integration tests for openqa-bats-review (JUnit XML based)."""

from __future__ import annotations
import importlib.machinery
import importlib.util
import sys
from unittest.mock import MagicMock, Mock, patch
import pathlib
from typing import Any

import pytest
from requests.exceptions import RequestException
import contextlib

# Load the script as module "bats_review" (the file is named `openqa-bats-review`)
rootpath = pathlib.Path(__file__).parent.parent.resolve()
loader = importlib.machinery.SourceFileLoader("bats_review", f"{rootpath}/openqa-bats-review")
spec = importlib.util.spec_from_loader(loader.name, loader)
bats_review = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = bats_review
loader.exec_module(bats_review)


#
# Unit tests
#


class TestGetFile:
    @patch("bats_review.session")
    def test_get_file_success(self, mock_session: MagicMock) -> None:
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

    @patch("bats_review.session")
    @patch("bats_review.log")
    def test_get_file_request_exception(self, mock_log: MagicMock, mock_session: MagicMock) -> None:
        mock_session.get.side_effect = RequestException("network")
        with pytest.raises(SystemExit) as exc:
            bats_review.get_file("http://example.com/foo.xml")
        assert exc.value.code == 1
        mock_log.error.assert_called_once()


class TestGetJob:
    def setup_method(self) -> None:
        # clear lru cache between tests
        with contextlib.suppress(Exception):
            bats_review.get_job.cache_clear()

    @patch("bats_review.session")
    def test_get_job_success(self, mock_session: MagicMock) -> None:
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

    @patch("bats_review.session")
    @patch("bats_review.log")
    def test_get_job_request_exception(self, mock_log: MagicMock, mock_session: MagicMock) -> None:
        mock_session.get.side_effect = RequestException("boom")
        with pytest.raises(SystemExit) as exc:
            bats_review.get_job("http://host/api/v1/jobs/123")
        assert exc.value.code == 1
        mock_log.error.assert_called_once()


class TestGrepFailures:
    @patch("bats_review.get_file")
    def test_grep_failures_success(self, mock_get_file: MagicMock) -> None:
        # one passing, one failing testcase (with classname)
        xml = """
        <testsuite>
          <testcase classname="suite1" name="ok"/>
          <testcase classname="suite1" name="failing_test">
            <failure>some failure</failure>
          </testcase>
        </testsuite>
        """
        mock_get_file.return_value = xml
        result = bats_review.grep_failures("http://example.com/test.xml")
        assert result == {"suite1:failing_test"}

    @patch("bats_review.get_file")
    @patch("bats_review.log")
    def test_grep_failures_malformed(self, mock_log: MagicMock, mock_get_file: MagicMock) -> None:
        mock_get_file.return_value = "<this is not xml"
        # script currently exits with code 1 on parse errors
        with pytest.raises(SystemExit) as exc:
            bats_review.grep_failures("http://example.com/test.xml")
        assert exc.value.code == 1
        mock_log.error.assert_called_once()


class TestProcessLogs:
    @patch("bats_review.grep_failures")
    def test_process_logs_single_file(self, mock_grep: MagicMock) -> None:
        mock_grep.return_value = {"a", "b"}
        res = bats_review.process_logs(["http://example.com/a.xml"])
        assert res == {"a", "b"}
        mock_grep.assert_called_once_with("http://example.com/a.xml")

    @patch("bats_review.ThreadPoolExecutor")
    def test_process_logs_multiple_files(self, mock_executor_class: MagicMock) -> None:
        # build fake executor that returns map -> iterator of sets
        fake_executor = Mock()
        fake_executor.map.return_value = iter([{"f1"}, {"f2"}])
        mock_executor_class.return_value.__enter__.return_value = fake_executor

        files = ["one.xml", "two.xml"]
        res = bats_review.process_logs(files)
        assert res == {"f1", "f2"}
        mock_executor_class.assert_called_once_with(max_workers=2)
        fake_executor.map.assert_called_once()


class TestResolveCloneChain:
    def setup_method(self) -> None:
        with contextlib.suppress(Exception):
            bats_review.get_job.cache_clear()

    @patch("bats_review.get_job")
    def test_resolve_clone_chain_single(self, mock_get_job: MagicMock) -> None:
        mock_get_job.return_value = {"id": 123}
        chain = bats_review.resolve_clone_chain("http://openqa", 123)
        assert chain == [123]
        mock_get_job.assert_called_once_with("http://openqa/api/v1/jobs/123/details")

    @patch("bats_review.get_job")
    def test_resolve_clone_chain_multiple(self, mock_get_job: MagicMock) -> None:
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


class TestMain:
    def setup_method(self) -> None:
        with contextlib.suppress(Exception):
            bats_review.get_job.cache_clear()

    @patch("bats_review.resolve_clone_chain")
    @patch("bats_review.log")
    def test_main_no_clones(self, mock_log: MagicMock, mock_resolve: MagicMock) -> None:
        mock_resolve.return_value = [123]  # single element -> "No clones"
        with pytest.raises(SystemExit) as exc:
            bats_review.main("http://openqa.example.com/tests/123", dry_run=True)
        assert exc.value.code == 0
        mock_log.info.assert_called_with("No clones. Exiting")

    @patch("bats_review.resolve_clone_chain")
    @patch("bats_review.get_job")
    @patch("bats_review.process_logs")
    @patch("bats_review.openqa_comment")
    @patch("bats_review.log")
    def test_main_no_common_failures(
        self,
        mock_openqa_comment: MagicMock,
        mock_process_logs: MagicMock,
        mock_get_job: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """Two jobs in chain; each produces different failures -> no common failures.

        main should call openqa_comment(...) (we patch it) and log Tagging as PASSED.
        """
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
        mock_openqa_comment.return_value = "commented"

        # should return normally (no SystemExit) because script prints comment and returns
        res = bats_review.main("http://openqa.example.com/tests/123", dry_run=True)
        assert res is None

        # openqa_comment should be called for the job that we started from (my_job_id == 123)
        called = mock_openqa_comment.call_args[0]
        job_id, host, comment, dry_run = called[:4]
        assert job_id == 123
        assert host.startswith(("http://openqa.example.com", "https://openqa.example.com")), host
        assert bats_review.PASSED in comment
        assert dry_run is True

    @patch("bats_review.resolve_clone_chain")
    @patch("bats_review.get_job")
    @patch("bats_review.log")
    def test_main_insufficient_logs(
        self, mock_log: MagicMock, mock_get_job: MagicMock, mock_resolve: MagicMock
    ) -> None:
        """If jobs do not have the expected number of logs (e.g. podman expects 4 but provides 2),

        main should log the 'only X logs' messages for each job and eventually exit(0).
        """
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
        mock_log.info.assert_any_call("Job %s has only %d logs, skipping", 123, 2)
        mock_log.info.assert_any_call("Job %s has only %d logs, skipping", 122, 2)
        mock_log.info.assert_any_call("No logs found in chain. Exiting")


class TestParseArgs:
    @patch("sys.argv", ["script.py", "http://example.com/tests/123"])
    def test_parse_args_success(self) -> None:
        args = bats_review.parse_args()
        assert args.url == "http://example.com/tests/123"

    @patch("sys.argv", ["script.py"])
    def test_parse_args_missing_url(self) -> None:
        with pytest.raises(SystemExit):
            bats_review.parse_args()


#
# Integration test
#
class TestIntegration:
    @patch("bats_review.openqa_comment")
    def test_full_workflow_no_common_failures(self, mock_openqa_comment: MagicMock) -> None:
        """Integration-style test: patch session.get to return proper JSON for job details

        and JUnit XML for files, simulate two jobs that each have a different failing
        testcase, and assert that the script decides to tag as PASSED (dry_run).
        """

        def fake_get(url: str, headers: dict[str, Any] | None = None, timeout: int | None = None) -> Mock:
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
        mock_openqa_comment.return_value = "ok-comment"

        # run main and assert successful path (no SystemExit); openqa_comment should be called
        res = bats_review.main("http://openqa.example.com/tests/123", dry_run=True)
        assert res is None
        called = mock_openqa_comment.call_args[0]
        job_id, host, comment, dry_run = called[:4]
        assert job_id == 123
        assert host.startswith(("http://openqa.example.com", "https://openqa.example.com")), host
        assert bats_review.PASSED in comment
        assert dry_run is True
