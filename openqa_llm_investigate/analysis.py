# Copyright SUSE LLC
"""Build the failure analysis text of an openQA job from its details and logs."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .failures import (
    CLUSTER_STEPS_SHOWN,
    extract_failed_modules,
    extract_test_died_errors,
    failing_cluster,
    failing_command_line,
    failing_step,
    get_context_steps,
    group_repeated_failures,
    is_command_step,
    is_noise_step,
    is_record_info,
    is_secondary,
    module_key,
    post_fail_start,
    previous_modules,
    repeat_note,
    xfstests_lines,
)
from .history import fetch_job, job_header, run_history
from .http import AnalyzerError, NoFailedModulesError
from .logs import (
    command_output,
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
    condense_log,
    extract_search_string_from_step,
    filter_text_data_lines,
    format_step_summary_line,
    has_command,
    ipa_search_key,
    parse_command_from_test_died,
    parse_command_from_text_data,
    record_info_body,
    redact,
    split_died_text,
    step_output_tail,
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
_STEPS_BEFORE_SCAN = 70
_STEPS_AFTER_SCAN = 30
_AFTER_COMMANDS = 3
_FAILING_OUTPUT_LINES = 50
_STEPS_BEFORE_SHOWN = 20
_STEPS_AFTER_SHOWN = 3
_OUTPUT_STEPS = 5
_RECORD_INFO_FULL = 5
_RECORD_INFO_FULL_LINES = 30
_RECORD_INFO_BRIEF_LINES = 2
_AUTOINST_STEPS_BEFORE = 5
_FAILURE_MARKER = "  >>> The next step is the one that failed the test <<<"
_RETRIES_SHOWN = 5
_PUBLICCLOUD_CONTEXT_AFTER = 5
_LOGIN_PROMPT_TAIL_LINES = 10
_MODULES_MAX = 3
_MIN_COMMAND_CHARS = 5
_NEEDLE_RE = re.compile(r"no candidate needle with tag\(s\) '[^']+'")
_LOGIN_PROMPT_RE = re.compile(r"login:\s*$")
STACK_TRACE = "Stack trace"
SECTION_END = "--- End of Log Section ---"
TEST_DIED = "# Test died:"


@dataclass
class _Job:
    """A job's logs and the settings the report sections need."""

    serial: str
    autoinst: str
    context_lines: int
    booted_to_login: bool = False


def section_start(title: str) -> str:
    """Return the line that opens the analysis section `title`."""
    return f"--- {title} ---"


def _section(title: str, body: list[str]) -> list[str]:
    return [section_start(title), *body, SECTION_END, ""]


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


def _died_step(job: _Job, detail: JsonDict, category: str | None) -> tuple[list[str], list[str]]:
    """Return the died step's message (plus its command's output) and the sections that follow the step context."""
    text_data = detail.get("text_data") or ""
    body, trace = split_died_text(text_data)
    sections = _section(STACK_TRACE, _masked(trace)) if trace else []
    if category == "publiccloud":
        return body, sections + _publiccloud_context(job)
    needle = _NEEDLE_RE.search(text_data)
    if needle and job.autoinst:
        return body, sections + _needle_context(job, needle.group(0))
    if command := parse_command_from_test_died(text_data):
        if output := command_output(job.serial, command, _FAILING_OUTPUT_LINES):
            return body + output, sections
        context = find_in_log(job.serial, command, job.context_lines, job.context_lines)
        found = (
            _section(f"Serial Terminal Log (searching for: {command})", format_log_context(context)) if context else []
        )
        return body, sections + found
    return body, sections + _serial_tail(job)


def _wait_serial_step(job: _Job, detail: JsonDict) -> tuple[list[str], list[str]]:
    text_data = detail.get("text_data") or ""
    if output := step_output_tail(text_data, _FAILING_OUTPUT_LINES):
        return output, []
    command = parse_command_from_text_data(text_data)
    context = find_in_log(job.serial, command, job.context_lines) if command else None
    if context:
        title = f"Serial Terminal Log ({job.context_lines} lines before/after)"
        return [], _section(title, format_log_context(context))
    return filter_text_data_lines(text_data).split("\n"), []


def _other_step(job: _Job, detail: JsonDict, cluster: list[JsonDict]) -> tuple[list[str], list[str]]:
    text_data = detail.get("text_data") or ""
    if text_data:
        body, trace = split_died_text(text_data)
        return body, _section(STACK_TRACE, _masked(trace)) if trace else []
    bare_screen = detail.get("screenshot") and any(TEST_DIED in (d.get("text_data") or "") for d in cluster)
    return ([], []) if bare_screen else ([], _serial_tail(job))


def _step_bodies(job: _Job, cluster: list[JsonDict], category: str | None) -> tuple[dict[int, list[str]], list[str]]:
    """Return the text shown under the failing and the died step in the step context, and the sections after it."""
    failing = failing_step(cluster)
    died = next((d for d in reversed(cluster) if TEST_DIED in (d.get("text_data") or "")), None)
    bodies: dict[int, list[str]] = {}
    sections: list[str] = []
    for step in (d for d in cluster if d is failing or d is died):
        if step is died:
            body, extra = _died_step(job, step, category)
        elif step.get("display_title") == "wait_serial":
            body, extra = _wait_serial_step(job, step)
        else:
            body, extra = _other_step(job, step, cluster)
        bodies[id(step)] = body
        sections += extra
    return bodies, sections


def _repeat_notes(cluster: list[JsonDict], shown: list[JsonDict]) -> list[str]:
    """Return a note for each group of identical failures that has a step in `shown`."""
    notes = []
    for first, repeats in group_repeated_failures(cluster):
        nums = [first.get("num", 0), *repeats]
        own = next((s.get("num", 0) for s in shown if s.get("num", 0) in nums), None)
        if own is not None and repeats:
            notes.append(repeat_note([n for n in nums if n != own]))
    return notes


def _step_lines(step: JsonDict, *, output: bool, info_lines: int, fail_label: str, failure: list[str]) -> list[str]:
    """Render a step: record_info as title plus `info_lines` body lines, a command with its output tail if `output`.

    `failure` is the text under a failing step; its first line is dropped when the step line already shows it.
    """
    if is_record_info(step):
        head = f"#{step.get('num', '?')}  {' '.join(str(step['display_title']).split())}"
        body = record_info_body(step["text_data"], info_lines)
    else:
        head = format_step_summary_line(step, fail_label)
        body = [ln for ln in failure if ln.strip()]
        if body and body[0] in head:
            body = body[1:]
        body = body or (step_output_tail(step.get("text_data") or "") if output else [])
    return [f"  {redact(head)}", *(f"    {redact(ln)}" for ln in body)]


def _marker_step(shown: list[JsonDict], failing: JsonDict) -> JsonDict | None:
    """Return the step the failure marker goes before: the failing one or the bare failed screens above it."""
    idx = next((i for i, d in enumerate(shown) if d is failing), None)
    if idx is None:
        return None
    while idx and shown[idx - 1].get("result") == "fail" and not shown[idx - 1].get("text_data"):
        idx -= 1
    return shown[idx]


def _fail_label(step: JsonDict, cluster: list[JsonDict], post_start: int | None) -> str:
    if any(d is step for d in cluster):
        return "FAIL"
    return "FAIL, post-fail hook" if is_secondary(step, post_start) else "FAIL, non-fatal"


def _step_context(
    all_details: list[JsonDict],
    cluster: list[JsonDict],
    before: list[JsonDict],
    after: list[JsonDict],
    bodies: dict[int, list[str]],
) -> list[str]:
    """Render the steps around a cluster; a long cluster keeps only its first and last steps and those with a body.

    The newest commands before the cluster show their output tail, the newest record_info steps (and those after
    the cluster) their body; older ones are reduced to a line or two. `bodies` holds the text under failing steps.
    """
    low, high = cluster[0].get("num", 0), cluster[-1].get("num", 0)
    steps = [d for d in all_details if low <= d.get("num", 0) <= high]
    half = CLUSTER_STEPS_SHOWN // 2
    shown = before + steps + after
    marker = _marker_step(shown, failing_step(cluster))
    hidden = {id(d) for d in steps[half:-half]} - bodies.keys() - {id(marker)} if len(steps) > 2 * half else set()
    with_output = {id(d) for d in [d for d in before if is_command_step(d)][-_OUTPUT_STEPS:]}
    with_info = {id(d) for d in [d for d in before if is_record_info(d)][-_RECORD_INFO_FULL:]}
    with_info |= {id(d) for d in after}
    with_output |= {id(d) for d in after if has_command(d.get("text_data") or "")}
    post_start = post_fail_start(all_details)
    out = [f"Step context (last {_STEPS_BEFORE_SHOWN} command/needle/record_info steps)"]
    skipped = 0
    for step in shown:
        if id(step) in hidden:
            skipped += 1
            continue
        if skipped:
            out.append(f"  ... ({skipped} steps omitted)")
            skipped = 0
        if step is marker:
            out.append(_FAILURE_MARKER)
        info_lines = _RECORD_INFO_FULL_LINES if id(step) in with_info else _RECORD_INFO_BRIEF_LINES
        out += _step_lines(
            step,
            output=id(step) in with_output,
            info_lines=info_lines,
            fail_label=_fail_label(step, cluster, post_start),
            failure=bodies.get(id(step), []),
        )
    return [*out, ""]


def _autoinst_range(job: _Job, cluster: list[JsonDict], before: list[JsonDict], after: list[JsonDict]) -> list[str]:
    """Show the autoinst-log.txt range spanning the last context steps and the cluster."""
    if not job.autoinst:
        return []
    anchors = (*before[-_AUTOINST_STEPS_BEFORE:], cluster[0])
    start = next((s for d in anchors if (s := extract_search_string_from_step(d))), None)
    end = extract_search_string_from_step(after[-1] if after else cluster[-1])
    log_range = find_log_range(job.autoinst, start, end, max_lines=2 * job.context_lines)
    lines = condense_log(log_range or [], autoinst=True, max_lines=job.context_lines)
    return _section("autoinst-log.txt (step context)", _masked(lines)) if lines else []


def _steps_until_commands(steps: list[JsonDict], n: int) -> list[JsonDict]:
    """Return `steps` up to and including the `n`-th one that ran a command."""
    seen = 0
    for i, step in enumerate(steps):
        seen += has_command(step.get("text_data") or "")
        if seen >= n:
            return steps[: i + 1]
    return steps


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
    after = _steps_until_commands([d for d in after if not is_noise_step(d)], _AFTER_COMMANDS)
    bodies, sections = _step_bodies(job, cluster, category)
    out += _repeat_notes(cluster, [d for d in cluster if id(d) in bodies])
    out += _step_context(all_details, cluster, before, after, bodies)
    out += _autoinst_range(job, cluster, before, after[:_STEPS_AFTER_SHOWN])
    return out + sections


def _previous_modules_line(all_modules: list[JsonDict], module: JsonDict) -> str:
    previous = previous_modules(all_modules, module)
    listed = ", ".join(f"{_module_title(m)} ({m.get('result', 'unknown')})" for m in previous)
    return f"Previous modules: {listed or 'none (first module)'}"


def _module_report(
    job: _Job, module: JsonDict, uploads: dict[str, list[str]], all_modules: list[JsonDict]
) -> list[str]:
    category = module.get("category")
    all_details = module.get("details", [])
    out = [_RULE, f"Failed Module: {_module_title(module)}", _RULE, ""]
    out += [_previous_modules_line(all_modules, module), ""]
    failing = failing_cluster(all_details)
    if failing and (line := failing_command_line(failing[0])):
        out += [redact(line), ""]
    # xfstests keep their failure information in record_info steps, IPA test cases in the serial log
    if not failing and category == "xfstests":
        return out + xfstests_lines(all_details)
    if category == "IPA":
        return out + _ipa_report(job, all_details, module.get("name", "unknown"))
    if failing:
        out += _cluster_report(job, all_details, *failing, category)
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


def _typed_command_re(command: str) -> re.Pattern[str]:
    """Match the command of a step that typed `command`, also as script_retry and zypper_call wrap it."""
    return re.compile(
        rf"(?:timeout -k \d+ (?:\S+ )?\d+ )?{re.escape(command)}"
        r"(?: ?\| (?:cat|tee \S+))?(?: ?; \( exit \$\{PIPESTATUS\[0\]\} \))?"
    )


def _ran_command(step: JsonDict, typed: re.Pattern[str]) -> bool:
    """Return whether a step is the `# Command:` step of a typed command, with or without its `; echo` suffix."""
    text = step.get("text_data") or ""
    return text.startswith("# Command:") and bool(typed.fullmatch(parse_command_from_text_data(text) or ""))


def _died_command_run(
    job: _Job, details: list[JsonDict], cluster: list[JsonDict], category: str | None
) -> JsonDict | None:
    """Return the last step before the died one that ran its command, if `_died_step` shows that command's output.

    That step holds the output of the run that failed; the serial log can hold a later run or a longer command too.
    """
    died = next((d for d in reversed(cluster) if TEST_DIED in (d.get("text_data") or "")), None)
    if died is None or category == "publiccloud" or (_NEEDLE_RE.search(died["text_data"]) and job.autoinst):
        return None
    command = parse_command_from_test_died(died["text_data"]) or ""
    if len(command) < _MIN_COMMAND_CHARS:
        return None
    typed, num = _typed_command_re(command), died.get("num", 0)
    return next((d for d in reversed(details) if d.get("num", 0) < num and _ran_command(d, typed)), None)


def _failing_step_texts(job: _Job, modules: list[JsonDict]) -> list[str]:
    """Return the texts of the steps that failed each module and of the step that ran its died command."""
    texts: list[str] = []
    for module in modules:
        details = module.get("details", [])
        if not (failing := failing_cluster(details)):
            continue
        cluster = failing[0]
        run = _died_command_run(job, details, cluster, module.get("category"))
        steps = [*cluster, run] if run and not any(d is run for d in cluster) else cluster
        texts += [f"{d.get('display_title', '')}\n{d.get('text_data') or ''}" for d in steps]
    return texts


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
    signals = "\n".join(_key_error_signals(failed_modules, boot_log, [consoles, job_files], uploads))
    failing = _failing_step_texts(job, failed_modules[:_MODULES_MAX])
    selinux_check = any(
        "selinux" in m.get("name", "") and m.get("result") == "failed" for m in failed_modules[:_MODULES_MAX]
    )
    if key_error := summarize_key_error(signals, failing, selinux_check=selinux_check):
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
    out += [f"OpenQA {'Failed/Softfailed' if include_softfailed else 'Failed'} Job Analysis:", ""]
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
        out += _module_report(job, module, uploads[module_key(module)], details_json.get("modules", []))
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
