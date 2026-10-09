# Copyright SUSE LLC
"""Build the failure analysis text of an openQA job from its details and logs."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .failures import (
    CLUSTER_MAX,
    CLUSTER_STEPS_SHOWN,
    STEP_GROUP_MAX,
    collapse_repeated_clusters,
    extract_failed_modules,
    extract_test_died_errors,
    get_context_steps,
    group_failures_by_proximity,
    group_repeated_failures,
    is_noise_step,
    is_secondary,
    module_cmd_lines,
    module_key,
    post_fail_start,
    repeat_note,
    secondary_line,
    xfstests_lines,
)
from .history import fetch_job, job_header, run_history
from .http import AnalyzerError, NoFailedModulesError
from .logs import (
    dialog_signal,
    find_in_log,
    find_log_range,
    format_log_context,
    get_serial_tail,
    job_level_failure,
    kdump_oops_symbol,
    last_test_died,
    log_lines,
    matched_line,
    scan_kernel_crash,
    summarize_key_error,
)
from .text import (
    clean_died_text,
    condense_log,
    extract_search_string_from_step,
    filter_text_data_lines,
    format_step_summary_line,
    ipa_search_key,
    parse_command_from_test_died,
    parse_command_from_text_data,
    parse_exit_code_from_text_data,
    redact,
)
from .uploads import (
    collect_job_level_logs,
    collect_uploaded_logs,
    format_uploaded_logs,
    list_job_files,
    select_console_files,
)

if TYPE_CHECKING:
    from .http import JsonDict, OpenQA

_RULE = "=" * 80
_STEPS_BEFORE_SCAN = 20
_STEPS_AFTER_SCAN = 10
_STEPS_BEFORE_SHOWN = 5
_STEPS_AFTER_SHOWN = 3
_RETRIES_SHOWN = 5
_PUBLICCLOUD_CONTEXT_AFTER = 5
_LOGIN_PROMPT_TAIL_LINES = 10
_MODULES_MAX = 3
_NEEDLE_RE = re.compile(r"no candidate needle with tag\(s\) '[^']+'")
_LOGIN_PROMPT_RE = re.compile(r"login:\s*$")


@dataclass
class _Job:
    """A job's logs and the settings the report sections need."""

    serial: str
    autoinst: str
    context_lines: int
    booted_to_login: bool = False


def _section(title: str, body: list[str]) -> list[str]:
    return [f"--- {title} ---", *body, "--- End of Log Section ---", ""]


def _masked(lines: list[str]) -> list[str]:
    return [redact(ln) for ln in lines]


def _module_title(module: JsonDict) -> str:
    """Return `path/name`: the path from a stack trace for category-less modules, else the category."""
    name, category = module.get("name", "unknown"), module.get("category")
    if category is None:
        for detail in extract_test_died_errors(module):
            if m := re.search(rf"\btests/([^/\s]+)/{re.escape(name)}\.pm", detail.get("text_data") or ""):
                return f"{m.group(1)}/{name}"
    return f"{category}/{name}" if category else name


def _ipa_report(job: _Job, all_details: list[JsonDict], name: str) -> list[str]:
    """Search the serial log for the pytest output of each failed img_proof test case."""
    out: list[str] = []
    for detail in (d for d in all_details if d.get("result") == "fail"):
        step_name = detail.get("display_title", name)
        key = ipa_search_key(step_name)
        context = find_in_log(job.serial, key, job.context_lines, job.context_lines)
        body = (
            format_log_context(context)
            if context
            else [f"('{key}' not found in serial log; raw step result: {(detail.get('text_data') or '').strip()!r})"]
        )
        out += _section(f"Serial Terminal Log (IPA test: {step_name})", body)
    return out


def _serial_tail(job: _Job) -> list[str]:
    return _section(
        f"Serial Terminal Log (last {job.context_lines} lines)",
        log_lines(get_serial_tail(job.serial, job.context_lines)),
    )


def _publiccloud_context(job: _Job) -> list[str]:
    """Anchor on the img-proof summary line; the serial tail is Terraform teardown noise."""
    context = find_in_log(job.serial, "FAILED tests=", job.context_lines, _PUBLICCLOUD_CONTEXT_AFTER)
    return _section("Serial Terminal Log (img-proof run summary)", format_log_context(context)) if context else []


def _needle_context(job: _Job, needle: str) -> list[str]:
    """Search autoinst-log.txt for a needle failure, which only appears there."""
    out: list[str] = []
    if context := find_in_log(job.autoinst, needle, job.context_lines, job.context_lines):
        body = (
            _masked(condense_log(context["before"], autoinst=True))
            + matched_line(context["matched"])
            + _masked(condense_log(context["after"], autoinst=True))
        )
        out += _section("autoinst-log.txt (searching for needle failure)", body)
    if job.booted_to_login:
        out += ["Note: serial0.txt ends at a login prompt: the system booted but the needle did not match.", ""]
    return out


def _died_step(job: _Job, detail: JsonDict, category: str | None) -> list[str]:
    text_data = detail.get("text_data") or ""
    out = ["*** TEST DIED ERROR ***", "", f"Error in step #{detail.get('num', '?')}:"]
    out += [redact(clean_died_text(text_data)), ""]
    if category == "publiccloud":
        return out + _publiccloud_context(job)
    needle = _NEEDLE_RE.search(text_data)
    if needle and job.autoinst:
        return out + _needle_context(job, needle.group(0))
    if command := parse_command_from_test_died(text_data):
        context = find_in_log(job.serial, command, job.context_lines, job.context_lines)
        return out + (
            _section(f"Serial Terminal Log (searching for: {command})", format_log_context(context)) if context else []
        )
    return out + _serial_tail(job)


def _wait_serial_step(job: _Job, detail: JsonDict) -> list[str]:
    text_data = detail.get("text_data") or ""
    command = parse_command_from_text_data(text_data)
    exit_code = parse_exit_code_from_text_data(text_data)
    out = [f"Failed Command #{detail.get('num', '?')}:"]
    out.append(f"  {redact(command)}" if command else "  (Could not parse command)")
    if exit_code is not None:
        out.append(f"  Exit code: {exit_code}")
    out.append("")
    context = find_in_log(job.serial, command, job.context_lines) if command else None
    if context:
        return out + _section(
            f"Serial Terminal Log ({job.context_lines} lines before/after)", format_log_context(context)
        )
    return [*out, "Text data from test details:", redact(filter_text_data_lines(text_data)), ""]


def _other_step(job: _Job, detail: JsonDict, cluster: list[JsonDict]) -> list[str]:
    text_data = detail.get("text_data") or ""
    if (
        not text_data
        and detail.get("screenshot")
        and any("# Test died:" in (d.get("text_data") or "") for d in cluster)
    ):
        return []
    out = [
        "*** STEP FAILURE ***",
        "",
        f"Failure in step #{detail.get('num', '?')}: {detail.get('display_title', 'Unknown')}",
    ]
    if text_data:
        out.append(redact(clean_died_text(text_data)))
    out.append("")
    return out if text_data else out + _serial_tail(job)


def _step_report(
    job: _Job, detail: JsonDict, repeats: list[int], cluster: list[JsonDict], category: str | None
) -> list[str]:
    out = [repeat_note(repeats)] if repeats else []
    if "# Test died:" in (detail.get("text_data") or ""):
        return out + _died_step(job, detail, category)
    if detail.get("display_title") == "wait_serial":
        return out + _wait_serial_step(job, detail)
    return out + _other_step(job, detail, cluster)


def _step_context(
    all_details: list[JsonDict], cluster: list[JsonDict], before: list[JsonDict], after: list[JsonDict]
) -> list[str]:
    """Render the steps around a cluster; a long cluster keeps only its first and last steps."""
    low, high = cluster[0].get("num", 0), cluster[-1].get("num", 0)
    steps = [d for d in all_details if low <= d.get("num", 0) <= high]
    half = CLUSTER_STEPS_SHOWN // 2
    omitted = max(0, len(steps) - 2 * half)
    if omitted:
        steps = steps[:half] + steps[-half:]
    window = before + steps + after
    out = ["Step context:"]
    for i, step in enumerate(window):
        if omitted and i == len(before) + half:
            out.append(f"  ... ({omitted} steps omitted)")
        out.append(f"  {redact(format_step_summary_line(step))}")
    return [*out, ""]


def _autoinst_range(job: _Job, cluster: list[JsonDict], before: list[JsonDict], after: list[JsonDict]) -> list[str]:
    """Show the autoinst-log.txt range spanning the context steps and the cluster."""
    if not job.autoinst:
        return []
    start = extract_search_string_from_step(before[0] if before else cluster[0])
    end = extract_search_string_from_step(after[-1] if after else cluster[-1])
    log_range = find_log_range(job.autoinst, start, end, max_lines=2 * job.context_lines)
    lines = condense_log(log_range or [], autoinst=True, max_lines=job.context_lines)
    return _section("autoinst-log.txt (step context)", _masked(lines)) if lines else []


def _cluster_report(
    job: _Job, all_details: list[JsonDict], cluster: list[JsonDict], retries: list[int], category: str | None
) -> list[str]:
    out: list[str] = []
    if retries:
        shown = ", ".join(f"#{n}" for n in retries[:_RETRIES_SHOWN])
        more = f" ... (+{len(retries) - _RETRIES_SHOWN})" if len(retries) > _RETRIES_SHOWN else ""
        out += [f"The failure below was retried {len(retries)} more times before this attempt: {shown}{more}", ""]
    before, after = get_context_steps(
        all_details,
        cluster[0].get("num", 0),
        cluster[-1].get("num", 0),
        before=_STEPS_BEFORE_SCAN,
        after=_STEPS_AFTER_SCAN,
    )
    before = [d for d in before if not is_noise_step(d)][-_STEPS_BEFORE_SHOWN:]
    after = [d for d in after if not is_noise_step(d)][:_STEPS_AFTER_SHOWN]
    out += _step_context(all_details, cluster, before, after) + _autoinst_range(job, cluster, before, after)
    for detail, repeats in group_repeated_failures(cluster)[:STEP_GROUP_MAX]:
        out += _step_report(job, detail, repeats, cluster, category)
    return out


def _module_report(job: _Job, module: JsonDict, uploads: dict[str, list[str]]) -> list[str]:
    category = module.get("category")
    all_details = module.get("details", [])
    out = [_RULE, f"Failed Module: {_module_title(module)}", _RULE, ""]
    out += module_cmd_lines(all_details)
    step_results = [d.get("result") for d in all_details]
    n_fail, n_soft = step_results.count("fail"), step_results.count("softfail")
    if n_fail > 1 or n_soft:
        out += [f"Steps: {n_fail} failed / {n_soft} softfailed", ""]
    post_start = post_fail_start(all_details)
    clusters = [
        c
        for cl in group_failures_by_proximity(all_details)
        if (c := [d for d in cl if not is_secondary(d, post_start)])
    ]
    secondary = [
        d for d in all_details if d.get("result") == "fail" and d.get("text_data") and is_secondary(d, post_start)
    ]
    # xfstests keep their failure information in record_info steps, IPA test cases in the serial log
    if not clusters and category == "xfstests":
        return out + xfstests_lines(all_details)
    if category == "IPA":
        return out + _ipa_report(job, all_details, module.get("name", "unknown"))
    collapsed = collapse_repeated_clusters(clusters)
    for cluster, retries in collapsed[:CLUSTER_MAX]:
        out += _cluster_report(job, all_details, cluster, retries, category)
    if len(collapsed) > CLUSTER_MAX:
        out += [f"... ({len(collapsed) - CLUSTER_MAX} more clusters omitted)", ""]
    out += [redact(secondary_line(sec, repeats)) for sec, repeats in group_repeated_failures(secondary)]
    if secondary:
        out.append("")
    return out + format_uploaded_logs(uploads)


def _key_error_signals(
    failed_modules: list[JsonDict],
    boot_log: str,
    job_level: list[dict[str, list[str]]],
    uploads: dict[str, dict[str, list[str]]],
) -> list[str]:
    signals = [
        f"{d.get('display_title', '')}\n{d.get('text_data') or ''}"
        for m in failed_modules
        for d in m.get("details", [])
    ]
    signals.append(boot_log)
    for logs in job_level:
        for lines in logs.values():
            signals.extend(lines)
    if oops := kdump_oops_symbol(boot_log):
        signals.append(f"KDUMP_OOPS: {oops}")
    for logs in uploads.values():
        for name, lines in logs.items():
            signals.extend(lines)
            if name.endswith("-widgets.json") and (dialog := dialog_signal(lines)):
                signals.append(dialog)
    return signals


def _preamble(
    oqa: OpenQA, job_id: str, job: _Job, failed_modules: list[JsonDict], *, include_softfailed: bool
) -> tuple[list[str], dict[str, dict[str, list[str]]], bool]:
    """Return the job-wide report lines, the analyzed modules' uploads and whether serial0.txt ends at a login."""
    names = list_job_files(oqa, job_id)
    boot_log, boot_report = scan_kernel_crash(oqa, job_id, job.context_lines, select_console_files(names) or None)
    tail = "\n".join(boot_log.rstrip().split("\n")[-_LOGIN_PROMPT_TAIL_LINES:])
    booted_to_login = bool(_LOGIN_PROMPT_RE.search(tail))
    consoles, job_files = collect_job_level_logs(oqa, job_id, names)
    uploads = {
        module_key(m): collect_uploaded_logs(oqa, job_id, names, m.get("name", ""))
        for m in failed_modules[:_MODULES_MAX]
    }
    out: list[str] = []
    if key_error := summarize_key_error(
        "\n".join(_key_error_signals(failed_modules, boot_log, [consoles, job_files], uploads))
    ):
        out += [redact(f"Key error: {key_error}"), ""]
    if (died := last_test_died(job.autoinst)) and died[1] > 1:
        out += [redact(f"Last test died ({died[1]} in autoinst-log.txt): {died[0]}"), ""]
    kind = "failed/softfailed" if include_softfailed else "failed"
    out += [f"Found {len(failed_modules)} {kind} module(s)", "", *boot_report]
    out += format_uploaded_logs(consoles, "console warnings") + format_uploaded_logs(job_files)
    return out, uploads, booted_to_login


def _analyze(  # ruff: ignore[too-many-arguments]
    oqa: OpenQA, job_id: str, context_lines: int, out: list[str], api_job: JsonDict | None, *, include_softfailed: bool
) -> None:
    out += [f"OpenQA Job Analysis: {oqa.server}/t{job_id}", ""]
    out.append(f"Fetching test details from {oqa.server}/tests/{job_id}/details_ajax...")
    details_json = oqa.json(f"/tests/{job_id}/details_ajax")
    failed_modules = extract_failed_modules(details_json, include_softfailed=include_softfailed)
    api_job = api_job or fetch_job(oqa, job_id)
    out += job_header(api_job, details_json) + run_history(oqa, job_id, api_job)
    if not failed_modules:
        if lines := job_level_failure(oqa, job_id, api_job):
            out += lines
            return
        msg = f"No failed{' or softfailed' if include_softfailed else ''} modules found in this test job."
        out.append(msg)
        raise NoFailedModulesError(msg)
    job = _Job(oqa.job_file(job_id, "serial_terminal.txt"), oqa.job_file(job_id, "autoinst-log.txt"), context_lines)
    lines, uploads, booted = _preamble(oqa, job_id, job, failed_modules, include_softfailed=include_softfailed)
    out += lines
    job = replace(job, booted_to_login=booted)
    for module in failed_modules[:_MODULES_MAX]:
        out += _module_report(job, module, uploads[module_key(module)])
    if rest := failed_modules[_MODULES_MAX:]:
        names = ", ".join(m.get("name", "unknown") for m in rest)
        out += [f"... {len(rest)} more failed modules not analyzed: {names}", ""]


def analyze_job(
    oqa: OpenQA,
    job_id: str,
    *,
    context_lines: int = 100,
    include_softfailed: bool = False,
    job: JsonDict | None = None,
) -> str:
    """Return the redacted failure analysis text of one openQA job (`job`: its API data, fetched if None).

    Raises `NoFailedModulesError` when the job has no failed (or, with `include_softfailed`, softfailed)
    module and no job-level reason or log signal either, and `AnalyzerError` when the details cannot be fetched.
    The text produced so far is kept in the error's `partial_output`.
    """
    out: list[str] = []
    try:
        _analyze(oqa, job_id, context_lines, out, job, include_softfailed=include_softfailed)
    except AnalyzerError as e:
        e.partial_output = redact("\n".join(out) + "\n")
        raise
    return redact("\n".join(out) + "\n")
