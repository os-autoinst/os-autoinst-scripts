# Copyright SUSE LLC
# ruff: noqa: FBT003, E501, S404, PLC0415, ERA001, PLR0915
"""Unit tests for openqa-label-known-issues."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
import typer

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# Load the script dynamically as a module
rootpath = pathlib.Path(__file__).parent.parent.resolve()
path = rootpath / "openqa-label-known-issues"
spec = importlib.util.spec_from_file_location(
    "openqa_label_known_issues",
    path,
    loader=importlib.machinery.SourceFileLoader("openqa_label_known_issues", str(path)),
)
assert spec is not None
assert spec.loader is not None
openqa_label_known_issues = importlib.util.module_from_spec(spec)
sys.modules["openqa_label_known_issues"] = openqa_label_known_issues
spec.loader.exec_module(openqa_label_known_issues)


def test_setup_logging() -> None:
    openqa_label_known_issues.setup_logging(0)
    openqa_label_known_issues.setup_logging(1)
    openqa_label_known_issues.setup_logging(2)


def test_extract_timeago() -> None:
    html1 = 'Result:<abbr class="timeago" title="2026-06-03T12:00:00Z"></abbr>'
    assert openqa_label_known_issues.extract_timeago(html1) == "2026-06-03T12:00:00Z"

    html2 = 'Result:<abbr title="2026-06-04T12:00:00Z" class="timeago"></abbr>'
    assert openqa_label_known_issues.extract_timeago(html2) == "2026-06-04T12:00:00Z"

    html3 = 'Result:<abbr class="other" title="2026-06-04T12:00:00Z"></abbr>'
    assert openqa_label_known_issues.extract_timeago(html3) is None


def test_is_older_than_14_days() -> None:
    # Older than 14 days
    assert openqa_label_known_issues.is_older_than_14_days("2020-01-01T00:00:00Z") is True
    # Naive date older than 14 days (timezone None branch)
    assert openqa_label_known_issues.is_older_than_14_days("2020-01-01T00:00:00") is True
    # Not older
    from datetime import datetime, timezone

    now_str = datetime.now(timezone.utc).isoformat()
    assert openqa_label_known_issues.is_older_than_14_days(now_str) is False
    # Invalid date
    assert openqa_label_known_issues.is_older_than_14_days("invalid") is False


def test_get_markdown_html(mocker: MockerFixture) -> None:
    # 1. Successful Markdown.pl run
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = Mock(stdout="<p>html</p>\n")
    assert openqa_label_known_issues.get_markdown_html("test") == "<p>html</p>\n"
    mock_run.assert_called_once()

    # 2. Markdown.pl missing, but markdown succeeds (fallback loop)
    mock_run.reset_mock()
    mock_run.side_effect = [FileNotFoundError, Mock(stdout="<p>html2</p>\n")]
    assert openqa_label_known_issues.get_markdown_html("test") == "<p>html2</p>\n"
    assert mock_run.call_count == 2

    # 3. Markdown tools missing / error out entirely
    mock_run.reset_mock()
    mock_run.side_effect = FileNotFoundError
    assert openqa_label_known_issues.get_markdown_html("test") == "test"


def test_multipart_from_markdown(mocker: MockerFixture) -> None:
    mocker.patch("openqa_label_known_issues.get_markdown_html", return_value="<p>html</p>")
    res = openqa_label_known_issues.multipart_from_markdown("plain text", "to@ex.com", "from@ex.com", "Subject")
    assert "To: to@ex.com" in res
    assert "From: from@ex.com" in res
    assert "Subject: Subject" in res
    assert "plain text" in res
    assert "<html><body>\n<p>html</p></body></html>" in res


def test_send_email(mocker: MockerFixture) -> None:
    # Dry run
    with patch("builtins.print") as mock_print:
        openqa_label_known_issues.send_email("to@ex.com", "content", dry_run=True)
        mock_print.assert_called_once_with("Would send email to 'to@ex.com':\ncontent")

    # Real run success
    mock_run = mocker.patch("subprocess.run")
    openqa_label_known_issues.send_email("to@ex.com", "content", dry_run=False)
    mock_run.assert_called_once_with(["/usr/sbin/sendmail", "-t", "to@ex.com"], input="content", text=True, check=True)

    # Real run failure
    mock_run.reset_mock()
    mock_run.side_effect = Exception("error")
    with patch("builtins.print") as mock_print:
        openqa_label_known_issues.send_email("to@ex.com", "content", dry_run=False)
        mock_print.assert_any_call("Failed to send email: error", file=sys.stderr)


def test_extract_excerpt(tmp_path: pathlib.Path) -> None:
    # Missing file
    assert openqa_label_known_issues.extract_excerpt("nonexistent_file") == "    # (No log excerpt found)"

    # Empty file
    f_empty = tmp_path / "empty_log"
    f_empty.write_text("")
    assert openqa_label_known_issues.extract_excerpt(str(f_empty)) == "    # (No log excerpt found)"

    # File with exactly 1 matched line (clean_lines has 1 element and is sliced to empty)
    f_one = tmp_path / "one_line_match"
    f_one.write_text("Result: died")
    assert openqa_label_known_issues.extract_excerpt(str(f_one)) == ""

    # Backend process died
    f = tmp_path / "log1"
    f.write_text("line1\nBackend process died, backend errors are reported below in the following lines\nline2\nline3")
    assert "Backend process died" in openqa_label_known_issues.extract_excerpt(str(f))

    # sending magic and exit
    f = tmp_path / "log2"
    f.write_text("line1\nline2\nsending magic and exit\n")
    assert "line2" in openqa_label_known_issues.extract_excerpt(str(f))

    # killing command server...
    f = tmp_path / "log3"
    f.write_text("line1\nkilling command server...because test execution ended through exception\n")
    assert "line1" in openqa_label_known_issues.extract_excerpt(str(f))

    # EXIT 1
    f = tmp_path / "log4"
    f.write_text("some error\nEXIT 1\n")
    assert "some error" in openqa_label_known_issues.extract_excerpt(str(f))

    # Result: died
    f = tmp_path / "log5"
    f.write_text("some output\nResult: died\n")
    assert "some output" in openqa_label_known_issues.extract_excerpt(str(f))

    # Fallback
    f = tmp_path / "log6"
    f.write_text("no matching lines here\n")
    assert openqa_label_known_issues.extract_excerpt(str(f)) == "    # (No log excerpt found)"


def test_comment_on_job(mocker: MockerFixture) -> None:
    # 1. Success without force_result
    mock_run = mocker.patch("subprocess.run")
    openqa_label_known_issues.comment_on_job("123", "my comment", "", ["openqa-cli"])
    mock_run.assert_called_once_with(
        ["openqa-cli", "-X", "POST", "jobs/123/comments", "text=my comment"], check=True, capture_output=True, text=True
    )

    # 2. Success with force_result and enable_force_result
    mock_run.reset_mock()
    mocker.patch.dict("os.environ", {"enable_force_result": "true"})
    openqa_label_known_issues.comment_on_job("123", "my comment", "softfailed", ["openqa-cli"])
    mock_run.assert_called_once_with(
        ["openqa-cli", "-X", "POST", "jobs/123/comments", "text=label:force_result:softfailed:my comment"],
        check=True,
        capture_output=True,
        text=True,
    )

    # 3. Failed subprocess
    mock_run.reset_mock()
    mock_run.side_effect = subprocess.CalledProcessError(1, "cmd", stderr="error string")
    with patch("builtins.print") as mock_print:
        openqa_label_known_issues.comment_on_job("123", "comment", "", ["openqa-cli"])
        mock_print.assert_called_once_with("Failed to comment on job 123: error string", file=sys.stderr)


def test_restart_job(mocker: MockerFixture) -> None:
    # Success
    mock_run = mocker.patch("subprocess.run")
    openqa_label_known_issues.restart_job("123", ["openqa-cli"])
    mock_run.assert_called_once_with(
        ["openqa-cli", "-X", "POST", "jobs/123/restart"], check=True, capture_output=True, text=True
    )

    # Failure
    mock_run.reset_mock()
    mock_run.side_effect = subprocess.CalledProcessError(1, "cmd", stderr="restart error")
    with patch("builtins.print") as mock_print:
        openqa_label_known_issues.restart_job("123", ["openqa-cli"])
        mock_print.assert_called_once_with("Failed to restart job 123: restart error", file=sys.stderr)


def test_search_log(tmp_path: pathlib.Path) -> None:
    # Missing file
    with patch("builtins.print") as mock_print:
        assert openqa_label_known_issues.search_log("patt", "nonexistent") is False
        mock_print.assert_called_once()

    # Pattern matches
    f = tmp_path / "report"
    f.write_text("some log with pattern text")
    assert openqa_label_known_issues.search_log("pattern", str(f)) is True
    assert openqa_label_known_issues.search_log("absent", str(f)) is False

    # Invalid regex compilation
    with pytest.raises(SystemExit) as exc:
        openqa_label_known_issues.search_log("[invalid", str(f))
    assert exc.value.code == 2


def test_label_on_issue(mocker: MockerFixture) -> None:
    # No match
    mocker.patch("openqa_label_known_issues.search_log", return_value=False)
    mock_comment = mocker.patch("openqa_label_known_issues.comment_on_job")
    assert openqa_label_known_issues.label_on_issue("123", "patt", "lbl", "file", "", "", []) is False
    mock_comment.assert_not_called()

    # Match without restart
    mocker.patch("openqa_label_known_issues.search_log", return_value=True)
    mock_comment.reset_mock()
    mock_restart = mocker.patch("openqa_label_known_issues.restart_job")
    assert openqa_label_known_issues.label_on_issue("123", "patt", "lbl", "file", "", "force", ["api"]) is True
    mock_comment.assert_called_once_with("123", "lbl", "force", ["api"])
    mock_restart.assert_not_called()

    # Match with restart
    mock_comment.reset_mock()
    assert openqa_label_known_issues.label_on_issue("123", "patt", "lbl", "file", "1", "force", ["api"]) is True
    mock_comment.assert_called_once_with("123", "lbl", "force", ["api"])
    mock_restart.assert_called_once_with("123", ["api"])


def test_label_on_issues_from_issue_tracker(mocker: MockerFixture) -> None:
    # Minimal setup
    mocker.patch.dict("os.environ", {"min_search_term": "5"})
    issues_list = [
        {
            "id": "1",
            "subject": 'test auto_review:"match_me":retry:force_result:softfailed',
            "tracker_name": "openqa-force-result",
        }
    ]
    mock_lbl = mocker.patch("openqa_label_known_issues.label_on_issue", return_value=True)
    assert openqa_label_known_issues.label_on_issues_from_issue_tracker("123", issues_list, "file", ["api"]) is True
    mock_lbl.assert_called_once_with(
        "123",
        "match_me",
        'poo#1 test auto_review:"match_me":retry:force_result:softfailed',
        "file",
        "1",
        "softfailed",
        ["api"],
    )

    # match_force is False and restart is False (covers branches 213->221, restart=False, and 223->195 loop continue when label_on_issue returns False)
    issues_list_no_force_no_retry = [
        {
            "id": "10",
            "subject": 'test auto_review:"match_me"',
            "tracker_name": "openqa-force-result",
        },
        {
            "id": "11",
            "subject": 'test auto_review:"match_again"',
            "tracker_name": "openqa-force-result",
        },
    ]
    mock_lbl.reset_mock()
    mock_lbl.side_effect = [False, True]
    assert (
        openqa_label_known_issues.label_on_issues_from_issue_tracker(
            "123", issues_list_no_force_no_retry, "file", ["api"]
        )
        is True
    )
    assert mock_lbl.call_count == 2

    # split with only one double quote (after is parts[1] but search empty)
    issues_list_one_quote = [
        {
            "id": "5",
            "subject": 'test auto_review:"match_me',
            "tracker_name": "openqa-force-result",
        }
    ]
    mock_lbl.reset_mock()
    mock_lbl.side_effect = None
    assert (
        openqa_label_known_issues.label_on_issues_from_issue_tracker("123", issues_list_one_quote, "file", ["api"])
        is False
    )
    mock_lbl.assert_not_called()

    # tracker mismatch
    issues_list2 = [
        {
            "id": "2",
            "subject": 'test auto_review:"match_me":force_result:softfailed',
            "tracker_name": "other-tracker",
        }
    ]
    mock_lbl.reset_mock()
    mock_lbl.return_value = True
    assert openqa_label_known_issues.label_on_issues_from_issue_tracker("123", issues_list2, "file", ["api"]) is True
    mock_lbl.assert_called_once_with(
        "123",
        "match_me",
        'poo#2 test auto_review:"match_me":force_result:softfailed (ignoring force result for ticket which is not in tracker "openqa-force-result")',
        "file",
        "",
        "",
        ["api"],
    )

    # short search term
    issues_list3 = [
        {
            "id": "3",
            "subject": 'test auto_review:"abc"',
            "tracker_name": "openqa-force-result",
        }
    ]
    mock_lbl.reset_mock()
    assert openqa_label_known_issues.label_on_issues_from_issue_tracker("123", issues_list3, "file", ["api"]) is False
    mock_lbl.assert_not_called()

    # no quotes
    issues_list4 = [
        {
            "id": "4",
            "subject": "test auto_review: match_me",
            "tracker_name": "openqa-force-result",
        }
    ]
    mock_lbl.reset_mock()
    assert openqa_label_known_issues.label_on_issues_from_issue_tracker("123", issues_list4, "file", ["api"]) is False
    mock_lbl.assert_not_called()


def test_label_on_issues_without_tickets(mocker: MockerFixture) -> None:
    mock_lbl = mocker.patch("openqa_label_known_issues.label_on_issue", return_value=False)
    assert openqa_label_known_issues.label_on_issues_without_tickets("123", "file", ["api"]) is False
    assert mock_lbl.call_count == 9

    mock_lbl.reset_mock()
    mock_lbl.side_effect = [False, True]
    assert openqa_label_known_issues.label_on_issues_without_tickets("123", "file", ["api"]) is True
    assert mock_lbl.call_count == 2


def test_handle_unreachable(mocker: MockerFixture) -> None:
    mock_client = MagicMock(spec=httpx.Client)

    # 1. testurl head failure, host_url not in testurl
    mock_client.head.side_effect = Exception("conn err")
    with patch("builtins.print") as mock_print:
        res = openqa_label_known_issues.handle_unreachable("http://test.com", "123", mock_client, "http://host.com", [])
        assert res == 1
        mock_print.assert_called_once_with(
            "'http://test.com' is not reachable and 'host_url' parameter does not match 'http://test.com', can not check further, continuing with next",
            file=sys.stderr,
        )

    # 2. testurl head failure, host_url in testurl, host_url unreachable (head exception branch)
    mock_client.head.side_effect = Exception("conn err")
    with patch("builtins.print") as mock_print:
        res = openqa_label_known_issues.handle_unreachable(
            "http://host.com/test", "123", mock_client, "http://host.com", []
        )
        assert res == 1
        mock_print.assert_any_call("'http://host.com' is not reachable, bailing out", file=sys.stderr)
        mock_print.assert_any_call(
            "'http://host.com/test' is not reachable, assuming deleted, continuing with next", file=sys.stderr
        )

    # 3. testurl head failure, host_url in testurl, host_url is reachable (returns 200)
    mock_client.head.side_effect = [Exception("conn err"), Mock(status_code=200)]
    with patch("builtins.print") as mock_print:
        res = openqa_label_known_issues.handle_unreachable(
            "http://host.com/test", "123", mock_client, "http://host.com", []
        )
        assert res == 1
        mock_print.assert_called_once_with(
            "'http://host.com/test' is not reachable, assuming deleted, continuing with next", file=sys.stderr
        )

    # 4. testurl reachable but download GET fails
    mock_resp_head = Mock(status_code=200)
    mock_client.head.side_effect = None
    mock_client.head.return_value = mock_resp_head
    mock_client.get.side_effect = Exception("download err")
    with patch("builtins.print") as mock_print, pytest.raises(SystemExit) as exc:
        openqa_label_known_issues.handle_unreachable("http://host.com/test", "123", mock_client, "http://host.com", [])
    assert exc.value.code == 2
    mock_print.assert_called_once_with(
        "'http://host.com/test' can be reached but not downloaded, bailing out", file=sys.stderr
    )

    # 5. testurl downloaded, Gru job failed pattern found
    mock_resp_get = Mock(status_code=200, text="Gru job failed connection error Inactivity timeout")
    mock_client.get.side_effect = None
    mock_client.get.return_value = mock_resp_get
    mock_comment = mocker.patch("openqa_label_known_issues.comment_on_job")
    mock_restart = mocker.patch("openqa_label_known_issues.restart_job")

    res = openqa_label_known_issues.handle_unreachable(
        "http://host.com/test", "123", mock_client, "http://host.com", []
    )
    assert res == 1
    mock_comment.assert_called_once()
    mock_restart.assert_called_once()

    # 6. testurl downloaded, Gru pattern not found, older than 14 days
    mock_resp_get.text = 'Result:<abbr class="timeago" title="2020-01-01T00:00:00Z"></abbr>'
    mock_comment.reset_mock()
    mock_restart.reset_mock()
    with patch("builtins.print") as mock_print:
        res = openqa_label_known_issues.handle_unreachable(
            "http://host.com/test", "123", mock_client, "http://host.com", []
        )
        assert res == 1
        mock_print.assert_called_once_with(
            "'http://host.com/test' job#123 without autoinst-log.txt older than 14 days. Do not label", file=sys.stderr
        )

    # 7. testurl downloaded, Gru not found, younger than 14 days
    from datetime import datetime, timezone

    now_str = datetime.now(timezone.utc).isoformat()
    mock_resp_get.text = f'Result:<abbr class="timeago" title="{now_str}"></abbr>'
    res = openqa_label_known_issues.handle_unreachable(
        "http://host.com/test", "123", mock_client, "http://host.com", []
    )
    assert res == 0

    # 8. testurl downloaded, younger than 14 days, KEEP_JOB_HTML_FILE is true (unlink skipped / test cleanup branches)
    mocker.patch.dict("os.environ", {"KEEP_JOB_HTML_FILE": "1"})
    res = openqa_label_known_issues.handle_unreachable(
        "http://host.com/test", "123", mock_client, "http://host.com", []
    )
    assert res == 0

    # 9. Exception during unlink (cleanup exception branch covers line 342-343)
    mocker.patch.dict("os.environ", {"KEEP_JOB_HTML_FILE": "0"}, clear=True)
    mocker.patch("pathlib.Path.unlink", side_effect=Exception("unlink err"))
    res = openqa_label_known_issues.handle_unreachable(
        "http://host.com/test", "123", mock_client, "http://host.com", []
    )
    assert res == 0


def test_handle_unreviewed(mocker: MockerFixture, tmp_path: pathlib.Path) -> None:
    f = tmp_path / "report"
    f.write_text("some log contents")
    mocker.patch("openqa_label_known_issues.extract_excerpt", return_value="my excerpt")
    mocker.patch("openqa_label_known_issues.multipart_from_markdown", return_value="multipart-email")
    mock_send = mocker.patch("openqa_label_known_issues.send_email")

    # 1. email_unreviewed is false
    with patch("builtins.print") as mock_print:
        openqa_label_known_issues.handle_unreviewed(
            "http://testurl", str(f), "my reason", "24", False, "from@ex.com", "notif@ex.com", {}, True, []
        )
        mock_print.assert_any_call(
            "[http://testurl](http://testurl): Unknown test issue, to be reviewed\n-> [autoinst-log.txt](http://testurl/file/autoinst-log.txt)\n"
        )
    mock_send.assert_not_called()

    # 2. email_unreviewed is true, group_id is null
    mock_send.reset_mock()
    openqa_label_known_issues.handle_unreviewed(
        "http://testurl", str(f), "my reason", "null", True, "from@ex.com", "notif@ex.com", {}, True, []
    )
    mock_send.assert_not_called()

    # 3. email_unreviewed true, group_id valid, clone_id is not null
    mock_send.reset_mock()
    job_data = {"job": {"clone_id": "12345"}}
    openqa_label_known_issues.handle_unreviewed(
        "http://testurl", str(f), "my reason", "24", True, "from@ex.com", "notif@ex.com", job_data, True, []
    )
    mock_send.assert_not_called()

    # 4. email_unreviewed true, group_id valid, group data fetch success, MAILTO found, clone_id is null
    mock_send.reset_mock()
    job_data_null = {"job": {"clone_id": "null", "name": "myjob", "result": "failed"}}
    mock_sub = mocker.patch("subprocess.run")
    mock_sub.return_value = Mock(stdout='[{"name": "Lala", "description": "MAILTO: dest@ex.com"}]')
    openqa_label_known_issues.handle_unreviewed(
        "http://testurl", str(f), "my reason", "24", True, "from@ex.com", "notif@ex.com", job_data_null, True, []
    )
    mock_send.assert_called_once_with("dest@ex.com", "multipart-email", True)

    # 5. group data fetch fails, fallback to notification address
    mock_send.reset_mock()
    mock_sub.side_effect = Exception("err")
    openqa_label_known_issues.handle_unreviewed(
        "http://testurl", str(f), "my reason", "24", True, "from@ex.com", "notif@ex.com", job_data_null, True, []
    )
    mock_send.assert_called_once_with("notif@ex.com", "multipart-email", True)

    # 6. group data fetch succeeds but MAILTO not found, fallback to notification address
    mock_send.reset_mock()
    mock_sub.side_effect = None
    mock_sub.return_value = Mock(stdout='[{"name": "Lala", "description": "no mailto info here"}]')
    openqa_label_known_issues.handle_unreviewed(
        "http://testurl", str(f), "my reason", "24", True, "from@ex.com", "notif@ex.com", job_data_null, True, []
    )
    mock_send.assert_called_once_with("notif@ex.com", "multipart-email", True)

    # 7. group data succeeds, MAILTO not found, no notification address (should not send)
    mock_send.reset_mock()
    openqa_label_known_issues.handle_unreviewed(
        "http://testurl", str(f), "my reason", "24", True, "from@ex.com", "", job_data_null, True, []
    )
    mock_send.assert_not_called()


def test_fetch_issues(mocker: MockerFixture) -> None:
    mock_client = MagicMock(spec=httpx.Client)

    # 1. From environment variable 'issues'
    mocker.patch.dict("os.environ", {"issues": "123\nsubject1\ntracker1\n456\nsubject2\ntracker2"})
    issues = openqa_label_known_issues.fetch_issues(mock_client, "http://query")
    assert len(issues) == 2
    assert issues[0]["id"] == "123"
    assert issues[1]["tracker_name"] == "tracker2"

    # 2. From query URL success
    mocker.patch.dict("os.environ", {}, clear=True)
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = {"issues": [{"id": 789, "subject": "subject3", "tracker": {"name": "tracker3"}}]}
    mock_client.get.return_value = mock_resp
    issues = openqa_label_known_issues.fetch_issues(mock_client, "http://query")
    assert len(issues) == 1
    assert issues[0]["id"] == "789"

    # 3. From query URL failure
    mock_client.get.side_effect = Exception("http error")
    with patch("builtins.print") as mock_print:
        issues = openqa_label_known_issues.fetch_issues(mock_client, "http://query")
        assert issues == []
        mock_print.assert_called_once_with("Error fetching issues from Redmine: http error", file=sys.stderr)


def test_investigate_issue(mocker: MockerFixture, tmp_path: pathlib.Path) -> None:
    mock_client = MagicMock(spec=httpx.Client)

    # 1. Invalid job ID
    with patch("builtins.print") as mock_print:
        openqa_label_known_issues.investigate_issue("http://host/tests/abc", mock_client, [], [], "http://host")
        mock_print.assert_called_once_with("Invalid job ID extracted from http://host/tests/abc", file=sys.stderr)

    # 2. job_data fetch fails
    mock_sub = mocker.patch("subprocess.run")
    mock_sub.side_effect = Exception("err")
    with patch("builtins.print") as mock_print:
        openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")
        mock_print.assert_any_call("Failed to load job data for 123: err", file=sys.stderr)

    # 3. job state not done / passed (should return early)
    mock_sub.side_effect = None
    mock_sub.return_value = Mock(stdout='{"job": {"state": "running", "result": "none"}}')
    mock_client_get = mocker.patch.object(mock_client, "get")
    openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")
    mock_client_get.assert_not_called()

    # 4. log fetch returns 200, handles issues tracker matched
    mock_sub.return_value = Mock(stdout='{"job": {"state": "done", "result": "failed", "reason": "reason"}}')
    mock_resp_log = Mock(status_code=200, text="log content\n")
    mock_client.get.return_value = mock_resp_log

    mock_tracker = mocker.patch("openqa_label_known_issues.label_on_issues_from_issue_tracker", return_value=True)
    openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")
    mock_tracker.assert_called_once()

    # 5. log fetch 404, reason null, unreachable fails
    mock_tracker.reset_mock()
    mock_tracker.return_value = False
    mock_sub.return_value = Mock(stdout='{"job": {"state": "done", "result": "failed", "reason": "null"}}')
    mock_resp_log.status_code = 404
    mock_client.get.return_value = mock_resp_log

    mock_unreachable = mocker.patch("openqa_label_known_issues.handle_unreachable", return_value=1)
    openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")
    mock_unreachable.assert_called_once()

    # 6. log fetch 404, reason null, unreachable returns 0 (should print cannot label)
    mock_unreachable.reset_mock()
    mock_unreachable.return_value = 0
    with patch("builtins.print") as mock_print:
        openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")
        mock_print.assert_any_call(
            "'http://host/tests/123' does not have autoinst-log.txt or reason, cannot label", file=sys.stderr
        )

    # 7. log fetch returns 500 (not 404/200/301), reason is None, autoinst-log has no trailing newline
    mock_resp_log.status_code = 500
    mock_resp_log.text = "internal error"
    mock_client.get.return_value = mock_resp_log
    mock_sub.return_value = Mock(
        stdout='{"job": {"state": "done", "result": "failed", "reason": null, "group_id": null}}'
    )
    openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")
    # unreachable matched and returns early

    # 8. REPORT_FILE, KEEP_REPORT_FILE set
    mock_unreachable.return_value = 0
    mocker.patch.dict("os.environ", {"REPORT_FILE": str(tmp_path / "custom_report"), "KEEP_REPORT_FILE": "1"})
    mock_resp_log.status_code = 200
    mock_resp_log.text = "some text"
    openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")
    assert (tmp_path / "custom_report").exists()

    # 9. Exception during report file unlink (covers lines 544-545 finally cleanup branch)
    mocker.patch.dict("os.environ", {"REPORT_FILE": "", "KEEP_REPORT_FILE": "0"}, clear=True)
    mocker.patch("pathlib.Path.unlink", side_effect=Exception("unlink err"))
    openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")

    # 10. label_on_issues_without_tickets returns True (covers line 519 return)
    mocker.patch("pathlib.Path.unlink", side_effect=None)
    mock_unreachable.return_value = 0
    mock_sub.return_value = Mock(stdout='{"job": {"state": "done", "result": "failed", "reason": "myreason"}}')
    mock_resp_log.status_code = 200
    mock_resp_log.text = "Compilation failed in require at isotovideo line 28."
    mock_client.get.return_value = mock_resp_log
    mocker.patch("openqa_label_known_issues.label_on_issues_without_tickets", return_value=True)
    openqa_label_known_issues.investigate_issue("http://host/tests/123", mock_client, [], [], "http://host")


def test_main(mocker: MockerFixture) -> None:
    # 1. Missing job URL
    with pytest.raises(typer.Exit) as exc, patch("builtins.print") as mock_print:
        openqa_label_known_issues.main(None)
    assert exc.value.exit_code == 1
    mock_print.assert_called_once_with("Need 'testurl'", file=sys.stderr)

    # 2. Main runs successfully (dry and dry_run env variables branches)
    mocker.patch("openqa_label_known_issues.fetch_issues", return_value=[])
    mock_investigate = mocker.patch("openqa_label_known_issues.investigate_issue")
    openqa_label_known_issues.main("http://host/tests/123", host="test.org", dry=True)
    mock_investigate.assert_called_once()

    # 3. Main with dry_run env variable already set
    mocker.patch.dict(
        "os.environ",
        {"dry_run": "1", "host": "test.org", "scheme": "http", "retries": "5", "issue_marker": "x", "issue_query": "y"},
    )
    mock_investigate.reset_mock()
    openqa_label_known_issues.main("http://host/tests/123")
    mock_investigate.assert_called_once()

    # 4. Main with dry=False explicitly and dry_run is not set (covers dry=False branch)
    mocker.patch.dict("os.environ", {}, clear=True)
    mock_investigate.reset_mock()
    openqa_label_known_issues.main("http://host/tests/123", host=None, dry=False)
    mock_investigate.assert_called_once()
