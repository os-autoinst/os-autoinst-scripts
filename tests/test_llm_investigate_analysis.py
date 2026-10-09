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
    assert out.startswith("OpenQA Job Analysis: https://srv/t42\n")
    assert "Fetching test details from https://srv/tests/42/details_ajax..." in out
    assert "Found 3 failed module(s)" in out
    assert "Failed Module: installation/install_pkgs" in out


def test_sections_for_each_failure_kind(make_openqa: Any, sample_details_failed: Any) -> None:
    out = _analyze(make_openqa, sample_details_failed)
    assert "Failed Command #1:" in out
    assert "*** TEST DIED ERROR ***" in out
    assert "*** STEP FAILURE ***" in out
    assert "# Cmd[1]: zypper install foo" in out


def test_module_path_from_stack_trace(make_openqa: Any, sample_details_failed: Any) -> None:
    assert "Failed Module: transactional/trup_install" in _analyze(make_openqa, sample_details_failed)


def test_log_fetch_failure_continues(make_openqa: Any, sample_details_failed: Any) -> None:
    assert "Found 3 failed module(s)" in _analyze(make_openqa, sample_details_failed)


def test_softfailed_modules_are_included_on_request(make_openqa: Any, sample_details_softfailed: Any) -> None:
    out = _analyze(make_openqa, sample_details_softfailed, include_softfailed=True)
    assert "Found 2 failed/softfailed module(s)" in out
    assert "warn_step" in out
    assert "hard_failure" in out


def test_command_not_in_serial_log_shows_text_data(make_openqa: Any, sample_details_failed: Any) -> None:
    out = _analyze(make_openqa, sample_details_failed, {"serial_terminal.txt": "unrelated\n"})
    assert "Text data from test details:" in out


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
    assert excinfo.value.partial_output.startswith("OpenQA Job Analysis: https://srv/t42\n")
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
    assert excinfo.value.partial_output.startswith("OpenQA Job Analysis: https://srv/t42\n")


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
        [_step(0, "ok", title="Post-fail"), _step(1, text="# Command: zypper patch\n# Exit code: 1")],
        category=category,
    )
    header = f"Failed Module: {category}/m\n" + "=" * 80 + "\n\n"
    assert header + "# Cmd[1]: zypper patch (exit 1)" in out
    assert out.count("# Cmd[1]:") == 1


def test_repeated_failures_are_shown_once(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_step(i, title="Unknown issue", text=f"msg at {i}") for i in range(1, 31)])
    assert out.count("*** STEP FAILURE ***") == 1
    assert "repeats 29 more times" in out
    assert "Steps: 30 failed / 0 softfailed" in out
    assert "steps omitted" in out


def test_step_context_skips_housekeeping(make_openqa: Any) -> None:
    details = [
        _step(1, "ok", text="# Command: zypper patch\n# Exit code: 0"),
        _step(2, "ok", text="# Command: scp a b\n# Exit code: 0"),
        _step(3, title="Failed", text="# Test died: boom"),
    ]
    out = _module_report(make_openqa, details)
    assert "command: zypper patch" in out
    assert "command: scp" not in out


def test_screenshot_failure_is_skipped_when_test_died_in_cluster(make_openqa: Any) -> None:
    out = _module_report(
        make_openqa, [_step(1, title="", screenshot="s.png"), _step(2, title="Failed", text="# Test died: boom")]
    )
    assert "STEP FAILURE" not in out
    assert "TEST DIED ERROR" in out


def test_post_fail_steps_are_one_line(make_openqa: Any) -> None:
    details = [
        _step(1, title="", text="# Test died: real cause\n"),
        _step(2, "unk", title="Post-fail", text="Entering post fail hook"),
        _step(3, text="# wait_serial expected: qr/login:/\n# Result:\nfoo"),
        _step(4, title="Failed (post)", text="# Post fail hook died: no login\n\tstack frame"),
    ]
    out = _module_report(make_openqa, details)
    assert "real cause" in out
    assert "Secondary failure (post-fail hook) #3 wait_serial" in out
    assert "Secondary failure (post-fail hook) #4 Failed (post): no login" in out
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
    assert out.count("*** STEP FAILURE ***") == 1
    assert "Failure in step #55" in out


def test_distinct_failures_are_all_shown(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_fail(10), _fail(30, "Other", "boom"), _fail(50)])
    assert out.count("*** STEP FAILURE ***") == 2
    assert "retried 1 more times" in out


def test_cluster_cap(make_openqa: Any) -> None:
    out = _module_report(make_openqa, [_fail(n * 20, f"T{n}", f"msg{n}") for n in range(1, 8)])
    assert "... (2 more clusters omitted)" in out


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
            "job": {"test": "patch_job", "result": "failed", "state": "done", "settings": {"DISTRI": "sle"}}
        }),
    }
    out = analysis.analyze_job(make_openqa(routes), "42")
    assert "Test: patch_job\nResult: failed (state done)\nSettings: DISTRI=sle\n" in out
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
    assert "Failure in step #1: T\n\n--- Serial Terminal Log (last 100 lines) ---" in out
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
