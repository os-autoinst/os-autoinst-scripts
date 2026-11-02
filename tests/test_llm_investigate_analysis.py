# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.analysis."""

from __future__ import annotations

import json
from typing import Any

import pytest

from openqa_llm_investigate import analysis
from openqa_llm_investigate.http import AnalyzerError, NoFailedModulesError

_IPA_SERIAL = (
    "Running test /usr/share/lib/img_proof/tests/SLES/test_sles_motd.py\n"
    "../../tests/SLES/test_sles_motd.py::test_sles_motd[paramiko://1.2.3.4] FAILED\n"
    "E           assert False\n"
    "Running test /usr/share/lib/img_proof/tests/SLES/test_sles_repos.py\n"
)
_IMG_PROOF_SERIAL = "FAILED tests=24|pass=18|skip=2|fail=4|error=0\nSCRIPT_FINISHED8vRal-1-\nTerraform teardown noise\n"


def _step(
    num: int, result: str = "fail", title: str = "wait_serial", text: str | None = None, **extra: Any
) -> dict[str, Any]:
    step: dict[str, Any] = {"num": num, "result": result, "display_title": title, **extra}
    if text is not None:
        step["text_data"] = text
    return step


def _downloads(*names: str) -> str:
    return "".join(f'<a href="/tests/42/file/{name}">' for name in names)


def _module(details: list[dict[str, Any]], name: str = "m", category: str | None = "c") -> dict[str, Any]:
    return {"name": name, "category": category, "result": "failed", "details": details}


def _analyze(make_openqa: Any, details: Any, files: dict[str, str] | None = None, **kwargs: Any) -> str:
    routes = {f"/tests/42/file/{name}": text for name, text in (files or {}).items()}
    routes["/api/v1/jobs/42"] = '{"job": {"result": "passed"}}'
    routes["/tests/42/details_ajax"] = json.dumps(details)
    return analysis.analyze_job(make_openqa(routes), "42", **kwargs)


def _module_report(
    make_openqa: Any, details: list[dict[str, Any]], files: dict[str, str] | None = None, **module: Any
) -> str:
    return _analyze(make_openqa, {"modules": [_module(details, **module)]}, files)


def test_header_and_failed_module_count(make_openqa: Any, sample_details_failed: Any) -> None:
    out = _analyze(make_openqa, sample_details_failed)
    assert out.startswith("OpenQA Failed Job Analysis:\n\n")
    assert "Fetching test details" not in out
    assert "Found 3 failed module(s)" in out
    assert "Failed Module: installation/install_pkgs" in out


def test_sections_for_each_failure_kind(make_openqa: Any, sample_details_failed: Any) -> None:
    out = _analyze(make_openqa, sample_details_failed)
    for gone in ("Failed Command #", "TEST DIED ERROR", "STEP FAILURE", "Failing command output"):
        assert gone not in out
    assert 'The module has been failed by: "zypper install foo" command' in out
    assert "# Cmd[" not in out


def test_module_path_from_stack_trace(make_openqa: Any, sample_details_failed: Any) -> None:
    assert "Failed Module: transactional/trup_install" in _analyze(make_openqa, sample_details_failed)


def test_log_fetch_failure_continues(make_openqa: Any, sample_details_failed: Any) -> None:
    assert "Found 3 failed module(s)" in _analyze(make_openqa, sample_details_failed)


def test_softfailed_modules_are_included_on_request(make_openqa: Any, sample_details_softfailed: Any) -> None:
    out = _analyze(make_openqa, sample_details_softfailed, include_softfailed=True)
    assert "Found 2 failed/softfailed module(s)" in out
    assert "warn_step" in out
    assert "hard_failure" in out


def test_command_not_in_serial_log_has_no_serial_section(make_openqa: Any, sample_details_failed: Any) -> None:
    out = _analyze(make_openqa, sample_details_failed, {"serial_terminal.txt": "unrelated\n"})
    assert "lines before/after" not in out


def test_serial_context_around_failed_command(make_openqa: Any, sample_details_failed: Any) -> None:
    out = _analyze(make_openqa, sample_details_failed, {"serial_terminal.txt": "a\n# zypper install foo\nb\n"})
    assert "--- Serial Terminal Log (100 lines before/after) ---" in out
    assert ">>> # zypper install foo" in out


def test_context_lines_option(make_openqa: Any, sample_details_failed: Any) -> None:
    serial = (
        "\n".join(f"line {i}" for i in range(50))
        + "\n# zypper install foo\n"
        + "\n".join(f"after {i}" for i in range(50))
    )
    out = _analyze(make_openqa, sample_details_failed, {"serial_terminal.txt": serial}, context_lines=2)
    assert "(2 lines before/after)" in out
    assert "line 48" in out
    assert "line 47" not in out


@pytest.mark.parametrize(
    ("soft", "msg"), [(False, "No failed modules found"), (True, "No failed or softfailed modules")]
)
def test_no_failed_modules_raises(make_openqa: Any, msg: str, *, soft: bool) -> None:
    with pytest.raises(NoFailedModulesError, match=msg) as excinfo:
        _analyze(make_openqa, {"modules": []}, include_softfailed=soft)
    label = "Failed/Softfailed" if soft else "Failed"
    assert excinfo.value.partial_output.startswith(f"OpenQA {label} Job Analysis:\n")
    assert msg in excinfo.value.partial_output


def test_job_without_modules_reports_job_level_failure(make_openqa: Any) -> None:
    oqa = make_openqa({
        "/tests/42/details_ajax": '{"modules": []}',
        "/api/v1/jobs/42": json.dumps({"job": {"result": "incomplete", "state": "done", "reason": "died: boom"}}),
        "/tests/42/file/autoinst-log.txt": "[error] boom\n",
    })
    out = analysis.analyze_job(oqa, "42")
    assert "Failed Module: Job" in out
    assert "Job result: incomplete (state done)" in out
    assert "Reason: died: boom" in out


def test_details_fetch_failure_keeps_header(make_openqa: Any) -> None:
    with pytest.raises(AnalyzerError, match="Error fetching") as excinfo:
        analysis.analyze_job(make_openqa({}), "42")
    assert excinfo.value.partial_output.startswith("OpenQA Failed Job Analysis:\n")


def test_job_level_failure_uses_supplied_job(make_openqa: Any) -> None:
    oqa = make_openqa({"/tests/42/details_ajax": '{"modules": []}'})
    out = analysis.analyze_job(oqa, "42", job={"result": "incomplete", "reason": "worker lost"})
    assert "Failed Module: Job" in out
    assert "Reason: worker lost" in out


def test_missing_job_metadata_does_not_refetch(make_openqa: Any, mocker: Any) -> None:
    oqa = make_openqa({"/tests/42/details_ajax": '{"modules": []}'})
    fetch = mocker.spy(oqa, "json")
    with pytest.raises(NoFailedModulesError):
        analysis.analyze_job(oqa, "42")
    assert [c.args[0] for c in fetch.call_args_list].count("/api/v1/jobs/42") == 1


@pytest.mark.parametrize("category", ["c", "IPA", "xfstests"])
def test_commands_follow_module_header(make_openqa: Any, category: str) -> None:
    out = _module_report(
        make_openqa,
        [_step(1, text="# Command: zypper patch\n# Exit code: 1"), _step(2, "ok", title="Post-fail")],
        category=category,
    )
    header = f"Failed Module: {category}/m\n" + "=" * 80 + "\n\nPrevious modules: none (first module)\n\n"
    assert header + 'The module has been failed by: "zypper patch" command (exit 1).\n' in out
    assert out.count("been failed by") == 1


def test_repeated_failures_are_shown_once(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_step(i, title="Unknown issue", text=f"msg at {i}") for i in range(1, 31)])
    assert out.count("[FAIL]") == 8
    assert "repeats 29 more times" in out
    assert "Steps:" not in out
    assert "steps omitted" in out


def test_step_context_skips_script_transfer_only(make_openqa: Any) -> None:
    details = [
        _step(1, "ok", text="# Command: zypper patch\n# Exit code: 0"),
        _step(2, "ok", text="# Command: curl -f -v http://h:1/tok/current_script > /tmp/scriptX.sh\n# Exit code: 0"),
        _step(3, "ok", text="# Command: scp a b\n# Exit code: 0"),
        _step(4, "ok", text="# Command: rm -rf /tmp/x\n# Exit code: 0"),
        _step(5, title="Failed", text="# Test died: boom"),
    ]
    out = _module_report(make_openqa, details)
    assert "command: zypper patch" in out
    assert "current_script" not in out
    assert "command: scp a b" in out
    assert "command: rm -rf /tmp/x" in out


def _cmd(num: int, cmd: str, output: str = "") -> dict[str, Any]:
    text = f"# Command: {cmd}\n# Result:\n{cmd[:20]}\n\n{output}\n\n# Exit code: 0\n"
    return _step(num, "ok", text=text)


def _context(out: str) -> list[str]:
    lines = out.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Step context"))
    return lines[start : lines.index("", start)]


def test_step_context_window_is_shared_and_header_is_fixed(make_openqa: Any) -> None:
    needle = {"num": 30, "result": "ok", "needle": "n-1", "area": [{"similarity": 100}], "tags": ["t"]}
    details = [_cmd(n, f"cmd{n}") for n in range(1, 30)] + [needle, _step(31, title="Failed", text="# Test died: x")]
    ctx = _context(_module_report(make_openqa, details))
    assert ctx[0] == "Step context (last 20 command/needle/record_info steps)"
    assert "  #30  screen match  needle: n-1 (100%)  tags: t" in ctx
    assert "  #11  wait_serial  command: cmd11  exit code: 0" in ctx
    assert not any(ln.startswith("  #10 ") for ln in ctx)


def test_step_context_output_tail_for_newest_five_commands_only(make_openqa: Any) -> None:
    details = [_cmd(n, f"cmd{n}", "\n".join(f"out{n}-{i}" for i in range(7))) for n in range(1, 8)]
    ctx = _context(_module_report(make_openqa, [*details, _step(8, title="Failed", text="# Test died: x")]))
    text = "\n".join(ctx)
    assert "out7-6" in text
    assert "    ... (2 earlier output lines omitted)" in ctx
    assert "out3-6" in text
    assert "out2-6" not in text
    assert "command: cmd2  exit code: 0" in text


def test_step_context_record_info_tiers(make_openqa: Any) -> None:
    body = "\n".join(f"b{i}" for i in range(40))
    infos = [_step(n, "ok", title=f"info{n}", text=body) for n in range(1, 8)]
    ctx = _context(_module_report(make_openqa, [*infos, _step(8, title="Failed", text="# Test died: x")]))
    text = "\n".join(ctx)
    assert text.count("    b0\n") == 7
    assert "    b29\n    ... (10 more lines)" in text
    assert "    b1\n    ... (38 more lines)" in text
    assert text.count("... (10 more lines)") == 5
    assert text.count("... (38 more lines)") == 2


def test_step_context_after_step_record_info_is_full(make_openqa: Any) -> None:
    body = "\n".join(f"b{i}" for i in range(10))
    details = [_step(1, title="Failed", text="# Test died: x"), _step(2, "ok", title="late info", text=body)]
    text = "\n".join(_context(_module_report(make_openqa, details)))
    assert "    b9" in text


def test_step_context_failed_record_info_is_not_listed_twice(make_openqa: Any) -> None:
    details = [_step(1, title="Stall", text="stalled\nmore"), _step(2, title="Failed", text="# Test died: x")]
    ctx = _context(_module_report(make_openqa, details))
    assert not any(ln.startswith("    stalled") for ln in ctx)


def test_previous_modules_line(make_openqa: Any) -> None:
    mods = [{"name": n, "category": "c", "result": r, "details": []} for n, r in (("a", "passed"), ("b", "softfailed"))]
    mods += [{"name": "p", "category": "c", "result": "passed", "details": []}] * 2
    mods.append(_module([_step(1, title="Failed", text="# Test died: x")], name="f"))
    out = _analyze(make_openqa, {"modules": mods})
    assert "Previous modules: c/b (softfailed), c/p (passed), c/p (passed)\n" in out


_DIED_ZYPPER = "# Test died: 'zypper in x' failed with code 4\n\nRelated zypper logs:\n"


def test_died_command_shows_its_output_instead_of_the_serial_tail(make_openqa: Any) -> None:
    serial = "# _OANM=1; zypper in x; echo e9sH4-$?-\nProblem: conflict\ne9sH4-4-\nlater noise\n"
    out = _module_report(make_openqa, [_step(1, title="Failed", text=_DIED_ZYPPER)], {"serial_terminal.txt": serial})
    assert "  #1 [FAIL]  Failed  # Test died: 'zypper in x' failed with code 4\n" in out
    assert "    Related zypper logs:\n    Problem: conflict\n" in out
    assert "later noise" not in out
    assert "Serial Terminal Log" not in out


def test_died_command_missing_from_the_serial_log_shows_nothing_else(make_openqa: Any) -> None:
    out = _module_report(
        make_openqa, [_step(1, title="Failed", text=_DIED_ZYPPER)], {"serial_terminal.txt": "unrelated\n"}
    )
    assert "Serial Terminal Log" not in out


def test_died_without_command_keeps_the_serial_tail(make_openqa: Any) -> None:
    out = _module_report(
        make_openqa, [_step(1, title="Failed", text="# Test died: boom")], {"serial_terminal.txt": "tail line\n"}
    )
    assert "--- Serial Terminal Log (last 100 lines) ---" in out


def test_failed_wait_serial_shows_the_last_output_lines(make_openqa: Any) -> None:
    text = "# wait_serial expected: qr/login/\n# Result:\nqr/login/\n\n" + "\n".join(f"o{i}" for i in range(60))
    out = _module_report(make_openqa, [_step(1, text=text)], {"serial_terminal.txt": "# qr/login/\nctx\n"})
    assert "  #1 [FAIL]  wait_serial  marker: qr/login/\n    ... (10 earlier output lines omitted)\n    o10\n" in out
    assert "    o59\n\n" in out
    assert "lines before/after" not in out


def test_failed_wait_serial_without_output_keeps_the_serial_context(make_openqa: Any) -> None:
    text = "# wait_serial expected: qr/login/\n# Result:\n\n"
    out = _module_report(make_openqa, [_step(1, text=text)], {"serial_terminal.txt": "a\n# qr/login/\nb\n"})
    assert "Serial Terminal Log (100 lines before/after)" in out


def test_step_context_after_window_runs_to_the_third_command(make_openqa: Any) -> None:
    marker = _step(3, "ok", text="# wait_serial expected: qr/x/\n# Result:\nm\n\nmarker out")
    after = [
        _step(2, "unk", title="Post-fail", text="hook"),
        marker,
        *(_cmd(n, f"post{n}", f"out{n}") for n in range(4, 9)),
    ]
    details = [_step(1, title="Failed", text="# Test died: x"), *after]
    ctx = "\n".join(_context(_module_report(make_openqa, details)))
    assert "command: post6  exit code: 0\n    out6" in ctx
    assert "post7" not in ctx
    assert "marker out" not in ctx
    assert "#3  wait_serial  marker: qr/x/" in ctx


def test_step_context_after_window_with_fewer_than_three_commands(make_openqa: Any) -> None:
    details = [_step(1, title="Failed", text="# Test died: x"), _cmd(2, "only", "o")]
    ctx = "\n".join(_context(_module_report(make_openqa, details)))
    assert "command: only  exit code: 0\n    o" in ctx


_MARK = "  >>> The next step is the one that failed the test <<<"


def test_failure_marker_precedes_the_first_failing_step(make_openqa: Any) -> None:
    details = [_cmd(1, "ok-cmd"), _step(2, title="Failed", text="# Test died: boom"), _cmd(3, "after")]
    ctx = _context(_module_report(make_openqa, details))
    assert ctx.index(_MARK) + 1 == next(i for i, ln in enumerate(ctx) if ln.startswith("  #2 [FAIL]"))
    assert ctx.count(_MARK) == 1


def test_failure_marker_precedes_the_died_step_of_a_failed_command(make_openqa: Any) -> None:
    details = [_cmd(1, "zypper in x"), _step(2, title="Failed", text="# Test died: 'zypper in x' failed with code 4")]
    ctx = _context(_module_report(make_openqa, details))
    assert ctx[-2] == _MARK
    assert ctx[-1].startswith("  #2 [FAIL]  Failed")


def test_only_the_last_cluster_is_reported(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_fail(10, "A", "boom a"), _fail(50, "B", "boom b")])
    assert out.count(_MARK) == 1
    assert "#50 [FAIL]  B  boom b" in out
    assert "#10 [FAIL, non-fatal]  A" in out
    assert out.count("boom a") == 1


def test_failure_marker_moves_before_preceding_bare_failed_screens(make_openqa: Any) -> None:
    details = [_cmd(1, "x"), _step(2, title="", screenshot="s.png"), _step(3, title="Failed", text="# Test died: boom")]
    ctx = _context(_module_report(make_openqa, details))
    assert ctx.index(_MARK) + 1 == next(i for i, ln in enumerate(ctx) if ln.startswith("  #2 [FAIL]"))


def test_failing_command_walks_back_inside_the_last_cluster_only(make_openqa: Any) -> None:
    details = [
        _step(1, text="# Command: early\n# Result:\n# Exit code: 2"),
        _step(40, text="# wait_serial expected: qr/login:/\n# Result:\nfoo"),
        _step(41, title="Failed", text="# Test died: Could not find login prompt"),
    ]
    out = _module_report(make_openqa, details)
    assert 'failed by: "qr/login:/" wait_serial marker.' in out
    ctx = _context(out)
    assert ctx.index(_MARK) + 1 == next(i for i, ln in enumerate(ctx) if ln.startswith("  #40 [FAIL]"))
    died_only = _module_report(make_openqa, [details[0], details[2]])
    assert "been failed by" not in died_only


def test_failure_marker_with_an_omitted_steps_cluster(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_step(i, title="Unknown issue", text=f"msg at {i}") for i in range(1, 31)])
    assert out.count(_MARK) == 1
    assert "steps omitted" in out


def test_settings_line_dropped_when_only_duplicated_keys_remain(make_openqa: Any, sample_details_failed: Any) -> None:
    routes = {
        "/tests/42/details_ajax": json.dumps(sample_details_failed),
        "/api/v1/jobs/42": json.dumps({"job": {"result": "failed", "settings": {"DISTRI": "sle", "BUILD": "1"}}}),
    }
    assert "Settings:" not in analysis.analyze_job(make_openqa(routes), "42")


def test_screenshot_failure_is_skipped_when_test_died_in_cluster(make_openqa: Any) -> None:
    out = _module_report(
        make_openqa, [_step(1, title="", screenshot="s.png"), _step(2, title="Failed", text="# Test died: boom")]
    )
    assert "#1 [FAIL]  (screenshot: s.png)" in out
    assert "Serial Terminal Log" in out


def test_post_fail_steps_are_labelled_not_reported(make_openqa: Any) -> None:
    details = [
        _step(1, title="Failed", text="# Test died: real cause\n"),
        _step(2, "unk", title="Post-fail", text="Entering post fail hook"),
        _step(3, text="# wait_serial expected: qr/login:/\n# Result:\nfoo"),
        _step(4, title="Failed (post)", text="# Post fail hook died: no login\n\tstack frame"),
    ]
    out = _module_report(make_openqa, details)
    assert "real cause" in out
    assert "#3 [FAIL, post-fail hook]  wait_serial" in out
    assert "Secondary failure" not in out
    assert "Failed Command #3" not in out
    assert "stack frame" not in out


def test_last_test_died_line(make_openqa: Any, sample_details_failed: Any) -> None:
    out = _analyze(make_openqa, sample_details_failed, {"autoinst-log.txt": "Test died: one\nTest died: two\n"})
    assert "Last test died (2 in autoinst-log.txt): Test died: two" in out


def _fail(num: int, title: str = "TFM apply output", text: str = "exit code: 1") -> dict[str, Any]:
    return _step(num, title=title, text=text)


def test_retries_are_announced_once(make_openqa: Any) -> None:
    details = [step for i in range(6) for step in (_step(i * 10 + 1, "ok", "step"), _fail(i * 10 + 5))]
    out = _module_report(make_openqa, details)
    assert "retried 5 more times before this attempt: #5, #15, #25, #35, #45" in out
    assert "  >>> The next step is the one that failed the test <<<\n  #55 [FAIL]" in out


def test_distinct_failures_are_all_shown(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_fail(10), _fail(30, "Other", "boom"), _fail(50)])
    assert "#30 [FAIL, non-fatal]  Other" in out
    assert out.count("boom") == 1
    assert "retried 1 more times" in out


def test_earlier_clusters_are_not_reported(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_fail(n * 20, f"T{n}", f"msg{n}") for n in range(1, 8)])
    assert "more clusters omitted" not in out
    assert "  >>> The next step is the one that failed the test <<<\n  #140 [FAIL]  T7  msg7" in out


def test_boilerplate_is_absent_from_serial_context(make_openqa: Any) -> None:
    text = (
        "# Command: echo tY~XN; bash -oe pipefail /tmp/scripttY~HOST.sh ; echo SCRIPT_FINISHEDtY~XN-$?-\n"
        '# wait_serial expected: "guestregister"\n# Exit code: 3\n'
    )
    serial = "before\necho tY~XN; bash -oe pipefail /tmp/scripttY~HOST.sh\nguestregister failure details here\nafter\n"
    out = _module_report(make_openqa, [_step(1, text=text)], {"serial_terminal.txt": serial})
    assert "echo tY~XN; bash" not in out
    assert "SCRIPT_FINISHED" not in out
    assert "guestregister failure details here" in out


def test_xfstests_module_uses_summary_steps(make_openqa: Any) -> None:
    details = [
        _step(1, "ok", "INFO", "xfs/1 FAILED"),
        _step(2, "ok", "output", "diff here"),
        _step(3, "ok", "dmesg", "no such file does not exist"),
    ]
    out = _module_report(make_openqa, details, {"serial_terminal.txt": "huge log"}, category="xfstests")
    assert "xfstests result:\nxfs/1 FAILED" in out
    assert "--- output ---\ndiff here" in out
    assert "dmesg" not in out
    assert "huge log" not in out


def test_ipa_module_searches_serial_log(make_openqa: Any) -> None:
    details = [_step(1, title=name, text=f"{name}\nfail") for name in ("SLES_test_sles_motd", "SLES_missing")]
    out = _module_report(make_openqa, details, {"serial_terminal.txt": _IPA_SERIAL}, category="IPA")
    assert "--- Serial Terminal Log (IPA test: SLES_test_sles_motd) ---" in out
    assert "E           assert False" in out
    assert "not found in serial log; raw step result: 'SLES_missing\\nfail'" in out


def test_publiccloud_test_died_anchors_on_img_proof_summary(make_openqa: Any) -> None:
    text = "# Test died: img_proof failed at sle/tests/publiccloud/img_proof.pm line 162.\n"
    out = _module_report(
        make_openqa,
        [_step(1, title="Failed", text=text)],
        {"serial_terminal.txt": _IMG_PROOF_SERIAL},
        category="publiccloud",
    )
    assert "--- Serial Terminal Log (img-proof run summary) ---" in out
    assert ">>> FAILED tests=24|pass=18|skip=2|fail=4|error=0" in out


@pytest.mark.parametrize(("serial0", "noted"), [("boot\nlocalhost login: \n", True), ("boot\n", False)])
def test_needle_failure_login_prompt_note(make_openqa: Any, serial0: str, *, noted: bool) -> None:
    text = "# Test died: no candidate needle with tag(s) 'grub2' matched\n"
    files = {"serial0.txt": serial0, "autoinst-log.txt": "x no candidate needle with tag(s) 'grub2' matched\ny\n"}
    out = _module_report(make_openqa, [_step(1, title="Failed", text=text)], files)
    assert "--- autoinst-log.txt (searching for needle failure) ---" in out
    assert ("ends at a login prompt" in out) is noted


def test_kernel_panic_and_key_error(make_openqa: Any, sample_details_failed: Any) -> None:
    out = _analyze(make_openqa, sample_details_failed, {"serial0.txt": "x\nSynchronous Exception at 0xFD52\ny\n"})
    assert "Key error: firmware crash (Synchronous Exception)" in out
    assert "*** FATAL BOOT ERROR DETECTED ***" in out
    assert out.index("Key error:") < out.index("Found 3 failed module(s)") < out.index("FATAL BOOT ERROR DETECTED")


def _died(command: str, how: str = "failed", num: int = 9) -> dict[str, Any]:
    return _step(num, title="Failed", text=f"# Test died: command '{command}' {how} at tests/x.pm line 3.\n")


def _run(num: int, command: str, output: str, code: int) -> dict[str, Any]:
    return _step(
        num, "ok", text=f"# Command: {command}\n# Result:\n{command[:19]}\u2026\n\n{output}\n\n# Exit code: {code}\n"
    )


_CURL = "curl -sf http://download.example.org/x"
_CURL_DNS = "curl: (6) Could not resolve host: download.example.org"
_CURL_404 = "curl: (22) The requested URL returned error: 404"
_POST_FAIL = _step(10, "unk", title="Post-fail", text="Entering post fail hook")
_ZYPPER_REF = "zypper -n ref | cat; ( exit ${PIPESTATUS[0]} )"
_ZYPPER_DNS = "Download (curl) error: Could not resolve host: download.opensuse.org"
_REPO_INVALID = "Repository 'OSS' is invalid."
_SERIAL_RUNS = f"# {_CURL}; echo Ab1-$?-\n{_CURL_404}\nAb1-22-\n# {_CURL}; echo Cd2-$?-\n{_CURL_DNS}\nCd2-6-\n"
_PING = "ping: server-node01: Name or service not known"


def test_sudo_prompt_ending_the_failing_step_is_the_key_error(make_openqa: Any) -> None:
    waited = "# wait_serial expected: qr/Ab1-\\d+-/\n# Result:\nsudo zypper -n in vim\n[sudo] password for x: \n"
    out = _module_report(make_openqa, [_step(8, text=waited), _died("sudo zypper -n in vim", "timed out")])
    assert "Key error: command waited for a sudo password" in out


@pytest.mark.parametrize(
    ("files", "steps", "expected"),
    [
        ({}, [_run(8, _CURL, _CURL_DNS, 6)], "name resolution failed (download.example.org)"),
        (
            {},
            [_run(8, f"{_CURL}; echo SCRIPT_FINISHEDab1-$?-", _CURL_DNS, 6)],
            "name resolution failed (download.example.org)",
        ),
        (
            {"serial0.txt": 'audit: avc:  denied  { read } for  pid=412 comm="plymouthd"\n'},
            [_run(8, _CURL, _CURL_DNS, 6)],
            "name resolution failed (download.example.org)",
        ),
        (
            {},
            [
                _run(7, "zypper -n in vim", "Permission to access 'https://updates.suse.com/x' denied.", 4),
                _run(8, _CURL, _CURL_DNS, 6),
            ],
            "repo auth failed (updates.suse.com)",
        ),
    ],
    ids=["died command run", "command with echo suffix", "stale AVC denial", "repo auth"],
)
def test_failing_step_key_error(
    make_openqa: Any, files: dict[str, str], steps: list[dict[str, Any]], expected: str
) -> None:
    assert f"Key error: {expected}\n" in _module_report(make_openqa, [*steps, _died(_CURL)], files)


@pytest.mark.parametrize(
    ("first", "last", "key_errors"),
    [
        (_REPO_INVALID, _ZYPPER_DNS, ["Key error: name resolution failed (download.opensuse.org)"]),
        (_ZYPPER_DNS, _REPO_INVALID, []),
    ],
    ids=["last retry", "earlier retry"],
)
def test_failing_step_rules_read_the_last_retry_of_the_died_command(
    make_openqa: Any, first: str, last: str, key_errors: list[str]
) -> None:
    grep = 'grep "Error code.*502" /var/log/zypper.log'
    steps = [
        _run(1, _ZYPPER_REF, first, 4),
        _run(2, grep, "", 1),
        _run(4, _ZYPPER_REF, last, 4),
        _run(5, grep, "", 1),
        _step(8, title="Failed", text="# Test died: 'zypper -n ref' failed with code 4\n\nRelated zypper logs:\n"),
    ]
    out = _module_report(make_openqa, steps)
    assert [ln for ln in out.splitlines() if ln.startswith("Key error:")] == key_errors


@pytest.mark.parametrize(
    ("steps", "files", "category"),
    [
        (
            [
                _step(
                    8, text="# wait_serial expected: qr/^root$/m\n# Result:\nsudo id -un\n[sudo] password for x: \nx\n"
                ),
                _step(9, title="Failed", text="# Test died: output not validating\n"),
            ],
            {},
            "c",
        ),
        ([_run(6, _CURL, _CURL_DNS, 6), _run(7, _CURL, "", 0), _died("grep -q foo /etc/bar")], {}, "c"),
        (
            [
                _run(
                    8,
                    "mount -o bogus /dev/vdb /mnt",
                    "mount: /mnt: wrong fs type, bad option, bad superblock on /dev/vdb",
                    32,
                ),
                _died("false"),
            ],
            {},
            "c",
        ),
        (
            [_run(8, _CURL, _CURL_404, 22), _died(_CURL), _POST_FAIL, _run(11, _CURL, _CURL_DNS, 6)],
            {"serial_terminal.txt": _SERIAL_RUNS},
            "c",
        ),
        (
            [
                _run(8, "ping -c1 server-node01", _PING, 2),
                _died("ping -c1 server"),
            ],
            {"serial_terminal.txt": f"# ping -c1 server-node01; echo Ab1-$?-\n{_PING}\nAb1-2-\n"},
            "c",
        ),
        ([_run(8, "info", _CURL_DNS, 6), _died("query 'SELECT * FROM info()'")], {}, "c"),
        ([_run(8, _CURL, _CURL_DNS, 6), _died(_CURL)], {}, "publiccloud"),
        (
            [
                _run(8, _CURL, _CURL_DNS, 6),
                _step(9, title="Failed", text="# Test died: no candidate needle with tag(s) 'ok' matched\n"),
            ],
            {"autoinst-log.txt": "x no candidate needle with tag(s) 'ok' matched\n"},
            "c",
        ),
    ],
    ids=[
        "answered sudo prompt",
        "passed name resolution retry",
        "mount probe",
        "post-fail rerun",
        "longer command",
        "short parsed command",
        "publiccloud",
        "needle",
    ],
)
def test_failing_step_rules_skip_other_steps_and_runs(
    make_openqa: Any, steps: list[dict[str, Any]], files: dict[str, str], category: str
) -> None:
    assert "Key error:" not in _module_report(make_openqa, steps, files, category=category)


_AVC_STEP = _step(1, result="ok", title="AVC", text="avc:  denied  { lock } for  comm=flock tclass=file permissive=1")
_SEALERT_STEPS = [
    _step(
        16,
        title="validate_script_output",
        text=(
            "Script:\nsealert -l '*'\n\nOutput:\n"
            "SELinux is preventing chmod from setattr access on the directory sdbootutil.\n\nRaw Audit Messages\n"
            'type=AVC msg=audit(1793337432.45:191): avc:  denied  { setattr } for  pid=2075 comm="chmod"'
            ' name="sdbootutil" scontext=system_u:system_r:sdbootutil_t:s0 tcontext=system_u:object_r:var_run_t:s0'
            " tclass=dir permissive=1\n"
        ),
    ),
    _step(17, title="Failed", text="# Test died: output not validating\n"),
]


@pytest.mark.parametrize(
    ("modules", "key_errors"),
    [
        ([_module([_AVC_STEP], name="before_test")], []),
        ([_module([_AVC_STEP], name="check_selinux_fails")], ["Key error: SELinux denial"]),
        ([_module([_AVC_STEP], name="before_test"), {**_module([], name="selinux_setup"), "result": "softfailed"}], []),
        ([_module(_SEALERT_STEPS, name="setroubleshootd")], ["Key error: SELinux denial"]),
    ],
    ids=["other", "selinux check", "softfailed selinux module", "failing step shows a denial"],
)
def test_permissive_selinux_denial_is_the_key_error_only_of_an_selinux_check(
    make_openqa: Any, modules: list[dict[str, Any]], key_errors: list[str]
) -> None:
    out = _analyze(make_openqa, {"modules": modules}, include_softfailed=True)
    assert [ln for ln in out.splitlines() if ln.startswith("Key error:")] == key_errors


def test_uploaded_widgets_and_terraform_output(make_openqa: Any) -> None:
    routes = {
        "/tests/42/details_ajax": json.dumps({"modules": [_module([_step(1, text="boom")], category="c")]}),
        "/tests/42/downloads_ajax": _downloads("m-widgets.json", "m-tf_apply_output"),
        "/tests/42/file/m-widgets.json": '{"title": "Warning", "text": "Missing /boot/efi"}',
        "/tests/42/file/m-tf_apply_output": "noise\n╷\n│ Error: Machine type 'c3' does not exist in zone z\n╵\n",
    }
    out = analysis.analyze_job(make_openqa(routes), "42")
    assert "Key error: cloud capacity or machine type unavailable" in out
    assert "--- uploaded log: m-widgets.json ---\ntitle: Warning\ntext: Missing /boot/efi" in out
    assert "--- uploaded log: m-tf_apply_output ---\nError: Machine type 'c3' does not exist in zone z" in out
    assert "noise" not in out


def test_widget_dialog_key_error(make_openqa: Any) -> None:
    routes = {
        "/tests/42/details_ajax": json.dumps({"modules": [_module([], name="m")]}),
        "/tests/42/downloads_ajax": _downloads("m-widgets.json"),
        "/tests/42/file/m-widgets.json": '{"title": "Warning"}',
    }
    assert 'Key error: unexpected dialog: "Warning"' in analysis.analyze_job(make_openqa(routes), "42")


def test_console_warnings_and_cloudregister(make_openqa: Any, sample_details_failed: Any) -> None:
    routes = {
        "/tests/42/details_ajax": json.dumps(sample_details_failed),
        "/tests/42/downloads_ajax": _downloads("destroy-console-end.txt", "cloudregister.txt"),
        "/tests/42/file/destroy-console-end.txt": "[FAILED] Failed to start Foo Service.\nGPT: Use GNU Parted\n",
        "/tests/42/file/cloudregister.txt": "ok\n2026: x: ERROR: register failed\n",
    }
    out = analysis.analyze_job(make_openqa(routes), "42")
    assert "--- console warnings: destroy-console-end.txt ---\n[FAILED] Failed to start Foo Service." in out
    assert "GPT: Use GNU Parted" not in out
    assert "--- uploaded log: cloudregister.txt ---" in out


def test_history_precedes_log_sections(make_openqa: Any, sample_details_failed: Any) -> None:
    routes = {
        "/tests/42/details_ajax": json.dumps(sample_details_failed),
        "/tests/42/ajax": json.dumps({"data": [{"id": 41, "result": "passed", "state": "done"}]}),
    }
    out = analysis.analyze_job(make_openqa(routes), "42")
    assert out.index("Previous/next runs: previous 1 run(s): 1 passed.") < out.index("Found 3 failed module(s)")


def test_job_header_lines(make_openqa: Any, sample_details_failed: Any) -> None:
    routes = {
        "/tests/42/details_ajax": json.dumps(sample_details_failed),
        "/api/v1/jobs/42": json.dumps({
            "job": {
                "test": "patch_job",
                "result": "failed",
                "state": "done",
                "settings": {"DISTRI": "sle", "MACHINE": "64bit", "BACKEND": "qemu"},
            }
        }),
    }
    out = analysis.analyze_job(make_openqa(routes), "42")
    assert "Test: patch_job\nResult: failed (state done)\nSettings: MACHINE=64bit BACKEND=qemu\n" in out
    assert "Modules: 3 failed / 0 softfailed / 0 skipped" in out


def test_module_cap(make_openqa: Any) -> None:
    modules = [_module([_step(1, title="T", text=f"boom{i}")], name=f"m{i}") for i in range(5)]
    out = _analyze(make_openqa, {"modules": modules})
    assert "Found 5 failed module(s)" in out
    assert out.count("Failed Module:") == 3
    assert "... 2 more failed modules not analyzed: m3, m4" in out


def test_module_path_from_any_distri(make_openqa: Any) -> None:
    text = "# Test died: boom at /var/lib/openqa/opensuse/tests/console/zypper_ref.pm line 3.\n"
    out = _module_report(make_openqa, [_step(1, title="Failed", text=text)], name="zypper_ref", category=None)
    assert "Failed Module: console/zypper_ref" in out


def test_unparsable_widgets_are_ignored(make_openqa: Any) -> None:
    routes = {
        "/tests/42/details_ajax": json.dumps({"modules": [_module([], name="m")]}),
        "/tests/42/downloads_ajax": _downloads("m-widgets.json"),
        "/tests/42/file/m-widgets.json": "error: not json",
    }
    out = analysis.analyze_job(make_openqa(routes), "42")
    assert "DIALOG" not in out
    assert "m-widgets.json" not in out


def test_given_job_is_not_fetched(make_openqa: Any, sample_details_failed: Any) -> None:
    routes = {"/tests/42/details_ajax": json.dumps(sample_details_failed)}
    out = analysis.analyze_job(make_openqa(routes), "42", job={"test": "given_job"})
    assert "Test: given_job" in out


def test_output_is_redacted(make_openqa: Any) -> None:
    # the raw IPA step result is only covered by the final redaction pass
    details = [_step(1, title="SLES_missing", text="password=hunter2")]
    out = _module_report(make_openqa, details, {"serial_terminal.txt": "x"}, category="IPA")
    assert "hunter2" not in out
    assert "password=****" in out


def test_module_title_without_category_or_stack_path(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_step(1, title="Failed", text="# Test died: boom")], name="x", category=None)
    assert "Failed Module: x\n" in out


def test_needle_failure_missing_from_autoinst_log(make_openqa: Any) -> None:
    text = "# Test died: no candidate needle with tag(s) 'grub2' matched\n"
    out = _module_report(make_openqa, [_step(1, title="Failed", text=text)], {"autoinst-log.txt": "unrelated\n"})
    assert "searching for needle failure" not in out


def test_step_failure_without_text_shows_serial_tail(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_step(1, title="T")], {"serial_terminal.txt": "last serial line\n"})
    assert "  #1 [FAIL]  T\n\n--- Serial Terminal Log (last 100 lines) ---" in out
    assert "last serial line" in out


def test_kdump_oops_key_error(make_openqa: Any) -> None:
    gap = "\n".join(f"filler {i}" for i in range(40))
    oops = "BUG: oops\nRIP: 0010:my_func\nKernel panic - not syncing: Fatal\n"
    serial0 = f"sysrq: Trigger a crash\nKernel panic - not syncing\n{gap}\n{oops}"
    out = _module_report(make_openqa, [_step(1, title="T", text="boom")], {"serial0.txt": serial0})
    assert "Key error: crash kernel oops in my_func" in out


@pytest.mark.parametrize("title", ["wait_serial", "assert_screen", "ipa_check"])
@pytest.mark.parametrize("category", ["c", None])
def test_null_text_data_does_not_crash(make_openqa: Any, title: str, category: str | None) -> None:
    details = [_step(1, title=title, text_data=None), _step(2, title="shot", text_data=None, screenshot="s.png")]
    out = _module_report(make_openqa, details, category=category)
    assert "Failed Module:" in out


def test_null_text_data_on_died_sibling_step(make_openqa: Any) -> None:
    details = [_step(1, text="# Test died: boom"), _step(2, title="shot", text_data=None, screenshot="s.png")]
    assert "boom" in _module_report(make_openqa, details)


def test_died_text_is_the_step_body_and_the_stack_trace_a_section(make_openqa: Any) -> None:
    text = "# Test died: boom\nmore detail\n--- # stack trace\nfoo() called at tests/x.pm line 1\n"
    out = _module_report(make_openqa, [_step(1, title="Failed", text=text)])
    assert "  #1 [FAIL]  Failed  # Test died: boom\n    more detail\n\n" in out
    assert "--- Stack trace ---\nfoo() called at tests/x.pm line 1\n--- End of Log Section ---" in out
    assert "TEST DIED ERROR" not in out


def test_died_text_is_capped_at_30_lines(make_openqa: Any) -> None:
    text = "# Test died: boom\n" + "\n".join(f"d{i}" for i in range(50))
    out = _module_report(make_openqa, [_step(1, title="Failed", text=text)])
    assert "    d28\n    ... (21 more lines)\n" in out
    assert "d29" not in out


def test_failing_step_in_a_trimmed_cluster_is_kept(make_openqa: Any) -> None:
    details = [_step(i, title="Unknown issue", text=f"msg at {i}") for i in range(1, 31)]
    details[14]["text_data"] = "# Test died: middle\nbody line"
    out = _module_report(make_openqa, details)
    assert "  #15 [FAIL]  Unknown issue  # Test died: middle\n    body line\n" in out
    assert out.count("steps omitted") == 2
