# Copyright SUSE LLC
"""Search serial/autoinst logs and report boot crashes, key errors and job-level failures."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypedDict

from .text import ESC_RE, condense_log, is_boilerplate_log_line, redact

if TYPE_CHECKING:
    from .http import JsonDict, OpenQA

_RULE = "=" * 80
_FALLBACK_MAX_LINES = 200
_BOOT_FATAL_RE = re.compile(
    r"Kernel panic|dracut: FATAL|Refusing to continue|Synchronous Exception"
    r"|\bBUG: |\bOops: |[Ee]mergency (?:shell|mode)"
)
_EXPECTED_CRASH_RE = re.compile(r"sysrq: Trigger a crash")
_REGISTER_DUMP_RE = re.compile(
    r"^(?:\[[\s\d.]+\]\s*)?(?:Code:|R(?:AX|BX|CX|DX|SI|DI|BP|SP)\b|R\d+:|(?:CS|DS|ES|FS|GS)[: ]|CR[0-4]:|DR\d)"
)
_EVENT_GAP = 30
_EVENT_BEFORE = 5
_EVENT_AFTER = 10
_EXPECTED_LOOKBACK = 10
_OOPS_SYMBOL_RE = re.compile(r"\b(?:RIP|IP):\s*(?:\w+:)?([A-Za-z_]\w*)")
_KEY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pattern, re.MULTILINE), template)
    for pattern, template in (
        (r"Permission to access '(?:\w+://)?([^/'\s]+)[^']*' denied", r"repo auth failed (\1)"),
        (
            r"Download \(curl\) error for '(?:\w+://)?([^/']+)[^']*':\s*Error code: HTTP response: 401",
            r"repo auth failed (\1)",
        ),
        (r"No enabled repos", "system not registered (no enabled repos)"),
        (
            r"does not exist in zone|(?:REGION|ZONE) UNAVAILABLE|has no resources available",
            "cloud capacity or machine type unavailable",
        ),
        (r"dracut: FATAL: FIPS", "initrd FIPS integrity failure"),
        (r"Synchronous Exception", "firmware crash (Synchronous Exception)"),
        (r"scon=(?:\w+:){2}(\w+)\S*[ ].*?tcon=(?:\w+:){0,2}(\w+)\S*.*?perm=(\w+)", r"SELinux denial (\1 -> \2 \3)"),
        (r"SELinux policy denies|avc:\s+denied", "SELinux denial"),
        (r"[Ee]mergency mode", "boot reached emergency mode"),
        (r"cloud-init.*(?:ERROR|Traceback)", "cloud-init error"),
        (r"^DIALOG: (.+)$", r'unexpected dialog: "\1"'),
        (r"^KDUMP_OOPS: (\S+)", r"crash kernel oops in \1"),
    )
]
_JOB_LOG_SIGNAL_RE = re.compile(r"\[(?:warn|error)\]|!!!|\bdied\b|Unable to|Failed to|No such file")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_JOB_LOG_MAX_LINES = 60
_JOB_LOG_TAIL_LINES = 30
_LINE_MAX_CHARS = 300
_SIGNAL_CONTEXT_LINES = 3
_DIALOG_PREFIX_LEN = len("title: ")


class LogContext(TypedDict):
    """Lines around a matched log line."""

    before: list[str]
    matched: str
    after: list[str]


def get_serial_tail(serial_log: str, lines: int = 75) -> list[str]:
    """Return the last `lines` lines of a serial log."""
    return serial_log.split("\n")[-lines:] if lines > 0 else []


def log_lines(lines: list[str]) -> list[str]:
    """Condense serial-log lines, dropping script-runner boilerplate and masking codes."""
    return [redact(ln) for ln in condense_log(lines) if not is_boilerplate_log_line(ln)]


def matched_line(line: str) -> list[str]:
    """Return a matched serial-log line prefixed with `>>> `, or nothing for boilerplate."""
    return [] if is_boilerplate_log_line(line) else [f">>> {redact(ESC_RE.sub('', line))}"]


def format_log_context(context: LogContext) -> list[str]:
    """Return a `find_in_log` match with its surroundings as report lines."""
    return log_lines(context["before"]) + matched_line(context["matched"]) + log_lines(context["after"])


def find_in_log(
    log_content: str, search_string: str | None, context_lines_before: int = 10, context_lines_after: int | None = None
) -> LogContext | None:
    """Find a string in a log and return its surroundings (a `# ` command line wins, else the last match)."""
    if not search_string:
        return None
    after = context_lines_before if context_lines_after is None else context_lines_after
    lines = log_content.split("\n")
    matches = [i for i, ln in enumerate(lines) if search_string in ln]
    if not matches:
        return None
    commands = [i for i in matches if lines[i].strip().startswith("# ")]
    idx = (commands or matches)[-1]
    return {
        "before": lines[max(0, idx - context_lines_before) : idx],
        "matched": lines[idx],
        "after": lines[idx + 1 : idx + after + 1],
    }


def _line_index(log_content: str, lines: list[str], marker: str | None) -> int | None:
    """Return the index of the line `find_in_log` prefers for `marker`."""
    found = find_in_log(log_content, marker, 0, 0)
    if found is None:
        return None
    return next((i for i in range(len(lines) - 1, -1, -1) if lines[i] == found["matched"]), None)


def find_log_range(
    log_content: str, start_marker: str | None, end_marker: str | None, max_lines: int | None = None
) -> list[str] | None:
    """Return the lines from `start_marker` to `end_marker` (None without a start marker).

    Without a usable end marker the result is capped at `max_lines` (200 by default); a
    span longer than `max_lines` keeps its first and last halves around an omission note.
    """
    if not log_content:
        return None
    lines = log_content.split("\n")
    start = _line_index(log_content, lines, start_marker)
    if start is None:
        return None
    end = _line_index(log_content, lines, end_marker)
    if end is None or end < start:
        return lines[start : start + (_FALLBACK_MAX_LINES if max_lines is None else max_lines)]
    span = lines[start : end + 1]
    if max_lines is not None and len(span) > max_lines:
        half = max_lines // 2
        return [
            *span[:half],
            f"[...{len(span) - max_lines} lines omitted to fit context limit...]",
            *(span[-half:] if half else []),
        ]
    return span


def _fatal_events(lines: list[str]) -> list[list[int]]:
    """Group indices of fatal boot/kernel lines into events (gap <= `_EVENT_GAP` lines)."""
    events: list[list[int]] = []
    for i, ln in enumerate(lines):
        if not _BOOT_FATAL_RE.search(ln):
            continue
        if events and i - events[-1][-1] <= _EVENT_GAP:
            events[-1].append(i)
        else:
            events.append([i])
    return events


def _event_expected(lines: list[str], event: list[int]) -> bool:
    """Return True when a sysrq-triggered crash (kdump's expected panic) precedes or is inside the event."""
    return any(_EXPECTED_CRASH_RE.search(ln) for ln in lines[max(0, event[0] - _EXPECTED_LOOKBACK) : event[-1] + 1])


def _crash_report(filename: str, lines: list[str], context_lines: int) -> list[str]:
    """Report the most relevant fatal event of a log (the last unexpected one, else the last)."""
    events = _fatal_events(lines)
    if not events:
        return []
    unexpected = [ev for ev in events if not _event_expected(lines, ev)]
    first = (unexpected or events)[-1][0]
    panic = "Kernel panic" in lines[first]
    label = "Kernel panic" if panic else "boot/firmware fatal error"
    out = ["", _RULE, f"*** {'KERNEL PANIC' if panic else 'FATAL BOOT ERROR'} DETECTED ***", _RULE, ""]
    out.append(f"--- {filename} (searching for: {label}) ---")
    if len(events) > 1:
        out.append(f"({len(events)} fatal events found; showing the {'last unexpected' if unexpected else 'last'})")
    elif not unexpected:
        out.append("(expected: crash triggered via sysrq)")
    start = max(0, first - min(context_lines, _EVENT_BEFORE))
    end = min(len(lines), first + min(context_lines, _EVENT_AFTER) + 1)
    for i in range(start, end):
        if not lines[i].strip() or (i != first and _REGISTER_DUMP_RE.match(lines[i].strip())):
            continue
        out.append(f"{'>>> ' if i == first else ''}{redact(lines[i])}")
    return [*out, "--- End of Log Section ---", ""]


def scan_kernel_crash(
    oqa: OpenQA, job_id: str, context_lines: int, console_files: list[str] | None = None
) -> tuple[str, list[str]]:
    """Scan serial0.txt and the console files for fatal boot events.

    Return the serial0.txt content ("" if unavailable) and the report lines (empty if none).
    """
    serial0 = oqa.job_file(job_id, "serial0.txt")
    report = _crash_report("serial0.txt", [ESC_RE.sub("", ln) for ln in serial0.split("\n")], context_lines)
    for name in console_files or ["destroy-console.txt"]:
        text = oqa.job_file(job_id, name)
        report += _crash_report(name, [ESC_RE.sub("", ln) for ln in text.split("\n")], context_lines)
    return serial0, report


def kdump_oops_symbol(serial0: str) -> str | None:
    """Return the symbol of an unexpected oops after a sysrq-triggered (kdump) crash, else None."""
    lines = [ESC_RE.sub("", ln) for ln in serial0.split("\n")]
    events = _fatal_events(lines)
    expected = [i for i, ev in enumerate(events) if _event_expected(lines, ev)]
    if not expected:
        return None
    for i in range(len(events) - 1, expected[0], -1):
        if i in expected:
            continue
        for ln in lines[events[i][0] : events[i][-1] + 1]:
            if m := _OOPS_SYMBOL_RE.search(ln):
                return m.group(1)
    return None


def dialog_signal(lines: list[str]) -> str | None:
    """Return a `DIALOG: <title or first text>` signal from "key: value" widget lines (None if empty)."""
    lines = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    if not lines:
        return None
    title = next((ln[_DIALOG_PREFIX_LEN:] for ln in lines if ln.startswith("title: ")), None)
    return f"DIALOG: {(title or lines[0].split(': ', 1)[-1])[:100]}"


def summarize_key_error(text: str) -> str | None:
    """Return a one-line verdict for the first matching signal rule (None if no rule matches)."""
    for rx, template in _KEY_RULES:
        if m := rx.search(text):
            return m.expand(template)
    return None


def last_test_died(autoinst_log: str) -> tuple[str, int] | None:
    """Return the last `Test died:` message of autoinst-log.txt and how many there are (None if none)."""
    found = [m.group(0)[:_LINE_MAX_CHARS] for ln in autoinst_log.split("\n") if (m := re.search(r"Test died: .*", ln))]
    return (ESC_RE.sub("", found[-1]), len(found)) if found else None


def extract_job_log_excerpt(log: str) -> list[str]:
    """Pick the failure-relevant lines of an autoinst-log.txt (empty if none).

    Matches warn/error/died-style lines plus 2 lines after each, keeping the last
    `_JOB_LOG_MAX_LINES`; empty when nothing matches so callers can tell "no signal".
    """
    lines = [_ANSI_RE.sub("", ln).rstrip()[:_LINE_MAX_CHARS] for ln in log.split("\n")]
    keep: set[int] = set()
    for i, ln in enumerate(lines):
        if _JOB_LOG_SIGNAL_RE.search(ln) and "make your needles more specific" not in ln:
            keep.update(range(i, min(i + _SIGNAL_CONTEXT_LINES, len(lines))))
    picked = [lines[i] for i in sorted(keep) if lines[i].strip() and not is_boilerplate_log_line(lines[i])]
    return picked[-_JOB_LOG_MAX_LINES:]


def job_level_failure(oqa: OpenQA, job_id: str, job: JsonDict) -> list[str]:
    """Report a pseudo 'Job' module for a job that died before any module failed.

    Uses the job API's `reason` and relevant autoinst-log.txt lines (else its tail);
    empty for passed/softfailed jobs without a reason and when there is neither a reason nor a log.
    """
    result = job.get("result", "none")
    reason = job.get("reason") or ""
    if result in {"passed", "softfailed", "none"} and not reason:
        return []
    log = oqa.job_file(job_id, "autoinst-log.txt")
    excerpt = extract_job_log_excerpt(log)
    if not reason and not log.strip():
        return []
    if not excerpt:
        tail = [_ANSI_RE.sub("", ln).rstrip()[:_LINE_MAX_CHARS] for ln in log.split("\n")]
        excerpt = [ln for ln in tail if ln.strip()][-_JOB_LOG_TAIL_LINES:]
    out = [_RULE, "Failed Module: Job", _RULE, "", f"Job result: {result} (state {job.get('state', 'unknown')})"]
    if reason:
        out.append(f"Reason: {redact(reason)}")
    if excerpt:
        out += ["", "--- autoinst-log.txt (relevant lines) ---", *map(redact, excerpt)]
        out.append("--- End of Log Section ---")
    return [*out, ""]
