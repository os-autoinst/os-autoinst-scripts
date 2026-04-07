# SPDX-License-Identifier: MIT
# Copyright SUSE LLC
"""Unit tests for openqa_scan."""

from __future__ import annotations

import argparse
import importlib
import pathlib
import random
import sys
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

# Load the script as module "openqa_scan" (the file is named `openqa-scan`)
rootpath = pathlib.Path(__file__).parent.parent.resolve()
loader = importlib.machinery.SourceFileLoader("openqa_scan", f"{rootpath}/openqa-scan")
spec = importlib.util.spec_from_loader(loader.name, loader)
oqs = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = oqs
loader.exec_module(oqs)

# Replace the module-level session with a mock so no real HTTP calls are made
mock_session = MagicMock()
oqs.session = mock_session


# ===========================================================================
# Helpers / fixtures
# ===========================================================================


def make_job_info(**overrides: object) -> dict[str, object]:
    """Return a minimal job-info dict (mirrors the openQA API shape)."""
    info: dict[str, object] = {
        "id": 1,
        "name": "kde-23.08-desktop@x86_64",
        "result": "failed",
        "state": "done",
        "priority": 50,
        "settings": {
            "ARCH": "x86_64",
            "BUILD": "20240101",
            "DISTRI": "opensuse",
            "VERSION": "Tumbleweed",
            "FLAVOR": "kde",
            "TEST": "desktop",
        },
        "logs": ["serial0.txt"],
        "ulogs": [],
    }
    info.update(overrides)
    return info


def make_job(**info_overrides: object) -> oqs.Job:
    return oqs.Job(url="https://openqa.opensuse.org/tests/1", info=make_job_info(**info_overrides))


# ===========================================================================
# parse_build
# ===========================================================================


class TestParseBuild:
    def test_plain_digits_returned_unchanged(self) -> None:
        assert oqs.parse_build("20240115") == "20240115"

    def test_build_prefix_stripped(self) -> None:
        assert oqs.parse_build("Build20240115") == "20240115"

    def test_today(self) -> None:
        expected = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert oqs.parse_build("today") == expected

    def test_yesterday(self) -> None:
        expected = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        assert oqs.parse_build("yesterday") == expected

    def test_relative_minus_n(self) -> None:
        expected = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
        assert oqs.parse_build("-3") == expected

    def test_arbitrary_string_passthrough(self) -> None:
        assert oqs.parse_build(":git:abc:python3") == ":git:abc:python3"

    def test_relative_minus_zero(self) -> None:
        expected = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert oqs.parse_build("-0") == expected


# ===========================================================================
# parse_url
# ===========================================================================


class TestParseUrl:
    def test_prepends_https_when_no_scheme(self) -> None:
        assert oqs.parse_url("openqa.opensuse.org/tests/1") == "https://openqa.opensuse.org/tests/1"

    def test_leaves_https_intact(self) -> None:
        url = "https://openqa.opensuse.org/tests/1"
        assert oqs.parse_url(url) == url

    def test_leaves_http_intact(self) -> None:
        url = "http://openqa.suse.de/tests/42"
        assert oqs.parse_url(url) == url


# ===========================================================================
# ignore_trace
# ===========================================================================


class TestIgnoreTrace:
    def test_btrfs_trace_ignored_on_xfstests_job(self) -> None:
        job = make_job(name="xfstests_btrfs@x86_64")
        assert oqs.ignore_trace(job, "btrfs: open_ctree failed") is True

    def test_btrfs_trace_not_ignored_on_non_xfstests_job(self) -> None:
        job = make_job(name="btrfs-smoke@x86_64")
        assert oqs.ignore_trace(job, "btrfs: open_ctree failed") is False

    def test_xfs_trace_ignored_on_xfstests_job(self) -> None:
        job = make_job(name="xfstests_xfs@x86_64")
        assert oqs.ignore_trace(job, "xfs_buf_find: block not found") is True

    def test_oom_trace_ignored_regardless_of_job(self) -> None:
        job = make_job(name="containers-basic@x86_64")
        assert oqs.ignore_trace(job, "out_of_memory: Kill process") is True

    def test_unrelated_trace_not_ignored(self) -> None:
        job = make_job(name="kde-desktop@x86_64")
        assert oqs.ignore_trace(job, "kernel BUG at mm/slub.c:4213") is False


# ===========================================================================
# get_traces
# ===========================================================================

CALL_TRACE_SAMPLE = """\
[   12.345678] WARNING: CPU: 0 PID: 1234 at mm/slub.c:4213
[   12.345679] -[ cut here ]-
[   12.345680] Kernel BUG at mm/slub.c:4213!
[   12.345681] Call Trace:
[   12.345682]  <TASK>
[   12.345683]  dump_stack+0x57/0x80
[   12.345684]  __warn+0xac/0xf0
[   12.345685] -[ end trace abcd1234 ]-
"""

SYSRQ_SAMPLE = """\
[  100.0] sysrq: Show backtrace of all active CPUs
[  100.1] Call Trace:
[  100.2]  dump_stack+0x57/0x80
[  100.3] -[ end trace eeee1234 ]-
"""

MULTI_TRACE_SAMPLE = """\
[  1.0] -[ cut here ]-
[  1.1] Call Trace:
[  1.2]  first_func+0x10/0x20
[  1.3] -[ end trace 0001 ]-
[  2.0] -[ cut here ]-
[  2.1] Call Trace:
[  2.2]  second_func+0x30/0x40
[  2.3] -[ end trace 0002 ]-
"""


class TestGetTraces:
    def _job_with_serial(self, text: str) -> oqs.Job:
        job = make_job()
        return oqs.Job(
            url=job.url,
            info=job.info,
            logs={"serial0.txt": {"text": text, "url": "https://example.com/serial0.txt"}},
        )

    def test_no_logs_returns_empty(self) -> None:
        job = make_job()
        assert oqs.get_traces(job) == []

    def test_single_call_trace_parsed(self) -> None:
        job = self._job_with_serial(CALL_TRACE_SAMPLE)
        traces = oqs.get_traces(job)
        assert len(traces) == 1
        assert "Call Trace" in traces[0]

    def test_sysrq_trace_ignored(self) -> None:
        job = self._job_with_serial(SYSRQ_SAMPLE)
        assert oqs.get_traces(job) == []

    def test_multiple_traces_returned(self) -> None:
        job = self._job_with_serial(MULTI_TRACE_SAMPLE)
        traces = oqs.get_traces(job)
        assert len(traces) == 2

    def test_oom_trace_filtered_via_ignore_traces(self) -> None:
        text = """\
[  1.0] -[ cut here ]-
[  1.1] Call Trace:
[  1.2]  out_of_memory+0x10/0x20
[  1.3] -[ end trace 0001 ]-
"""
        job = self._job_with_serial(text)
        # out_of_memory rule has no "job" key, so it matches any job name
        assert oqs.get_traces(job) == []

    def test_empty_log_returns_empty(self) -> None:
        job = self._job_with_serial("")
        assert oqs.get_traces(job) == []


# ===========================================================================
# get_file
# ===========================================================================


class TestGetFile:
    def test_returns_text_on_success(self) -> None:
        resp = MagicMock()
        resp.text = "hello"
        mock_session.get.return_value = resp
        result = oqs.get_file("https://example.com/file.txt")
        assert result == "hello"

    def test_returns_none_on_http_error(self) -> None:
        mock_session.get.side_effect = requests.exceptions.HTTPError("404")
        result = oqs.get_file("https://example.com/missing.txt")
        assert result is None
        mock_session.get.side_effect = None  # reset

    def test_returns_none_on_connection_error(self) -> None:
        mock_session.get.side_effect = requests.exceptions.ConnectionError()
        result = oqs.get_file("https://example.com/file.txt")
        assert result is None
        mock_session.get.side_effect = None


# ===========================================================================
# get_json — pagination
# ===========================================================================


class TestGetJson:
    def _make_response(self, data: object, headers: dict[str, str] | None = None) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = data
        resp.headers = headers or {}
        return resp

    def test_single_page_list(self) -> None:
        mock_session.request.return_value = self._make_response([{"id": 1}])
        result = oqs.get_json("https://example.com/api/jobs")
        assert result == [{"id": 1}]

    def test_follows_next_link_header(self) -> None:
        page1 = self._make_response(
            [{"id": 1}],
            headers={"Link": '<https://example.com/api/jobs?page=2>; rel="next"'},
        )
        page2 = self._make_response([{"id": 2}])
        mock_session.request.side_effect = [page1, page2]

        result = oqs.get_json("https://example.com/api/jobs")
        assert result == [{"id": 1}, {"id": 2}]
        mock_session.request.side_effect = None

    def test_key_parameter_unwraps_dict(self) -> None:
        mock_session.request.return_value = self._make_response({"job": {"id": 99}})
        result = oqs.get_json("https://example.com/api/jobs/99", key="job")
        assert result == {"id": 99}

    def test_returns_none_on_request_exception(self) -> None:
        mock_session.request.side_effect = requests.exceptions.Timeout()
        result = oqs.get_json("https://example.com/api/jobs")
        assert result is None
        mock_session.request.side_effect = None

    def test_non_get_method_returns_single_page(self) -> None:
        mock_session.request.reset_mock()
        mock_session.request.return_value = self._make_response({"result": None})
        result = oqs.get_json("https://example.com/api/jobs/1/cancel", method="POST")
        assert result == {"result": None}
        # Should NOT follow any Link header — exactly one HTTP call
        assert mock_session.request.call_count == 1


# ===========================================================================
# get_jobs — filtering
# ===========================================================================


class TestGetJobs:
    def _patch_get_json(self, data: object) -> AbstractContextManager[MagicMock]:
        return patch.object(oqs, "get_json", return_value=data)

    def test_filters_ignored_job_names(self) -> None:
        raw = [
            make_job_info(id=1, name="kde-desktop@x86_64"),
            make_job_info(id=2, name="kde:investigate:desktop@x86_64"),
        ]
        with self._patch_get_json(raw):
            jobs = oqs.get_jobs("https://openqa.opensuse.org/tests/overview?build=20240101")
        assert len(jobs) == 1
        assert jobs[0].info["id"] == 1

    def test_single_job_url_detected(self) -> None:
        with patch.object(oqs, "get_json", return_value=make_job_info(id=42)):
            jobs = oqs.get_jobs("https://openqa.opensuse.org/tests/42")
        # get_json is called with a jobs/42 api url; result is normalised
        assert len(jobs) == 1

    def test_returns_empty_list_when_get_json_fails(self) -> None:
        with self._patch_get_json(None):
            jobs = oqs.get_jobs("https://openqa.opensuse.org/tests/overview")
        assert jobs == []


# ===========================================================================
# get_latest_build
# ===========================================================================


class TestGetLatestBuild:
    def test_o3_picks_first_digit_build(self) -> None:
        builds = [
            {"build": "20240201", "distris": {"opensuse": 1}, "version": "Tumbleweed"},
            {"build": "20240130", "distris": {"opensuse": 1}, "version": "Tumbleweed"},
        ]
        with patch.object(oqs, "get_json", return_value=builds):
            result = oqs.get_latest_build("https://openqa.opensuse.org/group_overview/1")
        assert "20240201" in result["build"]

    def test_o3_skips_non_digit_builds(self) -> None:
        builds = [
            {"build": ":git:abc", "distris": {"opensuse": 1}, "version": "Tumbleweed"},
            {"build": "20240201", "distris": {"opensuse": 1}, "version": "Tumbleweed"},
        ]
        with patch.object(oqs, "get_json", return_value=builds):
            result = oqs.get_latest_build("https://openqa.opensuse.org/group_overview/1")
        assert "20240201" in result["build"]

    def test_returns_none_when_api_fails(self) -> None:
        with patch.object(oqs, "get_json", return_value=None):
            assert oqs.get_latest_build("https://openqa.opensuse.org/group_overview/1") is None

    def test_osd_uses_version_count_slice(self) -> None:
        builds = [
            {
                "build": "20240201-1",
                "distris": {"sle": 1},
                "version": "15-SP5",
                "version_count": 2,
            },
            {
                "build": "20240130-1",
                "distris": {"sle": 1},
                "version": "15-SP5",
                "version_count": 2,
            },
            {
                "build": "20240101-1",
                "distris": {"sle": 1},
                "version": "15-SP5",
                "version_count": 2,
            },
        ]
        with patch.object(oqs, "get_json", return_value=builds):
            result = oqs.get_latest_build("https://openqa.suse.de/group_overview/5")
        assert len(result["build"]) == 2  # only first version_count=2 builds


# ===========================================================================
# get_urls — URL normalisation
# ===========================================================================


class TestGetUrls:
    def _args(self, url: str, **kwargs: object) -> argparse.Namespace:
        base: dict[str, object] = {
            "build": None,
            "arch": None,
            "distri": None,
            "flavor": None,
            "groupid": None,
            "result": None,
            "state": None,
            "version": None,
        }
        base.update(kwargs)
        base["url"] = [url]
        return argparse.Namespace(**base)

    def test_overview_path_preserved(self) -> None:
        args = self._args("https://openqa.opensuse.org/tests/overview?build=20240101")
        urls = oqs.get_urls(args)
        assert len(urls) == 1
        assert "/tests/overview" in urls[0]

    def test_group_overview_gets_groupid_and_latest(self) -> None:
        args = self._args("https://openqa.opensuse.org/group_overview/3")
        urls = oqs.get_urls(args)
        assert "groupid=3" in urls[0]
        assert "latest=1" in urls[0]

    def test_direct_test_url_returned_verbatim(self) -> None:
        args = self._args("https://openqa.opensuse.org/tests/99")
        urls = oqs.get_urls(args)
        assert urls == ["https://openqa.opensuse.org/tests/99"]

    def test_short_test_url_expanded(self) -> None:
        args = self._args("https://openqa.opensuse.org/t99")
        urls = oqs.get_urls(args)
        assert urls == ["https://openqa.opensuse.org/tests/99"]

    def test_build_appended_to_query(self) -> None:
        args = self._args(
            "https://openqa.opensuse.org/tests/overview",
            build=["20240101"],
        )
        urls = oqs.get_urls(args)
        assert "build=20240101" in urls[0]

    def test_osd_build_gets_dash_one_suffix(self) -> None:
        """Plain YYYYMMDD builds on osd should become YYYYMMDD-1."""
        args = self._args(
            "https://openqa.suse.de/tests/overview",
            build=["20240101"],
        )
        urls = oqs.get_urls(args)
        assert "build=20240101-1" in urls[0]

    def test_osd_non_digit_build_unchanged(self) -> None:
        from urllib.parse import unquote

        args = self._args(
            "https://openqa.suse.de/tests/overview",
            build=[":git:abc:python3"],
        )
        urls = oqs.get_urls(args)
        # urlencode percent-encodes colons; decode before asserting
        assert ":git:abc:python3" in unquote(urls[0])
        # Crucially, no "-1" suffix should have been appended
        assert ":git:abc:python3-1" not in unquote(urls[0])

    def test_latest_build_triggers_api_call(self) -> None:
        with patch.object(
            oqs,
            "get_latest_build",
            return_value={
                "build": ["20240201"],
                "distri": ["opensuse"],
                "version": ["Tumbleweed"],
            },
        ) as mock_lb:
            args = self._args(
                "https://openqa.opensuse.org/tests/overview",
                build=["latest"],
            )
            urls = oqs.get_urls(args)
        mock_lb.assert_called_once()
        assert "build=20240201" in urls[0]


# ===========================================================================
# parse_args — argument validation
# ===========================================================================


class TestParseArgs:
    def _parse(self, argv: list[str]) -> argparse.Namespace:
        with patch("sys.argv", ["openqa_scan.py", *argv]):
            return oqs.parse_args()

    def test_extract_all_expands_to_all_extracts(self) -> None:
        args = self._parse(["-x", "all", "https://openqa.opensuse.org/tests/1"])
        assert "traces" in args.extract
        assert "coredumps" in args.extract
        assert "all" not in args.extract

    def test_priority_without_action_implies_prio(self) -> None:
        args = self._parse(["-p", "30", "https://openqa.opensuse.org/tests/1"])
        assert args.action == "prio"
        assert args.priority == 30

    def test_comment_without_action_implies_comments(self) -> None:
        args = self._parse(["-c", "hello world", "https://openqa.opensuse.org/tests/1"])
        assert args.action == "comments"

    def test_invalid_result_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["-r", "not_a_result", "https://openqa.opensuse.org/tests/1"])

    def test_invalid_action_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["-A", "nuke", "https://openqa.opensuse.org/tests/1"])

    def test_priority_with_cancel_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["-A", "cancel", "-p", "30", "https://openqa.opensuse.org/tests/1"])

    def test_comment_with_cancel_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["-A", "cancel", "-c", "oops", "https://openqa.opensuse.org/tests/1"])

    def test_multiple_urls_accepted(self) -> None:
        args = self._parse([
            "https://openqa.opensuse.org/tests/1",
            "https://openqa.opensuse.org/tests/2",
        ])
        assert len(args.url) == 2

    def test_build_today_resolved(self) -> None:
        args = self._parse(["-b", "today", "https://openqa.opensuse.org/tests/1"])
        expected = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert args.build == [expected]


# ===========================================================================
# post_route — action dispatching
# ===========================================================================


class TestPostRoute:
    def _job(self) -> oqs.Job:
        return oqs.Job(
            url="https://openqa.opensuse.org/tests/7",
            info=make_job_info(id=7),
        )

    def _setup_get_json(self, data: object) -> AbstractContextManager[MagicMock]:
        return patch.object(oqs, "get_json", return_value=data)

    def test_cancel_prints_cancelled(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._setup_get_json({"result": None}):
            oqs.post_route(self._job(), "cancel")
        assert "cancelled" in capsys.readouterr().out

    def test_restart_prints_restarted_with_new_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._setup_get_json({"result": [{"7": 8}]}):
            oqs.post_route(self._job(), "restart")
        out = capsys.readouterr().out
        assert "restarted" in out
        assert "8" in out

    def test_delete_prints_deleted(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._setup_get_json({"result": True}):
            oqs.post_route(self._job(), "delete")
        assert "deleted" in capsys.readouterr().out

    def test_failed_action_prints_failed(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._setup_get_json({}):
            oqs.post_route(self._job(), "cancel")
        assert "failed" in capsys.readouterr().out

    def test_comments_prints_ok(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._setup_get_json({"id": 42}):
            oqs.post_route(self._job(), "comments", data={"text": "hello"})
        assert "ok" in capsys.readouterr().out


# ===========================================================================
# print_job — output formatting
# ===========================================================================


class TestPrintJob:
    def _serial_job(self, serial_text: str) -> oqs.Job:
        return oqs.Job(
            url="https://openqa.opensuse.org/tests/1",
            info=make_job_info(),
            logs={
                "serial0.txt": {
                    "text": serial_text,
                    "url": "https://example.com/serial0.txt",
                }
            },
        )

    def test_status_parallel_failed_displayed_as_failed(self, capsys: pytest.CaptureFixture[str]) -> None:
        job = make_job(result="parallel_failed")
        oqs.print_job(job)
        out = capsys.readouterr().out
        assert "failed" in out
        assert "parallel" not in out

    def test_status_timeout_exceeded_displayed_as_exceeded(self, capsys: pytest.CaptureFixture[str]) -> None:
        job = make_job(result="timeout_exceeded")
        oqs.print_job(job)
        assert "exceeded" in capsys.readouterr().out

    def test_state_shown_when_result_is_none(self, capsys: pytest.CaptureFixture[str]) -> None:
        job = make_job(result="none", state="running")
        oqs.print_job(job)
        assert "running" in capsys.readouterr().out

    def test_no_output_when_extract_set_and_no_traces(self, capsys: pytest.CaptureFixture[str]) -> None:
        job = make_job()
        oqs.print_job(job, extract={"traces"})
        assert capsys.readouterr().out == ""

    def test_traces_section_printed_when_present(self, capsys: pytest.CaptureFixture[str]) -> None:
        job = self._serial_job(CALL_TRACE_SAMPLE)
        oqs.print_job(job, extract={"traces"})
        assert "traces" in capsys.readouterr().out

    def test_coredump_url_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        job = oqs.Job(
            url="https://openqa.opensuse.org/tests/1",
            info=make_job_info(ulogs=["core.12345", "core.12345.txt"]),
        )
        oqs.print_job(job, extract={"coredumps"})
        assert "core" in capsys.readouterr().out


# ===========================================================================
# get_all_jobs — batch fetching
# ===========================================================================


class TestGetAllJobs:
    def test_returns_empty_for_no_groups(self) -> None:
        assert oqs.get_all_jobs([]) == []

    def test_lightweight_path_batches_ids(self) -> None:
        """When no logs/comments requested, IDs are fetched in bulk."""
        jobs = [make_job(id=i) for i in range(1, 5)]
        with patch.object(oqs, "get_jobs", return_value=jobs) as mock_gj:
            result = oqs.get_all_jobs(["https://openqa.opensuse.org/tests/overview"])
        # Called once for the overview, then once (or more) with ids
        assert mock_gj.call_count >= 1
        assert len(result) > 0

    def test_expensive_path_used_when_logs_requested(self) -> None:
        """When logs are requested, each job is fetched individually."""
        overview_jobs = [make_job(id=i) for i in range(1, 3)]
        with (
            patch.object(oqs, "get_jobs", return_value=overview_jobs),
            patch.object(oqs, "get_job", return_value=overview_jobs[0]) as mock_gj,
        ):
            oqs.get_all_jobs(
                ["https://openqa.opensuse.org/tests/overview"],
                logs=["serial0.txt"],
            )
        mock_gj.assert_called()


# ===========================================================================
# main — orchestration
# ===========================================================================


class TestMain:
    """main() wires together get_urls → get_all_jobs → sort → act/print.

    We patch get_all_jobs and post_route/print_job to stay focused on
    the orchestration logic rather than re-testing subordinate functions.
    """

    def _args(self, **kwargs: object) -> argparse.Namespace:
        defaults: dict[str, object] = {
            "url": ["https://openqa.opensuse.org/tests/overview"],
            "action": None,
            "extract": set(),
            "verbose": False,
            "priority": None,
            "comment": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def _make_jobs(
        self,
        n: int = 3,
        result: str = "failed",
        state: str = "done",
        priority: int = 50,
    ) -> list[Any]:
        return [
            oqs.Job(
                url=f"https://openqa.opensuse.org/tests/{i}",
                info=make_job_info(
                    id=i,
                    name=f"test-job-{i}@x86_64",
                    result=result,
                    state=state,
                    priority=priority,
                    settings={
                        "ARCH": "x86_64",
                        "BUILD": f"2024010{i}",
                        "DISTRI": "opensuse",
                        "VERSION": "Tumbleweed",
                        "FLAVOR": "kde",
                        "TEST": f"job{i}",
                    },
                ),
            )
            for i in range(1, n + 1)
        ]

    # --- print path ---

    def test_print_path_calls_print_job_for_each_job(self, capsys: pytest.CaptureFixture[str]) -> None:
        jobs = self._make_jobs(3)
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(
                oqs,
                "get_urls",
                return_value=["https://openqa.opensuse.org/tests/overview"],
            ),
        ):
            oqs.main(self._args())
        out = capsys.readouterr().out
        # Each job's name should appear in the output
        for job in jobs:
            assert job.info["name"] in out

    def test_jobs_sorted_by_build_distri_version_flavor_arch_test(self) -> None:
        jobs = self._make_jobs(3)
        # Shuffle so order is not already correct
        shuffled = jobs[:]
        random.shuffle(shuffled)
        captured_order: list[int] = []
        with (
            patch.object(oqs, "get_all_jobs", return_value=shuffled),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(
                oqs,
                "print_job",
                side_effect=lambda j, **_: captured_order.append(j.info["id"]),
            ),
        ):
            oqs.main(self._args())
        assert captured_order == [1, 2, 3]

    def test_traces_extract_passes_serial_log_to_get_all_jobs(self) -> None:
        with (
            patch.object(oqs, "get_all_jobs", return_value=[]) as mock_gaj,
            patch.object(oqs, "get_urls", return_value=[""]),
        ):
            oqs.main(self._args(extract={"traces"}))
        _, kwargs = mock_gaj.call_args
        assert kwargs.get("logs") == ["serial0.txt"]

    def test_verbose_passes_comments_flag(self) -> None:
        with (
            patch.object(oqs, "get_all_jobs", return_value=[]) as mock_gaj,
            patch.object(oqs, "get_urls", return_value=[""]),
        ):
            oqs.main(self._args(verbose=True))
        _, kwargs = mock_gaj.call_args
        assert kwargs.get("comments") is True

    def test_no_logs_requested_when_extract_is_empty(self) -> None:
        with (
            patch.object(oqs, "get_all_jobs", return_value=[]) as mock_gaj,
            patch.object(oqs, "get_urls", return_value=[""]),
        ):
            oqs.main(self._args())
        _, kwargs = mock_gaj.call_args
        assert kwargs.get("logs") is None

    # --- action: cancel ---

    def test_cancel_skips_already_cancelled_jobs(self) -> None:
        jobs = [
            *self._make_jobs(2, state="done"),
            oqs.Job(
                url="https://openqa.opensuse.org/tests/99",
                info=make_job_info(id=99, state="cancelled", result="user_cancelled"),
            ),
        ]
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="cancel"))
        routed_ids = {c.args[0].info["id"] for c in mock_pr.call_args_list}
        assert 99 not in routed_ids
        assert len(routed_ids) == 2

    def test_cancel_with_no_cancellable_jobs_returns_early(self) -> None:
        jobs = self._make_jobs(2, state="cancelled")
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="cancel"))
        mock_pr.assert_not_called()

    # --- action: prio ---

    def test_prio_only_updates_scheduled_or_assigned_jobs(self) -> None:
        jobs = [
            *self._make_jobs(2, state="scheduled", priority=50),
            oqs.Job(
                url="https://openqa.opensuse.org/tests/99",
                info=make_job_info(id=99, state="done", priority=50),
            ),
        ]
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="prio", priority=30))
        routed_ids = {c.args[0].info["id"] for c in mock_pr.call_args_list}
        assert 99 not in routed_ids

    def test_prio_skips_jobs_already_at_target_priority(self) -> None:
        jobs = self._make_jobs(3, state="scheduled", priority=30)
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="prio", priority=30))
        mock_pr.assert_not_called()

    def test_prio_passes_priority_in_data(self) -> None:
        jobs = self._make_jobs(1, state="scheduled", priority=50)
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="prio", priority=20))
        assert mock_pr.call_args.kwargs["data"] == {"prio": 20}

    # --- action: restart ---

    def test_restart_adds_skip_parents_to_data(self) -> None:
        jobs = self._make_jobs(1)
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="restart"))
        data = mock_pr.call_args.kwargs["data"]
        assert data.get("skip_parents") == 1

    def test_restart_with_priority_merges_prio_and_skip_parents(self) -> None:
        jobs = self._make_jobs(1)
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="restart", priority=10))
        data = mock_pr.call_args.kwargs["data"]
        assert data == {"prio": 10, "skip_parents": 1}

    # --- action: comments ---

    def test_comments_passes_text_in_data(self) -> None:
        jobs = self._make_jobs(1)
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="comments", comment="bsc#12345"))
        assert mock_pr.call_args.kwargs["data"] == {"text": "bsc#12345"}

    # --- action: delete safety limit ---

    def test_delete_refuses_more_than_99_jobs(self) -> None:
        jobs = self._make_jobs(100)
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
            patch("logging.error") as mock_log,
        ):
            oqs.main(self._args(action="delete"))
        mock_pr.assert_not_called()
        mock_log.assert_called()

    def test_delete_proceeds_with_99_or_fewer_jobs(self) -> None:
        jobs = self._make_jobs(99)
        with (
            patch.object(oqs, "get_all_jobs", return_value=jobs),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="delete"))
        assert mock_pr.call_count == 99

    # --- empty job list ---

    def test_action_with_empty_job_list_returns_early(self) -> None:
        with (
            patch.object(oqs, "get_all_jobs", return_value=[]),
            patch.object(oqs, "get_urls", return_value=[""]),
            patch.object(oqs, "post_route") as mock_pr,
        ):
            oqs.main(self._args(action="cancel"))
        mock_pr.assert_not_called()
