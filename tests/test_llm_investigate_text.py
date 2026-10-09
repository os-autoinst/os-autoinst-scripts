# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.text."""

from __future__ import annotations

from typing import Any

import pytest

from openqa_llm_investigate import text as txt


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "key INTERNAL-USE-ONLY-abcd-1234 here",
            "key INTERNAL-USE-ONLY-****-**** here",
        ),
        (
            "key INTERNAL-USE-ONLY-fba30486a8951760 here",
            "key INTERNAL-USE-ONLY-****-**** here",
        ),
        (
            "key INTERNAL-USE-ONLY-ABCD-1234 here",
            "key INTERNAL-USE-ONLY-****-**** here",
        ),
        (
            "key INTERNAL-USE-ONLY-Abcd-12Ef here",
            "key INTERNAL-USE-ONLY-****-**** here",
        ),
        (
            "first INTERNAL-USE-ONLY-aaaa-bbbb and INTERNAL-USE-ONLY-1111-2222 done",
            "first INTERNAL-USE-ONLY-****-**** and INTERNAL-USE-ONLY-****-**** done",
        ),
        ("no codes here", "no codes here"),
        ("", ""),
    ],
)
def test_redact_masks_valid_codes(text: Any, expected: Any) -> None:
    assert txt.redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "INTERNAL-USE-ONLY-XXXX-YYYY",  # non-hex
        "INTERNAL-USE-ONLY-abc-1234",  # short
        "INTERNAL-USE-ONLY-abcde-1234",  # too long
        "INTERNAL-USE-ONLY-abcd1234",  # missing dash
    ],
)
def test_redact_does_not_mask_malformed(text: Any) -> None:
    assert txt.redact(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GET /repo?credentials=abc123&x=1", "GET /repo?credentials=****&x=1"),
        ("Authorization: Bearer abc.def.ghi", "Authorization: Bearer ****"),
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: Basic ****"),
        ("curl -H 'Bearer abcdefgh1234'", "curl -H 'Bearer ****'"),
        ("https://user:hunter2@example.com/x", "https://****@example.com/x"),
        ("password=hunter2 next", "password=**** next"),
        ("db_password: 'hunter2'", "db_password: '****'"),
        ('"token": "abc"', '"token": "****"'),
        ("API_KEY=abc; ls", "API_KEY=****; ls"),
        ("client_secret = xyz", "client_secret = ****"),
        ("SCC_REGCODE=abc", "SCC_REGCODE=****"),
        ('"SCC_REGCODE_SLES": "abc"', '"SCC_REGCODE_SLES": "****"'),
        ("scc_regcode_ltss='abc'", "scc_regcode_ltss='****'"),
        ("REGCODE=abc123", "REGCODE=****"),
        ("regcode: abc123", "regcode: ****"),
        ("SUSEConnect --regcode=abc123 -p x", "SUSEConnect --regcode=**** -p x"),
        ("registercloudguest --reg-code abc123 --x", "registercloudguest --reg-code **** --x"),
        (
            "AccountName=test;AccountKey=abc+/==;EndpointSuffix=core",
            "AccountName=test;AccountKey=****;EndpointSuffix=core",
        ),
        ("SUSEConnect -r abc -p product", "SUSEConnect -r **** -p product"),
        ("sudo /usr/sbin/registercloudguest --force -r 'abc'", "sudo /usr/sbin/registercloudguest --force -r ****"),
        ('SUSEConnect -r "abc def"', "SUSEConnect -r ****"),
        ("curl -s -u user:pass https://host", "curl -s -u **** https://host"),
        ('curl --user="user:pass word" https://host', "curl --user=**** https://host"),
        ("curl -uuser:pass https://host", "curl -u**** https://host"),
        ("grep -r text .; sort -u file", "grep -r text .; sort -u file"),
        ("SUSEConnect --status; grep -r text .", "SUSEConnect --status; grep -r text ."),
        ("cli --password hunter2 --x", "cli --password **** --x"),
        ("cli --api-key=abc", "cli --api-key=****"),
        ("key AKIAABCDEFGHIJKLMNOP end", "key AKIA**** end"),
        ("u?X-Amz-Signature=deadbeef&X-Amz-Credential=AK%2F", "u?X-Amz-Signature=****&X-Amz-Credential=****"),
        ("blob?sv=2020&sig=abc%2Bdef&se=1", "blob?sv=2020&sig=****&se=1"),
        ("t ghp_" + "a" * 36 + " end", "t gh_**** end"),
        ("jwt eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeK end", "jwt [jwt] end"),
        ("a\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\nb", "a\n[private key]\nb"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl", "[private key]"),
        ("cat: /etc/passwd: No such file", "cat: /etc/passwd: No such file"),
        ("Enter password for root", "Enter password for root"),
    ],
)
def test_redact_masks_secrets(text: str, expected: str) -> None:
    assert txt.redact(text) == expected
    assert txt.redact(expected) == expected


def test_redact_strips_script_finished_suffix() -> None:
    text = "sudo systemctl is-active guestregister; echo SCRIPT_FINISHEDM98FO-$?-"
    assert txt.redact(text) == "sudo systemctl is-active guestregister"


def test_redact_strips_script_finished_suffix_with_spaces() -> None:
    text = "timeout 90 ssh host -- $'cmd' ; echo SCRIPT_FINISHEDM98FO-$?-"
    assert txt.redact(text) == "timeout 90 ssh host -- $'cmd' "


def test_redact_strips_script_finished_different_token() -> None:
    text = "ls /tmp; echo SCRIPT_FINISHEDxyz123-$?-"
    assert txt.redact(text) == "ls /tmp"


def test_redact_strips_script_finished_expanded_exit_code() -> None:
    text = "sudo systemctl is-active guestregister; echo SCRIPT_FINISHEDM98FO-3-"
    assert txt.redact(text) == "sudo systemctl is-active guestregister"


def test_redact_strips_script_finished_expanded_zero() -> None:
    text = "ls /tmp; echo SCRIPT_FINISHEDM98FO-0-"
    assert txt.redact(text) == "ls /tmp"


@pytest.mark.parametrize(
    ("text_data", "expected"),
    [
        ("# Command: ls -la /tmp\nmore stuff", "ls -la /tmp"),
        ("# Command:   spaced  ", "spaced"),
        (
            '# wait_serial expected: "running"\nfoo',
            "running",
        ),
        (
            "# wait_serial expected: 'single'\n",
            "single",
        ),
        (
            '# wait_serial expected: "do_stuff; echo done"\n',
            "do_stuff; echo done",
        ),
        (
            '# wait_serial expected: "do_stuff; echo SCRIPT_FINISHEDabc-3-"\n',
            "do_stuff",
        ),
        (
            '# wait_serial expected: "echo \\$HOME \\\\path"\n',
            "echo $HOME \\path",
        ),
        ("nothing matches here", None),
    ],
)
def test_parse_command_from_text_data_parses(text_data: Any, expected: Any) -> None:
    assert txt.parse_command_from_text_data(text_data) == expected


def test_parse_command_from_text_data_prefers_command_over_expected() -> None:
    text = '# wait_serial expected: "ignored"\n# Command: chosen\n'
    assert txt.parse_command_from_text_data(text) == "chosen"


def test_parse_command_from_text_data_command_strips_echo_suffix() -> None:
    text = "# Command: sudo systemctl is-active guestregister; echo SCRIPT_FINISHEDM98FO-$?-\n"
    assert txt.parse_command_from_text_data(text) == "sudo systemctl is-active guestregister"


def test_parse_command_from_text_data_command_strips_echo_suffix_with_ssh_wrapper() -> None:
    text = (
        '# Command: ssh -E /var/tmp/ssh_sut.log "user@host" -- '
        "$'sudo systemctl is-active guestregister'; echo SCRIPT_FINISHEDM98FO-$?-\n"
    )
    result = txt.parse_command_from_text_data(text)
    assert result is not None
    assert result.endswith("$'sudo systemctl is-active guestregister'")
    assert "echo SCRIPT_FINISHED" not in result


def test_parse_command_from_text_data_command_skips_openqa_boilerplate_falls_through_to_wait_serial() -> None:
    text = (
        "# Command: echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh ; echo SCRIPT_FINISHEDM98FO-$?-\n"
        '# wait_serial expected: "guestregister"\n'
    )
    assert txt.parse_command_from_text_data(text) == "guestregister"


def test_parse_command_from_text_data_command_skips_openqa_boilerplate_returns_none_if_no_fallback() -> None:
    text = "# Command: echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh ; echo SCRIPT_FINISHEDM98FO-$?-\n"
    assert txt.parse_command_from_text_data(text) is None


@pytest.mark.parametrize(
    ("text_data", "expected"),
    [
        # Pattern 1: command 'X' timed out / failed
        ("# Test died: command 'reboot' timed out", "reboot"),
        ("# Test died: command 'systemctl restart' failed", "systemctl restart"),
        # Pattern 2: Waiting for Godot — plain
        (
            "# Test died: Waiting for Godot: do_thing now at /foo/bar.pm line 10",
            "do_thing now",
        ),
        # Pattern 2 — ssh with $'...' inner — full inner command, no truncation
        (
            "# Test died: Waiting for Godot: ssh root@host $'zypper install vim' at /x.pm line 1",
            "zypper install vim",
        ),
        # Pattern 3: returned-with with quoted args — full args, no truncation
        (
            (
                "# Test died: trup_call returned with 1, expected 0\n"
                'transactional::trup_call("pkg install sudo git-core vim curl") at /x.pm line 1'
            ),
            "pkg install sudo git-core vim curl",
        ),
        # Pattern 3 — fallback to bare command name when no quoted args
        (
            "# Test died: do_stuff returned with 2, expected 0",
            "do_stuff",
        ),
        # Pattern 4: no candidate needle with tag(s) '...' matched
        (
            (
                "# Test died: no candidate needle with tag(s) 'agama-product-selection,"
                " agama-configuring-the-product, agama-installing' matched within 400 seconds"
            ),
            (
                "no candidate needle with tag(s) 'agama-product-selection,"
                " agama-configuring-the-product, agama-installing'"
            ),
        ),
        # No match
        ("# Test died: unknown failure mode", None),
        ("", None),
    ],
)
def test_parse_command_from_test_died_parses(text_data: Any, expected: Any) -> None:
    assert txt.parse_command_from_test_died(text_data) == expected


def test_parse_command_from_test_died_pattern1_beats_pattern2() -> None:
    text = "# Test died: command 'first' failed\nWaiting for Godot: ignored stuff at /x.pm line 1"
    assert txt.parse_command_from_test_died(text) == "first"


def test_parse_command_from_test_died_pattern2_beats_pattern3() -> None:
    text = "# Test died: Waiting for Godot: god_cmd arg at /x.pm line 1\nthing returned with 1, expected 0"
    assert txt.parse_command_from_test_died(text) == "god_cmd arg"


def test_parse_command_from_test_died_p2_full_inner_command_not_truncated() -> None:
    """P2 with $'...' returns the full inner command, not just first 2 words."""
    text = "# Test died: Waiting for Godot: ssh root@host $'zypper install vim curl' at /x.pm line 1"
    assert txt.parse_command_from_test_died(text) == "zypper install vim curl"


def test_parse_command_from_test_died_p2_plain_full_command_not_truncated() -> None:
    """P2 plain returns the full command, not just first 2 words."""
    text = "# Test died: Waiting for Godot: do_thing arg1 arg2 arg3 at /x.pm line 1"
    assert txt.parse_command_from_test_died(text) == "do_thing arg1 arg2 arg3"


def test_parse_command_from_test_died_p3_full_args_not_truncated() -> None:
    """P3 returns the full quoted-arg string, not just first 3 words."""
    text = (
        "# Test died: trup_call returned with 1, expected 0\n"
        'transactional::trup_call("pkg install sudo git-core vim curl") at /x.pm line 1'
    )
    assert txt.parse_command_from_test_died(text) == "pkg install sudo git-core vim curl"


def test_parse_command_from_test_died_p3_single_quoted_args() -> None:
    """P3 also handles single-quoted function args."""
    text = "# Test died: do_stuff returned with 1\nmodule::do_stuff('some arg here') at /x.pm line 1"
    assert txt.parse_command_from_test_died(text) == "some arg here"


def test_parse_command_from_test_died_p5_testapi_call_on_died_line() -> None:
    """P5: testapi function call directly on the # Test died: line."""
    text = "# Test died: assert_screen('login_screen') at /path/to/test.pm line 42"
    result = txt.parse_command_from_test_died(text)
    assert result == "assert_screen('login_screen')"


def test_parse_command_from_test_died_p5_testapi_namespaced_call() -> None:
    """P5: fully-qualified testapi:: call is captured."""
    text = "# Test died: testapi::assert_screen('login_screen', timeout => 30) at /path line 1"
    result = txt.parse_command_from_test_died(text)
    assert result is not None
    assert "assert_screen" in result
    assert "login_screen" in result


def test_parse_command_from_test_died_p5_not_triggered_when_p1_matches() -> None:
    """P1 takes priority over P5."""
    text = "# Test died: command 'reboot' failed and assert_screen('x') at /path line 1"
    assert txt.parse_command_from_test_died(text) == "reboot"


def test_parse_command_from_test_died_p5_no_args_returns_func_name() -> None:
    """P5 without args returns just the function name."""
    text = "# Test died: wait_serial() at /path/test.pm line 5"
    result = txt.parse_command_from_test_died(text)
    assert result == "wait_serial"


def test_extract_search_string_from_step_command_line_takes_priority() -> None:
    detail = {"text_data": '# Command: ls -la /tmp\n# wait_serial expected: "ignored"\n'}
    assert txt.extract_search_string_from_step(detail) == "ls -la /tmp"


def test_extract_search_string_from_step_qr_pattern_extracts_prefix() -> None:
    detail = {"text_data": "# wait_serial expected: qr/FtFpg-(\\d+)-/u\n"}
    assert txt.extract_search_string_from_step(detail) == "FtFpg-"


def test_extract_search_string_from_step_quoted_wait_serial_returns_string() -> None:
    detail = {"text_data": '# wait_serial expected: "active (running)"\n'}
    assert txt.extract_search_string_from_step(detail) == "active (running)"


def test_extract_search_string_from_step_test_died_uses_parse_command() -> None:
    detail = {"text_data": "# Test died: command 'reboot' timed out at /x.pm line 1\n"}
    assert txt.extract_search_string_from_step(detail) == "reboot"


def test_extract_search_string_from_step_no_match_returns_none() -> None:
    detail = {"text_data": "plain text with no markers\n"}
    assert txt.extract_search_string_from_step(detail) is None


def test_extract_search_string_from_step_none_detail_returns_none() -> None:
    assert txt.extract_search_string_from_step(None) is None


def test_extract_search_string_from_step_missing_text_data_returns_none() -> None:
    assert txt.extract_search_string_from_step({}) is None


def test_extract_search_string_from_step_empty_text_data_returns_none() -> None:
    assert txt.extract_search_string_from_step({"text_data": ""}) is None


def test_extract_search_string_from_step_boilerplate_command_falls_through_to_wait_serial() -> None:
    """When # Command: is the bash-wrapper boilerplate, fall through to wait_serial token."""
    detail = {
        "text_data": (
            "# Command: echo rrNUA; bash -oe pipefail /tmp/scriptrrNUA.sh;"
            " echo SCRIPT_FINISHEDrrNUA-$?-\n"
            "# wait_serial expected: qr/SCRIPT_FINISHEDrrNUA-(\\d+)-/u\n"
            "# Result:\n\nrrNUA\n"
            "An error occurred (AuthFailure) when calling the DescribeImages operation\n"
            "SCRIPT_FINISHEDrrNUA-254-\n"
            "# Exit code: 254\n"
        )
    }
    # Should return the literal prefix from the qr/ pattern, not the bash wrapper
    assert txt.extract_search_string_from_step(detail) == "SCRIPT_FINISHEDrrNUA-"


def test_extract_search_string_from_step_boilerplate_command_with_no_wait_serial_returns_none() -> None:
    """When # Command: is boilerplate and there is no wait_serial line, return None."""
    detail = {
        "text_data": (
            "# Command: echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh;"
            " echo SCRIPT_FINISHEDM98FO-$?-\n"
            "# Result:\n\nM98FO\nsome output\n"
            "SCRIPT_FINISHEDM98FO-1-\n"
            "# Exit code: 1\n"
        )
    }
    assert txt.extract_search_string_from_step(detail) is None


def test_is_boilerplate_log_line_plain_boilerplate() -> None:
    assert txt.is_boilerplate_log_line("echo tY~XN; bash -oe pipefail /tmp/scripttY~HOST.sh")


def test_is_boilerplate_log_line_boilerplate_with_script_finished_suffix() -> None:
    assert txt.is_boilerplate_log_line(
        "echo tY~XN; bash -oe pipefail /tmp/scripttY~HOST.sh ; echo SCRIPT_FINISHEDtY~XN-$?-"
    )


def test_is_boilerplate_log_line_boilerplate_with_timestamp_prefix() -> None:
    """Leading timestamp must not prevent detection (uses re.search)."""
    assert txt.is_boilerplate_log_line("[2024-01-01T12:00:00] echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh")


def test_is_boilerplate_log_line_tilde_token_caught() -> None:
    r"""Non-word chars in the echo token (e.g. ~) are handled by \\S+."""
    assert txt.is_boilerplate_log_line("echo tY~XN; bash -oe pipefail /tmp/scripttY~HOST.sh")


def test_is_boilerplate_log_line_real_command_not_flagged() -> None:
    assert not txt.is_boilerplate_log_line("zypper install -y guestregister")


def test_is_boilerplate_log_line_empty_line_not_flagged() -> None:
    assert not txt.is_boilerplate_log_line("")


def test_is_boilerplate_log_line_unrelated_echo_not_flagged() -> None:
    assert not txt.is_boilerplate_log_line("echo hello world")


def test_filter_text_data_lines_removes_boilerplate_command_line() -> None:
    text = (
        "# Command: echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh ; echo SCRIPT_FINISHEDM98FO-$?-\n# Exit code: 3\n"
    )
    result = txt.filter_text_data_lines(text)
    assert "echo M98FO" not in result
    assert "# Exit code: 3" in result


def test_filter_text_data_lines_keeps_non_boilerplate_command_line() -> None:
    text = "# Command: sudo systemctl is-active guestregister\n# Exit code: 3\n"
    result = txt.filter_text_data_lines(text)
    assert "sudo systemctl is-active guestregister" in result


def test_filter_text_data_lines_keeps_unrelated_lines_intact() -> None:
    text = "some output\n# Exit code: 0\nmore output\n"
    assert txt.filter_text_data_lines(text) == text


def test_filter_text_data_lines_empty_string_returns_empty() -> None:
    assert not txt.filter_text_data_lines("")


def test_parse_exit_code_from_text_data_extracts_exit_code() -> None:
    text = "# Command: foo\n# Exit code: 0\n"
    assert txt.parse_exit_code_from_text_data(text) == "0"


def test_parse_exit_code_from_text_data_extracts_nonzero_exit_code() -> None:
    text = "# Exit code: 127\n"
    assert txt.parse_exit_code_from_text_data(text) == "127"


def test_parse_exit_code_from_text_data_returns_none_when_absent() -> None:
    assert txt.parse_exit_code_from_text_data("# Command: foo\n") is None


def test_parse_exit_code_from_text_data_returns_none_for_empty() -> None:
    assert txt.parse_exit_code_from_text_data("") is None


def test_parse_exit_code_from_text_data_strips_whitespace() -> None:
    text = "# Exit code:   42   \n"
    assert txt.parse_exit_code_from_text_data(text) == "42"


def test_format_step_summary_line_command_marker_exit_code() -> None:
    detail = {
        "num": 5,
        "result": "ok",
        "display_title": "wait_serial",
        "text_data": (
            "# Command: curl http://host/upload\n# wait_serial expected: qr/FtFpg-(\\d+)-/u\n# Exit code: 0\n"
        ),
    }
    line = txt.format_step_summary_line(detail)
    assert "#5" in line
    assert "[FAIL]" not in line
    assert "command: curl http://host/upload" in line
    assert "marker: qr/FtFpg-(\\d+)-/u" in line
    assert "exit code: 0" in line


def test_format_step_summary_line_fail_tag_when_result_fail() -> None:
    detail = {
        "num": 7,
        "result": "fail",
        "display_title": "wait_serial",
        "text_data": "# Command: foo\n",
    }
    line = txt.format_step_summary_line(detail)
    assert "#7 [FAIL]" in line


def test_format_step_summary_line_no_fail_tag_when_result_ok() -> None:
    detail = {
        "num": 3,
        "result": "ok",
        "display_title": "wait_serial",
        "text_data": "# Command: bar\n",
    }
    line = txt.format_step_summary_line(detail)
    assert "[FAIL]" not in line


def test_format_step_summary_line_no_parseable_fields_shows_first_line() -> None:
    detail = {
        "num": 10,
        "result": "ok",
        "display_title": "CHECK guestregister",
        "text_data": "guestregister check\nsome more info\n",
    }
    line = txt.format_step_summary_line(detail)
    assert "#10" in line
    assert "CHECK guestregister" in line
    assert "guestregister check" in line
    assert "some more info" not in line


def test_format_step_summary_line_screenshot_step() -> None:
    detail = {
        "num": 6,
        "result": "unk",
        "display_title": "",
        "screenshot": "prepare_instance-6.png",
    }
    line = txt.format_step_summary_line(detail)
    assert "#6" in line
    assert "(screenshot: prepare_instance-6.png)" in line


def test_format_step_summary_line_empty_display_title_omitted() -> None:
    detail = {
        "num": 1,
        "result": "ok",
        "display_title": "",
        "text_data": "# Command: ls\n",
    }
    line = txt.format_step_summary_line(detail)
    # No double-space from empty title+separator
    assert "  " not in line.replace("  command:", "MARKER")  # only one double-space gap


def test_format_step_summary_line_first_line_truncated_at_120() -> None:
    long_line = "x" * 200
    detail = {
        "num": 2,
        "result": "ok",
        "display_title": "step",
        "text_data": long_line + "\n",
    }
    line = txt.format_step_summary_line(detail)
    # The first line content in the summary is at most 120 chars
    assert "x" * 121 not in line


def test_format_step_summary_line_marker_only() -> None:
    detail = {
        "num": 8,
        "result": "ok",
        "display_title": "wait_serial",
        "text_data": '# wait_serial expected: "active (running)"\n',
    }
    line = txt.format_step_summary_line(detail)
    assert 'marker: "active (running)"' in line
    assert "command:" not in line
    assert "exit code:" not in line


def test_format_step_summary_line_no_text_data_no_screenshot() -> None:
    detail = {"num": 9, "result": "ok", "display_title": "assert_screen"}
    line = txt.format_step_summary_line(detail)
    assert "#9" in line
    assert "assert_screen" in line


def test_format_step_summary_line_marker_is_raw_not_parsed() -> None:
    """The marker shown is the raw wait_serial expected value, not the qr/ prefix."""
    detail = {
        "num": 3,
        "result": "ok",
        "display_title": "wait_serial",
        "text_data": "# wait_serial expected: qr/XMark-(\\d+)-/u\n",
    }
    line = txt.format_step_summary_line(detail)
    assert "marker: qr/XMark-(\\d+)-/u" in line


def test_format_step_summary_line_command_strips_echo_suffix() -> None:
    detail = {
        "num": 4,
        "result": "fail",
        "display_title": "wait_serial",
        "text_data": "# Command: sudo zypper install vim; echo SCRIPT_FINISHEDM98FO-$?-\n",
    }
    line = txt.format_step_summary_line(detail)
    assert "command: sudo zypper install vim" in line
    assert "echo SCRIPT_FINISHED" not in line


def test_format_step_summary_line_command_skips_openqa_boilerplate_shows_marker_instead() -> None:
    detail = {
        "num": 5,
        "result": "fail",
        "display_title": "wait_serial",
        "text_data": (
            "# Command: echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh ; echo SCRIPT_FINISHEDM98FO-$?-\n"
            '# wait_serial expected: "guestregister"\n'
            "# Exit code: 3\n"
        ),
    }
    line = txt.format_step_summary_line(detail)
    assert "command:" not in line
    assert "echo M98FO" not in line
    assert 'marker: "guestregister"' in line
    assert "exit code: 3" in line


@pytest.mark.parametrize(
    ("text_data", "expected"),
    [
        (
            "# wait_serial expected: qr/FtFpg-(\\d+)-/u\n",
            "FtFpg-",
        ),
        (
            "# wait_serial expected: qr/XMark-(\\d+)-/\n",
            "XMark-",
        ),
        (
            "# wait_serial expected: qr/PREFIX_(\\w+)_SUFFIX/i\n",
            "PREFIX_",
        ),
        # Command: still takes priority over qr/
        (
            "# Command: my-cmd\n# wait_serial expected: qr/FtFpg-(\\d+)-/u\n",
            "my-cmd",
        ),
        # Plain quoted string still works
        (
            '# wait_serial expected: "plain string"\n',
            "plain string",
        ),
    ],
)
def test_parse_command_from_text_data_qr_qr_parses(text_data: Any, expected: Any) -> None:
    assert txt.parse_command_from_text_data(text_data) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # SLES_ prefix stripped, test_ part kept
        ("SLES_test_sles_motd", "test_sles_motd"),
        ("SLES_test_sles_repos", "test_sles_repos"),
        ("SLES_test_sles_smt_reg", "test_sles_smt_reg"),
        ("SLES_test_sles_guestregister", "test_sles_guestregister"),
        # Azure compound prefix stripped
        (
            "SLES_Azure_test_sles_azure_services_test_sles_azure_running_services_waagent",
            "test_sles_azure_services_test_sles_azure_running_services_waagent",
        ),
        # Already starts with test_ — returned as-is
        ("test_soft_reboot", "test_soft_reboot"),
        ("test_hard_reboot", "test_hard_reboot"),
        # No test_ at all — returned as-is
        ("grow_root", "grow_root"),
    ],
)
def test_ipa_search_key_extracts_test_portion(name: Any, expected: Any) -> None:
    assert txt.ipa_search_key(name) == expected


_ZYPP_DIED = (
    "# Test died: zypper patch failed with code: 8\n\n"
    "Related zypper logs:\n"
    "2026-10-07 22:09:29 <5> h(1) [zypp-core] Exception.cc(log):245 MediaCurl.cc(x):932 "
    "THROW:    Login failed. (https://smt.example.net/repo/a.rpm?credentials=Base_Module): 401\n"
    "2026-10-07 22:09:50 <5> h(1) [zypp-core] Exception.cc(log):245 MediaCurl.cc(x):932 "
    "THROW:    Login failed. (https://smt.example.net/repo/b.rpm?credentials=Base_Module): 401\n"
    "2026-10-07 22:10:03 <5> h(1) [zypp-core] Exception.cc(log):245 MediaCurl.cc(y):835 "
    "RETHROW:  Permission to access 'https://smt.example.net/c.rpm' denied.\n"
    "2026-10-07 22:10:03 <5> h(1) [zypp-core] Exception.cc(log):245 History:\n"
    "2026-10-07 22:10:03 <5> h(1) [zypp-core] Exception.cc(log):245  - Can't provide x\n"
    "\tpublic::run(x) called at sle/lib/publiccloud/zypper.pm line 532\n"
    "\tautotest::run_all() called at /usr/lib/os-autoinst/autotest.pm line 433\n"
    "\teval {...} called at /usr/lib/perl5/vendor_perl/Mojo/IOLoop/ReadWriteProcess.pm line 329\n"
    "\tMojo::IOLoop::ReadWriteProcess::start(x) called at /usr/lib/perl5/x.pm line 492\n"
    "\n--- # stack trace\nlib/publiccloud/zypper.pm line 532\n"
)


def test_clean_died_text_collapses_zypper_chain_and_strips_credentials() -> None:
    out = txt.clean_died_text(_ZYPP_DIED)
    assert out.count("Login failed") == 1
    assert "(x2)" in out
    assert "credentials" not in out
    assert "RETHROW" not in out
    assert "History:" not in out
    assert "Can't provide" not in out


def test_clean_died_text_drops_framework_frames_keeps_test_frames() -> None:
    out = txt.clean_died_text(_ZYPP_DIED)
    assert "sle/lib/publiccloud/zypper.pm line 532" in out
    for noise in ("Mojo::IOLoop", "autotest::", "/usr/lib/perl5", "eval {...}"):
        assert noise not in out


def test_clean_died_text_drops_redundant_stack_trace_footer() -> None:
    assert "--- # stack trace" not in txt.clean_died_text(_ZYPP_DIED)


def test_clean_died_text_keeps_stack_trace_footer_with_new_info() -> None:
    text = "# Test died: x\n--- # stack trace\nlib/foo.pm line 650\n"
    assert "lib/foo.pm line 650" in txt.clean_died_text(text)


def test_clean_died_text_strips_escapes_and_event_lines() -> None:
    text = 'a\x1b]3008;x=1\x1b\\b\x1b[=3h\nEVENT {"data":1}\nkeep'
    assert txt.clean_died_text(text) == "ab\nkeep"


def test_clean_died_text_caps_lines() -> None:
    out = txt.clean_died_text("\n".join(f"l{i}" for i in range(100)), 10)
    assert out.splitlines()[-1] == "... (90 more lines)"
    assert len(out.splitlines()) == 11


def test_clean_died_text_collapses_blank_lines() -> None:
    assert txt.clean_died_text("a\n\n\n\nb\n\n") == "a\n\nb"


def _ts(n: Any, rest: Any) -> Any:
    return f"[2026-10-08T03:27:{n:02d}.000000Z] {rest}"


def test_condense_log_collapses_needle_polls_with_best_score() -> None:
    lines = [
        _ts(
            1,
            "[debug] [pid:1] \x1b[37mno match: 9.0s, best candidate: a-1 (0.10)\x1b[0m",
        ),
        _ts(2, "[debug] [pid:1] no match: 8.0s, best candidate: b-2 (0.47)"),
        _ts(3, "[debug] [pid:1] no change: 7.0s"),
        _ts(
            4,
            "[warn] [pid:1] !!! check_asserted_screen took 1.71 seconds for 3 needles",
        ),
        _ts(5, "[info] after"),
    ]
    out = txt.condense_log(lines)
    assert len(out) == 2
    assert "2 no-match polls" in out[0]
    assert "best candidate b-2 (0.47)" in out[0]
    assert "1 unchanged-screen polls" in out[0]
    assert "1 slow-check warnings" in out[0]
    assert out[1].endswith("after")


def test_condense_log_strips_escapes_oa_markers_and_event_lines() -> None:
    out = txt.condense_log(["a\x1b]3008;x\x1b\\b", "OA:START-ab-0-OA:DONE", 'EVENT {"x":1}', "keep"])
    assert out == ["ab", "keep"]


def test_condense_log_autoinst_drops_debug_and_continuations_keeps_signal() -> None:
    lines = [
        _ts(1, "[debug] [pid:1] <<< testapi::wait_serial(timeout=3,"),
        '    "# "',
        "  ], x=1)",
        _ts(2, "[info] ::: serial_screen::read_until: Matched output from SUT"),
        _ts(3, '[debug] [pid:1] <<< testapi::script_run(cmd="ls")'),
        _ts(4, '[debug] [pid:1] <<< testapi::record_info(title="T")'),
        _ts(5, "[warn] real warning"),
    ]
    out = txt.condense_log(lines, autoinst=True)
    assert len(out) == 3
    assert "script_run" in out[0]
    assert "record_info" in out[1]
    assert out[2].endswith("[warn] real warning")
    assert not any("wait_serial" in ln or "Matched" in ln for ln in out)


def test_condense_log_non_autoinst_keeps_debug() -> None:
    assert len(txt.condense_log([_ts(1, "[debug] plain")])) == 1


def test_condense_log_drops_framework_frames() -> None:
    out = txt.condense_log([
        "\tautotest::run_all() called at /usr/lib/os-autoinst/autotest.pm line 1",
        "ok",
    ])
    assert out == ["ok"]


def test_condense_log_max_lines_keeps_tail() -> None:
    out = txt.condense_log([f"l{i}" for i in range(10)], max_lines=3)
    assert out == ["[... 7 earlier lines omitted ...]", "l7", "l8", "l9"]


def _frames(n: Any, indent: Any = "\t") -> Any:
    return [f"{indent}pkg::f{i}() called at x.pm line {i}" for i in range(n)]


def test_trim_stack_frames_long_run_trimmed() -> None:
    out = txt.trim_stack_frames(["Test died: x", *_frames(8), "tail"])
    assert out == [
        "Test died: x",
        *_frames(5),
        "\t... (3 more stack frames)",
        "tail",
    ]


def test_trim_stack_frames_short_run_unchanged() -> None:
    lines = ["Test died: x", *_frames(5)]
    assert txt.trim_stack_frames(lines) == lines


def test_trim_stack_frames_separate_runs_each_trimmed() -> None:
    out = txt.trim_stack_frames([*_frames(7), "--- # stack trace", *_frames(6)])
    assert out.count("\t... (2 more stack frames)") == 1
    assert out.count("\t... (1 more stack frames)") == 1


def test_trim_stack_frames_clean_died_text_trims() -> None:
    text = "# Test died: boom\n" + "\n".join(_frames(9))
    out = txt.clean_died_text(text)
    assert "(4 more stack frames)" in out
    assert out.count("called at") == 5


def test_trim_stack_frames_condense_autoinst_trims() -> None:
    lines = ["[2026-10-08T03:27:00.000000Z] [info] Test died: x", *_frames(8)]
    out = txt.condense_log(lines, autoinst=True)
    assert out[-1].strip() == "... (3 more stack frames)"


def test_condense_log_needle_polls_without_timestamps_keep_best_score() -> None:
    lines = [
        "no match: 1.0s, best candidate: a (0.80)",
        "no match: 1.0s, best candidate: b (0.50)",
    ]
    assert txt.condense_log(lines) == ["[needle search: 2 no-match polls, best candidate a (0.80)]"]


def test_condense_log_max_lines_zero_keeps_only_omission_note() -> None:
    assert txt.condense_log(["a", "b"], max_lines=0) == ["[... 2 earlier lines omitted ...]"]


def test_command_keeps_echo_inside_command() -> None:
    assert txt.parse_command_from_text_data("# Command: do_stuff; echo done\n") == "do_stuff; echo done"
