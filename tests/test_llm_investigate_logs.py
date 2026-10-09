# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.logs."""

from __future__ import annotations

from typing import Any

import pytest

from openqa_llm_investigate import logs


def test_get_serial_tail_returns_all_when_log_shorter_than_lines() -> None:
    log = "a\nb\nc"
    result = logs.get_serial_tail(log, lines=10)
    assert result == ["a", "b", "c"]


def test_get_serial_tail_returns_last_n_when_log_longer() -> None:
    log = "\n".join(str(i) for i in range(50))
    result = logs.get_serial_tail(log, lines=5)
    assert result == ["45", "46", "47", "48", "49"]


def test_get_serial_tail_default_lines_is_75() -> None:
    log = "\n".join(str(i) for i in range(200))
    result = logs.get_serial_tail(log)
    assert len(result) == 75
    assert result[0] == "125"
    assert result[-1] == "199"


def test_get_serial_tail_empty_string_returns_single_empty() -> None:
    assert logs.get_serial_tail("", lines=10) == [""]


def test_get_serial_tail_exactly_n_lines() -> None:
    log = "a\nb\nc\nd\ne"
    result = logs.get_serial_tail(log, lines=5)
    assert result == ["a", "b", "c", "d", "e"]


def test_find_in_log_finds_match_in_middle() -> None:
    log = "a\nb\nNEEDLE\nd\ne"
    result = logs.find_in_log(log, "NEEDLE", context_lines_before=1, context_lines_after=1)
    assert result == {"before": ["b"], "matched": "NEEDLE", "after": ["d"]}


def test_find_in_log_returns_none_when_not_found() -> None:
    assert logs.find_in_log("a\nb\nc", "missing") is None


def test_find_in_log_returns_none_when_search_string_empty() -> None:
    assert logs.find_in_log("a\nb\nc", "") is None


def test_find_in_log_returns_none_when_search_string_none() -> None:
    assert logs.find_in_log("a\nb\nc", None) is None


def test_find_in_log_prefers_command_line_over_plain_match() -> None:
    log = "plain foo here\n# foo command here\nplain foo again"
    result = logs.find_in_log(log, "foo", context_lines_before=0, context_lines_after=0)
    assert result is not None
    assert result["matched"] == "# foo command here"


def test_find_in_log_uses_last_plain_match_when_no_command() -> None:
    log = "first foo\nsecond foo\nthird foo"
    result = logs.find_in_log(log, "foo", context_lines_before=0, context_lines_after=0)
    assert result is not None
    assert result["matched"] == "third foo"


def test_find_in_log_uses_last_command_match_when_multiple() -> None:
    log = "# foo one\n# foo two\n# foo three"
    result = logs.find_in_log(log, "foo", context_lines_before=0, context_lines_after=0)
    assert result is not None
    assert result["matched"] == "# foo three"


def test_find_in_log_clips_before_at_start() -> None:
    log = "NEEDLE\nb\nc"
    result = logs.find_in_log(log, "NEEDLE", context_lines_before=5, context_lines_after=1)
    assert result is not None
    assert result["before"] == []
    assert result["after"] == ["b"]


def test_find_in_log_clips_after_at_end() -> None:
    log = "a\nb\nNEEDLE"
    result = logs.find_in_log(log, "NEEDLE", context_lines_before=1, context_lines_after=5)
    assert result is not None
    assert result["before"] == ["b"]
    assert result["after"] == []


def test_find_in_log_after_defaults_to_before() -> None:
    log = "a\nb\nNEEDLE\nd\ne\nf"
    result = logs.find_in_log(log, "NEEDLE", context_lines_before=2)
    assert result is not None
    assert result["before"] == ["a", "b"]
    assert result["after"] == ["d", "e"]


def test_find_in_log_independent_before_after() -> None:
    log = "a\nb\nc\nNEEDLE\nd\ne\nf"
    result = logs.find_in_log(log, "NEEDLE", context_lines_before=1, context_lines_after=3)
    assert result is not None
    assert result["before"] == ["c"]
    assert result["after"] == ["d", "e", "f"]


_FIND_LOG = "line 0 alpha\nline 1 beta\nline 2 gamma\nline 3 delta\nline 4 epsilon\nline 5 zeta\nline 6 eta"


def test_find_log_range_both_markers_found() -> None:
    result = logs.find_log_range(_FIND_LOG, "beta", "epsilon")
    assert result is not None
    assert result[0] == "line 1 beta"
    assert result[-1] == "line 4 epsilon"
    assert len(result) == 4


def test_find_log_range_only_start_marker_found() -> None:
    result = logs.find_log_range(_FIND_LOG, "gamma", "MISSING")
    assert result is not None
    assert result[0] == "line 2 gamma"


def test_find_log_range_only_end_marker_found_returns_none() -> None:
    """Without a start marker there is no sensible range start — return None."""
    result = logs.find_log_range(_FIND_LOG, "MISSING", "delta")
    assert result is None


def test_find_log_range_none_start_marker_returns_none() -> None:
    """Passing None as start_marker must return None even if end_marker is found."""
    result = logs.find_log_range(_FIND_LOG, None, "delta")
    assert result is None


def test_find_log_range_neither_marker_found_returns_none() -> None:
    result = logs.find_log_range(_FIND_LOG, "MISSING1", "MISSING2")
    assert result is None


def test_find_log_range_empty_log_returns_none() -> None:
    assert logs.find_log_range("", "x", "y") is None


def test_find_log_range_markers_in_wrong_order_uses_start_only() -> None:
    result = logs.find_log_range(_FIND_LOG, "delta", "beta")
    assert result is not None
    # Should fall back to start-only mode
    assert result[0] == "line 3 delta"


def test_find_log_range_same_marker_for_start_and_end() -> None:
    result = logs.find_log_range(_FIND_LOG, "gamma", "gamma")
    assert result is not None
    assert result[0] == "line 2 gamma"
    assert result[-1] == "line 2 gamma"


def test_find_log_range_max_lines_caps_bounded_span() -> None:
    """When both markers found and span > max_lines, result is capped."""
    # beta..epsilon is 4 lines; cap at 2 → first 1 + last 1 + omission marker
    result = logs.find_log_range(_FIND_LOG, "beta", "epsilon", max_lines=2)
    assert result is not None
    assert len(result) == 3  # 1 + omission marker + 1
    assert "omitted" in result[1]
    assert result[0] == "line 1 beta"
    assert result[-1] == "line 4 epsilon"


def test_find_log_range_max_lines_span_fits_no_omission() -> None:
    """When span <= max_lines, no omission marker is inserted."""
    result = logs.find_log_range(_FIND_LOG, "beta", "epsilon", max_lines=10)
    assert result is not None
    assert len(result) == 4
    assert not any("omitted" in ln for ln in result)


def test_find_log_range_max_lines_caps_fallback() -> None:
    """When only start marker found, max_lines caps the fallback too."""
    # With max_lines=2, only start_idx:start_idx+2 returned (default would be 200)
    result = logs.find_log_range(_FIND_LOG, "gamma", "MISSING", max_lines=2)
    assert result is not None
    assert len(result) == 2
    assert result[0] == "line 2 gamma"


def test_find_log_range_max_lines_none_uses_200_fallback() -> None:
    """max_lines=None keeps old 200-line fallback behaviour for start-only case."""
    # 7-line log; should return remaining lines from start marker onward
    result = logs.find_log_range(_FIND_LOG, "gamma", "MISSING", max_lines=None)
    assert result is not None
    # gamma is index 2; log has 7 lines → 5 lines from gamma to end
    assert result[0] == "line 2 gamma"
    assert len(result) == 5


def test_find_log_range_omission_marker_shows_count() -> None:
    """The omission marker reports the number of lines omitted."""
    # Log: lines 0..6 (7 lines); span beta..eta = lines 1..6 = 6 lines
    # max_lines=2 → omit 4 lines
    result = logs.find_log_range(_FIND_LOG, "beta", "eta", max_lines=2)
    assert result is not None
    marker = result[1]
    assert "4" in marker  # 4 lines omitted


_KDUMP = "\n".join(
    [
        "boot",
        "sysrq: Trigger a crash",
        "Kernel panic - not syncing: sysrq triggered crash",
        "RAX: 1",
        "Code: 90 90",
        "trace1",
        "reboot",
    ]
    + [f"filler {i}" for i in range(40)]
    + [
        "kdump boot",
        "BUG: kernel NULL pointer dereference in vmbus_chan_sched",
        "RIP: 0010:vmbus_chan_sched",
        "RSP: 0018:ffff",
        "Call Trace: vmbus",
        "Kernel panic - not syncing: Fatal exception in interrupt",
    ]
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Permission to access 'https://updates.suse.com/x?credentials=a' denied.",
            "repo auth failed (updates.suse.com)",
        ),
        (
            "Download (curl) error for 'https://scc.suse.com/repo/x': Error code: HTTP response: 401",
            "repo auth failed (scc.suse.com)",
        ),
        ("No enabled repos defined.", "system not registered (no enabled repos)"),
        (
            "Machine type 'c3' does not exist in zone europe-central2-c",
            "cloud capacity or machine type unavailable",
        ),
        ("REGION UNAVAILABLE", "cloud capacity or machine type unavailable"),
        (
            "dracut: FATAL: FIPS integrity test failed",
            "initrd FIPS integrity failure",
        ),
        (
            "Synchronous Exception at 0xFD52",
            "firmware crash (Synchronous Exception)",
        ),
        (
            (
                "SELinux access check scon=system_u:system_r:local_login_t:s0 "
                "tcon=systemd_unit_file_t tclass=service perm=start state=enforcing"
            ),
            "SELinux denial (local_login_t -> systemd_unit_file_t start)",
        ),
        ("pam: SELinux policy denies access", "SELinux denial"),
        ("x\nDIALOG: Warning", 'unexpected dialog: "Warning"'),
        ("KDUMP_OOPS: vmbus_chan_sched", "crash kernel oops in vmbus_chan_sched"),
    ],
)
def test_key_error_rule(text: Any, expected: Any) -> None:
    assert logs.summarize_key_error(text) == expected


def test_key_error_no_match_is_none() -> None:
    assert logs.summarize_key_error("all fine\n") is None


def test_kdump_oops_symbol() -> None:
    assert logs.kdump_oops_symbol(_KDUMP) == "vmbus_chan_sched"
    assert logs.kdump_oops_symbol("Kernel panic - not syncing\n") is None


def test_key_error_cloud_init() -> None:
    assert logs.summarize_key_error("cloud-init[1]: util.py[ERROR]: boom") == "cloud-init error"


def test_log_lines_filters_boilerplate() -> None:
    lines = ["real log line 1", "echo tY~XN; bash -oe pipefail /tmp/scripttY~HOST.sh", "real log line 2"]
    assert logs.log_lines(lines) == ["real log line 1", "real log line 2"]


def test_log_lines_redact() -> None:
    assert logs.log_lines(["code INTERNAL-USE-ONLY-cafe-beef"]) == ["code INTERNAL-USE-ONLY-****-****"]


def test_matched_line_adds_marker() -> None:
    assert logs.matched_line("the matched line") == [">>> the matched line"]


def test_matched_line_strips_escapes() -> None:
    assert logs.matched_line("\x1b[33mTest died\x1b[0m") == [">>> Test died"]


def test_matched_line_suppresses_boilerplate() -> None:
    assert not logs.matched_line("echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh")


def test_format_log_context() -> None:
    context = logs.find_in_log("a\nb\nNEEDLE\nd\ne", "NEEDLE", 1)
    assert context is not None
    assert logs.format_log_context(context) == ["b", ">>> NEEDLE", "d"]


def test_scan_kernel_crash_reports_panic(make_openqa: Any, sample_serial0_panic: str) -> None:
    oqa = make_openqa({"/tests/123/file/serial0.txt": sample_serial0_panic})
    serial0, report = logs.scan_kernel_crash(oqa, "123", 5)
    assert serial0 == sample_serial0_panic
    assert "*** KERNEL PANIC DETECTED ***" in report
    assert any(ln.startswith(">>> ") for ln in report)
    assert "--- End of Log Section ---" in report


def test_scan_kernel_crash_clean_log_is_silent(make_openqa: Any, sample_serial_log: str) -> None:
    oqa = make_openqa({"/tests/123/file/serial0.txt": sample_serial_log})
    assert logs.scan_kernel_crash(oqa, "123", 5) == (sample_serial_log, [])


def test_scan_kernel_crash_unavailable_files_are_silent(make_openqa: Any) -> None:
    assert logs.scan_kernel_crash(make_openqa({}), "123", 5) == ("", [])


def test_scan_kernel_crash_masks_registration_codes(make_openqa: Any, sample_serial0_panic: str) -> None:
    oqa = make_openqa({"/tests/123/file/serial0.txt": sample_serial0_panic})
    report = "\n".join(logs.scan_kernel_crash(oqa, "123", 100)[1])
    assert "INTERNAL-USE-ONLY-cafe-beef" not in report
    assert "INTERNAL-USE-ONLY-****-****" in report


def test_scan_kernel_crash_searches_destroy_console_by_default(make_openqa: Any, sample_serial0_panic: str) -> None:
    oqa = make_openqa({"/tests/1/file/destroy-console.txt": sample_serial0_panic})
    report = logs.scan_kernel_crash(oqa, "1", 5)[1]
    assert "--- destroy-console.txt (searching for: Kernel panic) ---" in report


def test_scan_kernel_crash_uses_given_console_files(make_openqa: Any, sample_serial0_panic: str) -> None:
    oqa = make_openqa({
        "/tests/1/file/x-console-end.txt": sample_serial0_panic,
        "/tests/1/file/destroy-console.txt": "",
    })
    report = logs.scan_kernel_crash(oqa, "1", 5, ["x-console-end.txt"])[1]
    assert "--- x-console-end.txt (searching for: Kernel panic) ---" in report


def _scan(make_openqa: Any, content: str, context_lines: int = 100) -> list[str]:
    return logs.scan_kernel_crash(make_openqa({"/tests/1/file/serial0.txt": content}), "1", context_lines)[1]


def test_kdump_shows_last_unexpected_event_without_registers(make_openqa: Any) -> None:
    out = "\n".join(_scan(make_openqa, _KDUMP))
    assert "2 fatal events found; showing the last unexpected" in out
    assert ">>> BUG: kernel NULL pointer dereference in vmbus_chan_sched" in out
    assert "RIP: 0010:vmbus_chan_sched" in out
    assert "RSP:" not in out
    assert "Code:" not in out
    assert "sysrq" not in out


def test_single_sysrq_panic_marked_expected(make_openqa: Any, sample_serial0_panic: str) -> None:
    out = "\n".join(_scan(make_openqa, sample_serial0_panic))
    assert "expected: crash triggered via sysrq" in out
    assert "*** KERNEL PANIC DETECTED ***" in out


@pytest.mark.parametrize(
    "line",
    [
        "dracut: FATAL: FIPS integrity test failed",
        "dracut: Refusing to continue",
        "Synchronous Exception at 0xFD528000",
        "You are in emergency mode.",
        "Oops: 0000 [#1] SMP",
    ],
)
def test_detects_other_fatal_patterns(make_openqa: Any, line: str) -> None:
    report = _scan(make_openqa, f"a\nb\n{line}\nc\n")
    assert "*** FATAL BOOT ERROR DETECTED ***" in report
    assert f">>> {line}" in report


def test_dialog_signal_prefers_title() -> None:
    assert logs.dialog_signal(["text: Body", "title: Warning"]) == "DIALOG: Warning"
    assert logs.dialog_signal(["text: Body"]) == "DIALOG: Body"


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["", "# comment", "label: Body"], "DIALOG: Body"),
        (["plain text"], "DIALOG: plain text"),
        (["", "# only comments"], None),
        ([], None),
    ],
)
def test_dialog_signal_skips_blank_and_comment_lines(lines: list[str], expected: str | None) -> None:
    assert logs.dialog_signal(lines) == expected


def test_last_test_died() -> None:
    log = "x\nTest died: first\nTest died: \x1b[0msecond\n"
    assert logs.last_test_died(log) == ("Test died: second", 2)
    assert logs.last_test_died("nothing") is None


def test_extract_job_log_excerpt_picks_signal_with_trailing_lines() -> None:
    log = "ok\n[error] boom\ntrace1\ntrace2\ntrace3\nfine"
    assert logs.extract_job_log_excerpt(log) == ["[error] boom", "trace1", "trace2"]


def test_extract_job_log_excerpt_ignores_needle_hint_and_boilerplate() -> None:
    log = "[warn] make your needles more specific\necho tok; bash -oe /tmp/x.sh died"
    assert not logs.extract_job_log_excerpt(log)


def _job_report(make_openqa: Any, job: dict[str, str], log: str = "") -> list[str]:
    return logs.job_level_failure(make_openqa({"/tests/1/file/autoinst-log.txt": log}), "1", job)


def test_job_level_failure_reports_reason_and_log(make_openqa: Any) -> None:
    out = _job_report(
        make_openqa,
        {"result": "incomplete", "state": "done", "reason": "died: INTERNAL-USE-ONLY-cafe-beef"},
        "[error] boom",
    )
    assert out == [
        "=" * 80,
        "Failed Module: Job",
        "=" * 80,
        "",
        "Job result: incomplete (state done)",
        "Reason: died: INTERNAL-USE-ONLY-****-****",
        "",
        "--- autoinst-log.txt (relevant lines) ---",
        "[error] boom",
        "--- End of Log Section ---",
        "",
    ]


def test_job_level_failure_falls_back_to_log_tail(make_openqa: Any) -> None:
    out = _job_report(make_openqa, {"result": "incomplete", "reason": "quit"}, "a\n\nb\n")
    assert out[-4:] == ["a", "b", "--- End of Log Section ---", ""]


def test_job_level_failure_nothing_to_report(make_openqa: Any) -> None:
    assert not _job_report(make_openqa, {"result": "passed"})
    assert not _job_report(make_openqa, {"result": "incomplete"}, " \n")
    assert not _job_report(make_openqa, {})


def test_job_level_failure_without_reason_shows_log_tail(make_openqa: Any) -> None:
    out = _job_report(make_openqa, {"result": "incomplete"}, "all fine")
    assert out[-3:] == ["all fine", "--- End of Log Section ---", ""]
    assert "Job result: incomplete (state unknown)" in out


def test_kdump_oops_symbol_skips_expected_events_and_symbolless_oops() -> None:
    gap = [f"filler {i}" for i in range(40)]
    crash = ["sysrq: Trigger a crash", "Kernel panic - not syncing: sysrq"]
    oops = ["BUG: kernel NULL pointer dereference", "RIP: 0010:my_func", "Kernel panic - not syncing: Fatal"]
    assert logs.kdump_oops_symbol("\n".join([*crash, *gap, *oops, *gap, *crash])) == "my_func"
    assert logs.kdump_oops_symbol("\n".join([*crash, *gap, "BUG: no symbol here"])) is None


def test_job_level_failure_reason_without_log(make_openqa: Any) -> None:
    out = _job_report(make_openqa, {"result": "incomplete", "reason": "quit"})
    assert out[-2:] == ["Reason: quit", ""]


@pytest.mark.parametrize("lines", [0, -3])
def test_get_serial_tail_non_positive_lines_returns_nothing(lines: int) -> None:
    assert logs.get_serial_tail("a\nb\nc", lines=lines) == []


def test_find_log_range_max_lines_one_keeps_no_tail() -> None:
    log = "S\n1\n2\n3\nE"
    assert logs.find_log_range(log, "S", "E", max_lines=1) == ["[...4 lines omitted to fit context limit...]"]
