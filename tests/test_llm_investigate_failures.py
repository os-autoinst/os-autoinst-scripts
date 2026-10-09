# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.failures."""

from __future__ import annotations

import operator
from typing import Any

from openqa_llm_investigate import failures as fl


def test_extract_failed_modules_failed_only_by_default(sample_details_softfailed: Any) -> None:
    result = fl.extract_failed_modules(sample_details_softfailed)
    assert len(result) == 1
    assert result[0]["name"] == "hard_failure"


def test_extract_failed_modules_includes_softfailed_when_requested(sample_details_softfailed: Any) -> None:
    result = fl.extract_failed_modules(sample_details_softfailed, include_softfailed=True)
    names = {m["name"] for m in result}
    assert names == {"warn_step", "hard_failure"}


def test_extract_failed_modules_returns_empty_when_no_failures() -> None:
    result = fl.extract_failed_modules({"modules": []})
    assert result == []


def test_extract_failed_modules_skips_passed_and_none_modules() -> None:
    details = {
        "modules": [
            {"name": "a", "result": "passed"},
            {"name": "b", "result": "none"},
            {"name": "c"},  # missing result
        ]
    }
    assert fl.extract_failed_modules(details) == []


def test_extract_failed_modules_missing_modules_key_returns_empty_list(sample_details_no_modules: Any) -> None:
    """OpenQA omits "modules" for a job whose worker died before any module ran."""
    assert fl.extract_failed_modules(sample_details_no_modules) == []


def test_extract_failed_modules_modules_sorted_by_earliest_failing_step() -> None:
    """Module with the lowest failing step num appears first."""
    details = {
        "modules": [
            {
                "name": "late_fail",
                "result": "failed",
                "details": [{"num": 10, "result": "fail"}],
            },
            {
                "name": "early_fail",
                "result": "failed",
                "details": [{"num": 2, "result": "fail"}],
            },
        ]
    }
    result = fl.extract_failed_modules(details)
    assert result[0]["name"] == "early_fail"
    assert result[1]["name"] == "late_fail"


def test_extract_failed_modules_details_within_module_sorted_by_num() -> None:
    """Steps inside a module are returned in ascending num order."""
    details = {
        "modules": [
            {
                "name": "mod",
                "result": "failed",
                "details": [
                    {"num": 5, "result": "fail"},
                    {"num": 1, "result": "fail"},
                    {"num": 3, "result": "pass"},
                ],
            }
        ]
    }
    result = fl.extract_failed_modules(details)
    nums = [d["num"] for d in result[0]["details"]]
    assert nums == [1, 3, 5]


def test_extract_failed_modules_modules_without_failing_details_sort_last() -> None:
    """Modules with no failing details (num=inf) sort after those with failures."""
    details = {
        "modules": [
            {
                "name": "no_fail_details",
                "result": "failed",
                "details": [{"num": 1, "result": "pass"}],
            },
            {
                "name": "has_fail",
                "result": "failed",
                "details": [{"num": 99, "result": "fail"}],
            },
        ]
    }
    result = fl.extract_failed_modules(details)
    assert result[0]["name"] == "has_fail"
    assert result[1]["name"] == "no_fail_details"


def test_extract_test_died_errors_includes_details_with_marker() -> None:
    module = {
        "details": [
            {"text_data": "ok"},
            {"text_data": "boom\n# Test died: oh no\nmore"},
        ]
    }
    result = fl.extract_test_died_errors(module)
    assert len(result) == 1
    assert "# Test died:" in result[0]["text_data"]


def test_extract_test_died_errors_handles_missing_text_data() -> None:
    module = {"details": [{"foo": "bar"}]}
    assert fl.extract_test_died_errors(module) == []


def test_extract_test_died_errors_returns_empty_when_no_details() -> None:
    assert fl.extract_test_died_errors({}) == []


def test_extract_test_died_errors_returns_empty_when_details_empty() -> None:
    assert fl.extract_test_died_errors({"details": []}) == []


def test_group_failures_by_proximity_single_failure_is_one_cluster() -> None:
    details = [{"num": 5, "result": "fail"}]
    result = fl.group_failures_by_proximity(details)
    assert result == [[{"num": 5, "result": "fail"}]]


def test_group_failures_by_proximity_two_failures_within_gap_merge() -> None:
    details = [
        {"num": 3, "result": "fail"},
        {"num": 7, "result": "fail"},
    ]
    result = fl.group_failures_by_proximity(details, gap=5)
    assert len(result) == 1
    assert len(result[0]) == 2


def test_group_failures_by_proximity_gap_of_exactly_5_merges() -> None:
    details = [
        {"num": 1, "result": "fail"},
        {"num": 6, "result": "fail"},
    ]
    result = fl.group_failures_by_proximity(details, gap=5)
    assert len(result) == 1


def test_group_failures_by_proximity_gap_of_6_splits() -> None:
    details = [
        {"num": 1, "result": "fail"},
        {"num": 7, "result": "fail"},
    ]
    result = fl.group_failures_by_proximity(details, gap=5)
    assert len(result) == 2


def test_group_failures_by_proximity_three_failures_two_clusters() -> None:
    details = [
        {"num": 3, "result": "fail"},
        {"num": 7, "result": "fail"},
        {"num": 15, "result": "fail"},
    ]
    result = fl.group_failures_by_proximity(details, gap=5)
    assert len(result) == 2
    assert result[0] == [{"num": 3, "result": "fail"}, {"num": 7, "result": "fail"}]
    assert result[1] == [{"num": 15, "result": "fail"}]


def test_group_failures_by_proximity_passing_steps_ignored() -> None:
    details = [
        {"num": 1, "result": "ok"},
        {"num": 2, "result": "fail"},
        {"num": 3, "result": "pass"},
        {"num": 9, "result": "fail"},
    ]
    result = fl.group_failures_by_proximity(details, gap=5)
    assert len(result) == 2
    assert result[0][0]["num"] == 2
    assert result[1][0]["num"] == 9


def test_group_failures_by_proximity_empty_details_returns_empty() -> None:
    assert fl.group_failures_by_proximity([]) == []


def test_group_failures_by_proximity_no_failures_returns_empty() -> None:
    details = [
        {"num": 1, "result": "ok"},
        {"num": 2, "result": "pass"},
    ]
    assert fl.group_failures_by_proximity(details) == []


def test_group_failures_by_proximity_all_failures_in_one_cluster() -> None:
    details = [{"num": i, "result": "fail"} for i in range(1, 6)]
    result = fl.group_failures_by_proximity(details, gap=5)
    assert len(result) == 1
    assert len(result[0]) == 5


def _get_context_steps_make_details(nums: Any) -> Any:
    return [{"num": n, "result": "ok"} for n in nums]


def test_get_context_steps_returns_steps_before_and_after() -> None:
    details = _get_context_steps_make_details([1, 2, 3, 4, 5, 6, 7])
    before, after = fl.get_context_steps(details, cluster_min_num=4, cluster_max_num=4)
    assert [d["num"] for d in before] == [1, 2, 3]
    assert [d["num"] for d in after] == [5, 6]


def test_get_context_steps_clips_before_at_start() -> None:
    details = _get_context_steps_make_details([1, 2, 5])
    before, _after = fl.get_context_steps(details, cluster_min_num=2, cluster_max_num=2)
    assert [d["num"] for d in before] == [1]


def test_get_context_steps_clips_after_at_end() -> None:
    details = _get_context_steps_make_details([1, 5, 6])
    _before, after = fl.get_context_steps(details, cluster_min_num=5, cluster_max_num=5)
    assert [d["num"] for d in after] == [6]


def test_get_context_steps_cluster_spanning_multiple_steps() -> None:
    details = _get_context_steps_make_details([1, 2, 3, 4, 5, 6, 7, 8])
    # cluster covers nums 3-5
    before, after = fl.get_context_steps(details, cluster_min_num=3, cluster_max_num=5)
    assert [d["num"] for d in before] == [1, 2]
    assert [d["num"] for d in after] == [6, 7]


def test_get_context_steps_empty_details_returns_empty() -> None:
    before, after = fl.get_context_steps([], cluster_min_num=1, cluster_max_num=1)
    assert before == []
    assert after == []


def test_get_context_steps_cluster_at_start() -> None:
    details = _get_context_steps_make_details([1, 2, 3, 4, 5])
    before, after = fl.get_context_steps(details, cluster_min_num=1, cluster_max_num=1)
    assert before == []
    assert [d["num"] for d in after] == [2, 3]


def test_get_context_steps_cluster_at_end() -> None:
    details = _get_context_steps_make_details([1, 2, 3, 4, 5])
    before, after = fl.get_context_steps(details, cluster_min_num=5, cluster_max_num=5)
    assert [d["num"] for d in before] == [2, 3, 4]
    assert after == []


def test_get_context_steps_includes_passing_steps_in_context() -> None:
    details = [
        {"num": 1, "result": "ok"},
        {"num": 2, "result": "pass"},
        {"num": 3, "result": "fail"},
        {"num": 4, "result": "ok"},
        {"num": 5, "result": "ok"},
    ]
    before, after = fl.get_context_steps(details, cluster_min_num=3, cluster_max_num=3)
    assert len(before) == 2
    assert len(after) == 2


def _extract_xfstests_result_steps_make_details(steps: Any) -> Any:
    """Build a details list from compact (num, title, text_data) tuples."""
    return [{"num": num, "display_title": title, "result": "ok", "text_data": td} for num, title, td in steps]


def test_extract_xfstests_result_steps_returns_known_summary_titles() -> None:
    details = _extract_xfstests_result_steps_make_details([
        (1, "wait_serial", "# Command: foo\n# Exit code: 0\n"),
        (2, "INFO", "name: generic/604\ntest result: FAILED\ntime: 10\n"),
        (3, "output", "FAIL: something went wrong\n"),
        (4, "out.bad", "/opt/log/generic/604.out.bad not exist"),
        (5, "full", "/opt/log/generic/604.full not exist"),
        (6, "dmesg", ""),
    ])
    result = fl.extract_xfstests_result_steps(details)
    titles = [d["display_title"] for d in result]
    assert titles == ["INFO", "output", "out.bad", "full", "dmesg"]


def test_extract_xfstests_result_steps_ignores_wait_serial_and_screenshot_steps() -> None:
    details = _extract_xfstests_result_steps_make_details([
        (1, "wait_serial", "# Command: foo\n"),
        (2, "INFO", "name: g/1\ntest result: FAILED\ntime: 5\n"),
    ])
    # screenshot step has no text_data key — simulate by using None
    details.insert(
        0,
        {
            "num": 0,
            "display_title": "",
            "result": "ok",
            "screenshot": "step-0.png",
            "text_data": None,
        },
    )
    result = fl.extract_xfstests_result_steps(details)
    # screenshot step has text_data=None → excluded; wait_serial not in titles → excluded
    titles = [d["display_title"] for d in result]
    assert titles == ["INFO"]


def test_extract_xfstests_result_steps_returns_empty_when_no_summary_steps() -> None:
    details = _extract_xfstests_result_steps_make_details([
        (1, "wait_serial", "# Command: foo\n"),
        (2, "", "some text"),
    ])
    assert fl.extract_xfstests_result_steps(details) == []


def test_extract_xfstests_result_steps_handles_none_text_data() -> None:
    details = [
        {"num": 1, "display_title": "INFO", "result": "ok", "text_data": None},
        {
            "num": 2,
            "display_title": "output",
            "result": "ok",
            "text_data": "FAIL: x\n",
        },
    ]
    result = fl.extract_xfstests_result_steps(details)
    # INFO excluded (text_data is None), output included
    assert len(result) == 1
    assert result[0]["display_title"] == "output"


def test_extract_xfstests_result_steps_preserves_forward_order() -> None:
    details = _extract_xfstests_result_steps_make_details([
        (5, "dmesg", "kernel log\n"),
        (3, "output", "test output\n"),
        (4, "full", "full log\n"),
        (1, "INFO", "name: g/1\ntest result: FAILED\ntime: 1\n"),
        (2, "out.bad", "bad output\n"),
    ])
    # sort by num first as main() would
    details.sort(key=operator.itemgetter("num"))
    result = fl.extract_xfstests_result_steps(details)
    assert [d["display_title"] for d in result] == [
        "INFO",
        "out.bad",
        "output",
        "full",
        "dmesg",
    ]


def test_build_module_cmd_list_wait_serial_with_exit_code() -> None:
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: zypper install foo\n# Exit code: 3\n",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    assert entries[0] == ("zypper install foo", "3")


def test_build_module_cmd_list_wait_serial_without_exit_code() -> None:
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: systemctl start foo.service\n",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert entries[0] == ("systemctl start foo.service", None)


def test_build_module_cmd_list_boilerplate_skipped_falls_through_to_marker() -> None:
    """Boilerplate echo TOKEN; bash ... /tmp/... commands are excluded."""
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": (
                "# Command: echo Y1dRv; bash -oe pipefail /tmp/host.sh\n"
                '# wait_serial expected: "guestregister"\n'
                "# Exit code: 3\n"
            ),
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    # Falls through to the wait_serial expected marker
    assert entries[0][0] == "guestregister"


def test_build_module_cmd_list_boilerplate_old_pattern_also_skipped() -> None:
    """Old-style echo TOKEN; bash .../tmp/scriptTOKEN.sh also excluded."""
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh\n",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert entries == []


def test_build_module_cmd_list_test_died_command_included() -> None:
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "assert_screen",
            "text_data": "# Test died:\ncommand 'zypper ref' timed out",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    assert "zypper ref" in entries[0][0]
    assert entries[0][1] is None  # no exit code for test_died


def test_build_module_cmd_list_other_failure_uses_display_title() -> None:
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "assert_screen",
            "text_data": "something",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    assert entries[0] == ("assert_screen", None)


def test_build_module_cmd_list_deduplication_by_command() -> None:
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: zypper install foo\n# Exit code: 1\n",
        },
        {
            "num": 2,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: zypper install foo\n# Exit code: 1\n",
        },
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1


def test_build_module_cmd_list_non_failing_steps_ignored() -> None:
    details = [
        {
            "num": 1,
            "result": "ok",
            "display_title": "wait_serial",
            "text_data": "# Command: ok cmd\n",
        },
        {
            "num": 2,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: fail cmd\n",
        },
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    assert "fail cmd" in entries[0][0]


def test_build_module_cmd_list_empty_details() -> None:
    assert fl.build_module_cmd_list([]) == []


def test_build_module_cmd_list_boilerplate_tilde_token_skipped() -> None:
    r"""Boilerplate with non-word token (tY~XN) is filtered by broadened \\S+ regex."""
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": (
                "# Command: echo tY~XN; bash -oe pipefail /tmp/scripttY~HOST.sh\n"
                '# wait_serial expected: "guestregister"\n'
                "# Exit code: 3\n"
            ),
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    assert entries[0][0] == "guestregister"
    assert "echo tY" not in entries[0][0]


def test_build_module_cmd_list_boilerplate_only_with_exit_code_emits_synthetic() -> None:
    """When command is boilerplate-only and no wait_serial expected, exit code used."""
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": ("# Command: echo M98FO; bash -oe pipefail /tmp/scriptM98FO.sh\n# Exit code: 254\n"),
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    cmd, exit_code = entries[0]
    assert cmd == "(exit 254)"
    assert exit_code == "254"


def test_build_module_cmd_list_assert_screen_needle_tag_extracted() -> None:
    """assert_screen with needle tags in text_data yields assert_screen('tags') token."""
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "assert_screen",
            "text_data": "Needle 'login_screen, login_screen-dark' not found within 90 seconds",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    cmd, exit_code = entries[0]
    assert cmd == "assert_screen('login_screen, login_screen-dark')"
    assert exit_code is None


def test_build_module_cmd_list_assert_screen_no_candidate_needle_extracted() -> None:
    """assert_screen with 'no candidate needle with tag(s)' form is extracted."""
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "assert_screen",
            "text_data": "no candidate needle with tag(s) 'agama-product' matched within 30s",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    assert entries[0][0] == "assert_screen('agama-product')"


def test_build_module_cmd_list_assert_screen_no_tags_falls_back_to_display_title() -> None:
    """assert_screen with no tag info in text_data falls back to the title."""
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "assert_screen",
            "text_data": "some unrelated message",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert len(entries) == 1
    assert entries[0][0] == "assert_screen"


def test_build_module_cmd_list_check_screen_needle_tag_extracted() -> None:
    """check_screen also participates in needle-tag extraction."""
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "check_screen",
            "text_data": "Needle 'some_needle' not found",
        }
    ]
    entries = fl.build_module_cmd_list(details)
    assert entries[0][0] == "check_screen('some_needle')"


def test_module_cmd_lines_emits_cmd_lines() -> None:
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: zypper install foo\n# Exit code: 3\n",
        }
    ]
    lines = fl.module_cmd_lines(details)
    out = "\n".join(lines)
    assert "# Cmd[1]: zypper install foo (exit 3)" in out


def test_module_cmd_lines_emits_multiple_cmd_lines() -> None:
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: cmd-a\n# Exit code: 1\n",
        },
        {
            "num": 2,
            "result": "fail",
            "display_title": "wait_serial",
            "text_data": "# Command: cmd-b\n# Exit code: 2\n",
        },
    ]
    lines = fl.module_cmd_lines(details)
    out = "\n".join(lines)
    assert "# Cmd[1]: cmd-a (exit 1)" in out
    assert "# Cmd[2]: cmd-b (exit 2)" in out


def test_module_cmd_lines_no_exit_code_omitted_from_suffix() -> None:
    details = [
        {
            "num": 1,
            "result": "fail",
            "display_title": "assert_screen",
            "text_data": "something",
        }
    ]
    lines = fl.module_cmd_lines(details)
    out = "\n".join(lines)
    assert "# Cmd[1]: assert_screen" in out
    assert "(exit" not in out


def test_module_cmd_lines_empty_details_emits_nothing() -> None:
    lines = fl.module_cmd_lines([])
    assert not lines


def _repeated_failures_step(num: Any, text: Any, title: Any = "Unknown issue") -> Any:
    return {"num": num, "result": "fail", "display_title": title, "text_data": text}


def test_repeated_failures_groups_by_normalised_text() -> None:
    cluster = [
        _repeated_failures_step(1, "Oct 08 04:30:53.6 host login[1044]: denied"),
        _repeated_failures_step(2, "Jan 20 03:15:20.3 host login[743]: denied"),
        _repeated_failures_step(3, "other message"),
        _repeated_failures_step(4, "Oct 09 01:00:00.1 host login[5]: denied"),
    ]
    groups = fl.group_repeated_failures(cluster)
    assert [(g[0]["num"], g[1]) for g in groups] == [(1, [2, 4]), (3, [])]


def test_repeated_failures_repeat_note_truncates() -> None:
    note = fl.repeat_note(list(range(10, 20)))
    assert "repeats 10 more times: #10, #11, #12, #13, #14, ... (+5)" in note


def test_noise_steps_housekeeping_and_bare_screenshots_are_noise() -> None:
    scp = {"num": 1, "result": "ok", "text_data": "# Command: scp a b\n# Result:"}
    curl = {
        "num": 2,
        "result": "ok",
        "text_data": "# Command: curl --form upload=@x http://h/uploadlog/z",
    }
    shot = {"num": 3, "result": "ok", "screenshot": "x.png"}
    for step in (scp, curl, shot):
        assert fl.is_noise_step(step)


def test_noise_steps_failing_or_real_steps_are_not_noise() -> None:
    assert not fl.is_noise_step({"result": "fail", "text_data": "# Command: scp a b"})
    assert not fl.is_noise_step({"result": "ok", "text_data": "# Command: zypper in x"})
    assert not fl.is_noise_step({"result": "ok", "text_data": "plain", "display_title": "t"})


def test_secondary_failures_repeated_secondary_counted() -> None:
    step = {"num": 4, "display_title": "x", "text_data": "# Post fail hook died: a"}
    assert fl.secondary_line(step, [5]).endswith("a (x2)")


@staticmethod
def _collapse_clusters_fail(num: Any, title: Any = "TFM apply output", text: Any = "exit code: 1") -> Any:
    return {"num": num, "result": "fail", "display_title": title, "text_data": text}


def test_collapse_clusters_collapse_keeps_last_with_retry_nums() -> None:
    cl = [[_collapse_clusters_fail(n)] for n in (10, 20, 30)] + [[_collapse_clusters_fail(40, "Other", "x")]]
    res = fl.collapse_repeated_clusters(cl)
    assert [(c[0]["num"], r) for c, r in res] == [(30, [10, 20]), (40, [])]


def test_cmd_repeat_comment_repeat_comment_after_entry() -> None:
    d = [{"num": n, "result": "fail", "display_title": "TFM apply output"} for n in (3, 13, 23)]
    lines = fl.module_cmd_lines(d)
    out = lines
    assert out == [
        "# Cmd[1]: TFM apply output",
        "# Cmd[1] repeated 3 times: #3, #13, #23",
    ]


def test_cmd_repeat_comment_single_has_no_comment() -> None:
    lines = fl.module_cmd_lines([{"num": 3, "result": "fail", "display_title": "T"}])
    assert lines == ["# Cmd[1]: T"]


def test_extract_failed_modules_keeps_modules_without_details() -> None:
    assert fl.extract_failed_modules({"modules": [{"name": "m", "result": "failed"}]}) == [
        {"name": "m", "result": "failed"}
    ]


def test_xfstests_lines_without_summary_steps() -> None:
    assert fl.xfstests_lines([{"num": 1, "result": "ok", "display_title": "other"}]) == []


def test_extract_test_died_errors_handles_none_text_data() -> None:
    died = {"num": 2, "text_data": "# Test died: x"}
    assert fl.extract_test_died_errors({"details": [{"num": 1, "text_data": None}, died]}) == [died]
