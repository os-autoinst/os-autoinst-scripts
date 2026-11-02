# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.logs."""

from __future__ import annotations

import time
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


_UNANSWERED_SUDO = (
    "# Command: sudo zypper -n in vim\n# wait_serial expected: qr/Ab1-\\d+-/\n# Result:\n"
    "sudo zypper -n in vim; echo Ab1-$?-\n[sudo] password for bernhard: \n"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_UNANSWERED_SUDO, "command waited for a sudo password"),
        ("# Result:\nsudo -i\n[sudo: authenticate] Password: \n", "command waited for a sudo password"),
        ("curl: (6) Could not resolve host: download.opensuse.org\n", "name resolution failed (download.opensuse.org)"),
        (
            "ssh: Could not resolve hostname server-node00: Name or service not known",
            "name resolution failed (server-node00)",
        ),
        ("Could not resolve host name mirror.example.org", "name resolution failed (mirror.example.org)"),
        (
            (
                'Error: Get "https://registry.example.org/v2/": dial tcp: lookup registry.example.org on 10.0.2.3:53: '
                "no such host"
            ),
            "name resolution failed (registry.example.org)",
        ),
        ("dial tcp: lookup quay.io: no such host", "name resolution failed (quay.io)"),
        ("ping: example.com: Temporary failure in name resolution", "name resolution failed"),
        (
            "mount.nfs: an incorrect mount option was specified for /mnt",
            "mount failed (mount.nfs: an incorrect mount option was specified)",
        ),
        ("# Result:\n\rmount.nfs4: Connection timed out\n\r", "mount failed (mount.nfs4: Connection timed out)"),
        (
            "mount: /opt/scratch: wrong fs type, bad option, bad superblock on /dev/loop1, missing codepage or helper",
            "mount failed (wrong fs type, bad option, bad superblock)",
        ),
        (
            "mount: /mnt: fsconfig system call failed: btrfs: Unknown parameter 'foo'.",
            "mount failed (fsconfig system call failed: btrfs: Unknown parameter 'foo'.)",
        ),
    ],
)
def test_failing_step_rule(text: str, expected: str) -> None:
    assert logs.summarize_key_error("", ["# Test died: boom", text]) == expected


@pytest.mark.parametrize(
    "text",
    [
        "# Result:\nsudo id -un\n[sudo] password for bernhard: \nroot\n\n# Exit code: 0\n",
        "# Result:\nsudo -k true\n[sudo] password for bernhard: \n\n# Exit code: 1\n",
        "[  253.247214] Key type dns_resolver registered\nsystemd-resolved.service: Deactivated successfully.\n",
        "mount.nfs: timeout set for Sat Oct 25 05:41:04 2026\n",
        "curl: (6) Could not resolve host address 'mirror.example.org'\n",
        "Could not resolve host Could not resolve host\n",
        "mount: /mnt: WARNING: source write-protected, mounted read-only.\n",
        "[  OK  ] Unmounted /home.\n# mount -t nfs server:/export /mnt\n",
    ],
)
def test_failing_step_rules_skip_near_misses(text: str) -> None:
    assert logs.summarize_key_error("", [text]) is None


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["Could not resolve host: example.com", _UNANSWERED_SUDO], "command waited for a sudo password"),
        (
            ["mount.nfs: access denied by server while mounting s:/x", "Name or service not known"],
            "name resolution failed",
        ),
    ],
)
def test_failing_step_rule_order(texts: list[str], expected: str) -> None:
    assert logs.summarize_key_error("", texts) == expected


def test_failing_step_rules_on_a_line_of_repeated_prompts() -> None:
    assert logs.summarize_key_error("", ["[sudo] password for " * (256 * 1024 // 20)]) is None


_STALE_AVC = 'audit: type=1400 avc:  denied  { read } for  pid=412 comm="plymouthd" name="fb0"'
_DNS = ["curl: (6) Could not resolve host: download.opensuse.org"]


@pytest.mark.parametrize(
    ("signals", "failing", "expected"),
    [
        (_STALE_AVC, _DNS, "name resolution failed (download.opensuse.org)"),
        ("cloud-init[1]: util.py[ERROR]: boom", _DNS, "name resolution failed (download.opensuse.org)"),
        ("x\nDIALOG: Warning", _DNS, "name resolution failed (download.opensuse.org)"),
        ("Permission to access 'https://updates.suse.com/x' denied.", _DNS, "repo auth failed (updates.suse.com)"),
        (f"{_STALE_AVC}\nYou are in emergency mode.", _DNS, "boot reached emergency mode"),
        (f"{_STALE_AVC}\nYou are in emergency mode.", [], "SELinux denial"),
        (f"{_STALE_AVC}\nYou are in emergency mode.", ["all fine"], "SELinux denial"),
    ],
)
def test_failing_step_verdict_beats_only_generic_key_errors(signals: str, failing: list[str], expected: str) -> None:
    assert logs.summarize_key_error(signals, failing) == expected


_PERMISSIVE_AVC = (
    "type=AVC msg=audit(10/30/2026 02:08:17.656:147) : avc:  denied  { lock } for  pid=3833 comm=flock"
    " path=/run/sdbootutil/lock scontext=system_u:system_r:sdbootutil_t:s0"
    " tcontext=system_u:object_r:var_run_t:s0 tclass=file permissive=1 "
)
_ENFORCING_AVC = (
    "type=AVC msg=audit(10/30/2026 02:08:36.918:194) : avc:  denied  { search } for  pid=5532 comm=ovs-vsctl"
    " name=4230 scontext=system_u:system_r:openvswitch_t:s0 tcontext=system_u:system_r:wicked_t:s0"
    " tclass=dir permissive=0 "
)
_RAW_AVC = (
    'type=AVC msg=audit(1793337432.45:191): avc:  denied  { setattr } for  pid=2075 comm="chmod" name="sdbootutil"'
    ' dev="tmpfs" ino=2217 scontext=system_u:system_r:sdbootutil_t:s0 tcontext=system_u:object_r:var_run_t:s0'
    " tclass=dir permissive=1"
)
_KERNEL_AVC = _RAW_AVC.replace("type=AVC msg=", "audit: type=1400 ")
_SEALERT_STEP = (
    "validate_script_output\nScript:\nsealert -l '*'\n\nOutput:\n"
    "SELinux is preventing chmod from setattr access on the directory sdbootutil.\n\n"
    "Enforcing Mode                Enforcing\n\nRaw Audit Messages\n"
    f"{_RAW_AVC}\n\n\nHash: chmod,sdbootutil_t,var_run_t,dir,setattr\n"
)


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (f"AVC\n{_PERMISSIVE_AVC}", None),
        (f"{_PERMISSIVE_AVC}\nYou are in emergency mode.", "boot reached emergency mode"),
        (f"{_PERMISSIVE_AVC}\n{_ENFORCING_AVC}", "SELinux denial"),
        (f"{_PERMISSIVE_AVC}{_ENFORCING_AVC}", "SELinux denial"),
        (f"{_ENFORCING_AVC}{_PERMISSIVE_AVC}", "SELinux denial"),
        (f"{_ENFORCING_AVC.replace(' permissive=0 ', ' ')}{_PERMISSIVE_AVC}", "SELinux denial"),
        (f"{_KERNEL_AVC}[   12.4] dracut: FATAL: FIPS integrity test failed", "initrd FIPS integrity failure"),
        (
            f"You are in emergency mode. After logging in, type [   12.3] {_KERNEL_AVC}",
            "boot reached emergency mode",
        ),
        (
            (
                "SELinux access check scon=system_u:system_r:init_t:s0 tcon=system_u:object_r:systemd_unit_file_t:s0"
                " tclass=service perm=start state=permissive function=method_start_unit"
                " path=/usr/lib/systemd/system/foo.service cmdline=n/a: Success"
            ),
            None,
        ),
    ],
)
def test_permissive_selinux_denial_is_no_key_error(signals: str, expected: str | None) -> None:
    assert logs.summarize_key_error(signals) == expected


def test_permissive_selinux_denials_on_a_long_line_of_records() -> None:
    start = time.perf_counter()
    assert logs.summarize_key_error("avc:  denied  { x } for " * 5000) == "SELinux denial"
    assert time.perf_counter() - start < 2


def test_permissive_selinux_denial_is_the_key_error_of_an_selinux_check() -> None:
    assert logs.summarize_key_error(_PERMISSIVE_AVC, selinux_check=True) == "SELinux denial"


@pytest.mark.parametrize(
    "failing",
    [
        [_SEALERT_STEP, "Failed\n# Test died: output not validating"],
        [f"AVC denial\n{_RAW_AVC}", "Failed\n# Test died: AVC denials detected at slem_basic.pm line 41."],
        [f"AVC (unpermitted)\n{_RAW_AVC}"],
    ],
    ids=["sealert", "slem_basic", "AVC_FAIL_ON_DENIALS"],
)
def test_permissive_selinux_denial_is_the_key_error_of_a_failing_step_showing_it(failing: list[str]) -> None:
    assert logs.summarize_key_error("\n".join(failing), failing) == "SELinux denial"


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


_SERIAL = (
    "# _OANM=1; echo hi; echo AAAA-$?-\nhi\nAAAA-0-\n"
    "# _OANM=1; zypper in x; echo e9sH4-$?-\n{out}\ne9sH4-4-\n"
    "# _OANM=1; next; echo BBBB-$?-\nnext out\nBBBB-0-\n"
)


def test_command_output_ends_at_the_exit_marker() -> None:
    serial = _SERIAL.format(out="l1\nl2\nl3")
    assert logs.command_output(serial, "zypper in x", 50) == ["l1", "l2", "l3"]


def test_command_output_keeps_last_lines_with_note() -> None:
    serial = _SERIAL.format(out="\n".join(f"l{i}" for i in range(1, 8)))
    assert logs.command_output(serial, "zypper in x", 3) == ["... (4 earlier output lines omitted)", "l5", "l6", "l7"]


def test_command_output_without_marker_stops_at_next_command() -> None:
    serial = "# _OANM=1; sleep 9; echo CCCC-$?-\npartial\n# _OANM=1; next; echo DDDD-$?-\nnext out\n"
    assert logs.command_output(serial, "sleep 9", 50) == ["partial"]


def test_command_output_missing_command_or_output_is_empty() -> None:
    assert logs.command_output(_SERIAL.format(out="x"), "nope", 50) == []
    assert logs.command_output(_SERIAL.format(out=""), "zypper in x", 50) == []


def test_command_output_uses_the_last_run_of_a_command() -> None:
    serial = "# _OANM=1; ls; echo AAAA-$?-\nold\nAAAA-0-\n# _OANM=1; ls; echo BBBB-$?-\nnew\nBBBB-0-\n"
    assert logs.command_output(serial, "ls", 50) == ["new"]
