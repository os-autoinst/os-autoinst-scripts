# Copyright SUSE LLC
"""Pure text helpers for parsing and condensing openQA step texts and logs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .http import JsonDict


# openQA script-runner exit-code echo suffix, unexpanded ($?-) or expanded (-3-)
_SCRIPT_FINISHED_RE = re.compile(r";\s*echo SCRIPT_FINISHED\w*-[^;\"'\s]*-")


_MASK = "****"
_SENSITIVE_NAMES = (
    r"password|passwd|secret|token|api[_-]?key|access[_-]key|client[_-]secret|account[_-]?key|reg[_-]?code(?:_\w+)?"
)
_REDACTIONS = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
        "[private key]",
    ),
    (re.compile(r"INTERNAL-USE-ONLY-[0-9a-fA-F]{4}(?:-[0-9a-fA-F]{4}|[0-9a-fA-F]{12})"), "INTERNAL-USE-ONLY-****-****"),
    (
        re.compile(r"([?&](?:credentials|sig|X-Amz-Signature|X-Amz-Credential)=)[^&\s'\"]+", re.IGNORECASE),
        rf"\1{_MASK}",
    ),
    (re.compile(r"\b(Authorization:\s*(?:Bearer|Basic|token)\s+)\S+", re.IGNORECASE), rf"\1{_MASK}"),
    (re.compile(r"\b(Bearer\s+)[\w.~+/=-]{8,}"), rf"\1{_MASK}"),
    (re.compile(r"(\b[a-z][\w+.-]*://)[^/\s:@'\"]+:[^/\s@'\"]+@", re.IGNORECASE), rf"\1{_MASK}@"),
    (
        re.compile(rf"((?<![/A-Za-z0-9-])(?:{_SENSITIVE_NAMES})['\"]?\s*[=:]\s*['\"]?)[^\s'\",;&]+", re.IGNORECASE),
        rf"\1{_MASK}",
    ),
    (re.compile(rf"(--(?:{_SENSITIVE_NAMES})[=\s]['\"]?)[^\s'\",;&]+", re.IGNORECASE), rf"\1{_MASK}"),
    (
        re.compile(
            r"(\b(?:SUSEConnect|registercloudguest)\b[^;&|\n]*?\s-r\s+)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|`'\")]+)"
        ),
        rf"\1{_MASK}",
    ),
    (
        re.compile(r"(\bcurl\b[^;&|\n]*?\s(?:-u\s*|--user[=\s]))(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|`'\")]+)"),
        rf"\1{_MASK}",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA" + _MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh_" + _MASK),
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), "[jwt]"),
    (_SCRIPT_FINISHED_RE, ""),
)


def redact(text: str) -> str:
    """Mask credentials, tokens, keys and registration codes; strip openQA internal suffixes."""
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# Script-runner wrapper "echo TOKEN; bash [opts] /tmp/..."; \S+ also catches tokens like ~
_BOILERPLATE_RE = re.compile(r"echo \S+;\s*bash\b.*\s/tmp/\S+")


def is_boilerplate_log_line(line: str) -> bool:
    """Return True when a serial-log line is an openQA script-runner wrapper command.

    openQA physically types the wrapper command into the terminal when it runs a
    test script, so lines like

        echo tY~XN; bash -oe pipefail /tmp/scripttY~HOST.sh ; echo SCRIPT_FINISHED…

    appear verbatim in the serial terminal log.  They contain no diagnostic value
    and confuse the LLM into treating the wrapper as the failing command.

    Uses re.search (not match) so lines with a leading timestamp are also caught.
    """
    return bool(_BOILERPLATE_RE.search(line))


ESC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?=!]*[A-Za-z]")
_OA_MARKER_RE = re.compile(r"^(?=.*OA:)[\w:~-]+$")
_LOG_TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\dT[\d:.]+Z)\]")
_NEEDLE_NOMATCH_RE = re.compile(r"no match: [\d.]+s, best candidate: (\S+) \(([\d.]+)\)")
_NEEDLE_NOCHANGE_RE = re.compile(r"no change: [\d.]+s")
_NEEDLE_SLOW_RE = re.compile(r"check_asserted_screen took [\d.]+ seconds")
_KEEP_DEBUG_RE = re.compile(
    r"record_info|died|_handle_found_needle|<<< testapi::(?:assert_script_run|"
    r"script_run|script_output|assert_screen|check_screen|assert_and_click|"
    r"send_key|enter_cmd|select_console)\b"
)


_NOISE_FRAME_RE = re.compile(
    r"/usr/lib/(?:os-autoinst|perl5)/|Mojo::IOLoop|\b(?:autotest|basetest)::"
    r"|OpenQA::Isotovideo"
)
_ZYPP_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d <\d> ")
_ZYPP_THROW_RE = re.compile(r"Exception\.cc\(log\):\d+ \S+ THROW:\s+(.*)")
_URL_RE = re.compile(r"https?://([^/\s'\")]+)\S*")
_DIED_MAX_LINES = 40


_STACK_FRAMES_SHOWN = 5


def trim_stack_frames(lines: list[str], keep: int = _STACK_FRAMES_SHOWN) -> list[str]:
    """Keep the first `keep` lines of each run of consecutive `called at` frames."""
    out: list[str] = []
    run = 0
    skipped: list[str] = []

    def flush() -> None:
        if skipped:
            indent = skipped[0][: len(skipped[0]) - len(skipped[0].lstrip())]
            out.append(f"{indent}... ({len(skipped)} more stack frames)")
            skipped.clear()

    for ln in lines:
        if " called at " in ln:
            run += 1
            if run > keep:
                skipped.append(ln)
                continue
        else:
            flush()
            run = 0
        out.append(ln)
    flush()
    return out


class _NeedlePolls:
    """Counts needle-poll log lines so they collapse into one summary."""

    def __init__(self) -> None:
        """Start with no polls seen."""
        self.no_match = 0
        self.same = 0
        self.slow = 0
        self.stamps: list[str] = []
        self.best = ("", -1.0)

    def add(self, line: str) -> bool:
        """Count `line` and return True when it is a needle-poll line."""
        no_match = _NEEDLE_NOMATCH_RE.search(line)
        same = _NEEDLE_NOCHANGE_RE.search(line)
        if not (no_match or same or _NEEDLE_SLOW_RE.search(line)):
            return False
        if stamp := _LOG_TS_RE.match(line):
            self.stamps.append(stamp.group(1))
        if no_match:
            self.no_match += 1
            score = float(no_match.group(2))
            if score > self.best[1]:
                self.best = (no_match.group(1), score)
        elif same:
            self.same += 1
        else:
            self.slow += 1
        return True

    def summary(self) -> str:
        """Render the collapsed polls as one line."""
        span = f" {self.stamps[0]} .. {self.stamps[-1]}," if self.stamps else ""
        best = f" best candidate {self.best[0]} ({self.best[1]:.2f})" if self.best[0] else ""
        extra = f", {self.same} unchanged-screen polls" if self.same else ""
        extra += f", {self.slow} slow-check warnings" if self.slow else ""
        return f"[needle search: {self.no_match} no-match polls,{span}{best}{extra}]"


def _is_noise_line(line: str) -> bool:
    """Return True for terminal markers, websocket events and framework backtrace frames."""
    return bool(_OA_MARKER_RE.match(line.strip())) or _is_framework_line(line)


def _is_droppable_debug(line: str) -> bool:
    """Return True for autoinst [debug] lines that carry no failure signal."""
    return "serial_screen::read_until: Matched" in line or ("[debug]" in line and not _KEEP_DEBUG_RE.search(line))


def _keep_tail(lines: list[str], max_lines: int | None) -> list[str]:
    """Keep the last `max_lines` lines behind an omission marker."""
    if max_lines is None or len(lines) <= max_lines:
        return lines
    return [f"[... {len(lines) - max_lines} earlier lines omitted ...]", *(lines[-max_lines:] if max_lines > 0 else [])]


def condense_log(lines: list[str], *, autoinst: bool = False, max_lines: int | None = None) -> list[str]:
    """Drop log noise: escapes, OA markers, EVENT lines, needle-poll streams.

    Needle `no match`/`no change`/slow-check lines collapse into one summary at
    the first occurrence. With autoinst, [debug] lines (and their continuation
    lines) are dropped unless they carry signal. max_lines keeps the tail.
    """
    out: list[str] = []
    polls = _NeedlePolls()
    poll_at = -1
    dropping = False
    for raw in lines:
        line = ESC_RE.sub("", raw)
        if _is_noise_line(line):
            continue
        stamped = bool(_LOG_TS_RE.match(line))
        if autoinst and not stamped and dropping:
            continue
        if autoinst and stamped:
            dropping = False
        if polls.add(line):
            if poll_at < 0:
                poll_at = len(out)
                out.append("")
            continue
        if autoinst and stamped and _is_droppable_debug(line):
            dropping = True
            continue
        out.append(line)
    if poll_at >= 0:
        out[poll_at] = polls.summary()
    return trim_stack_frames(_keep_tail(out, max_lines))


_QR_PREFIX_RE = re.compile(r"qr/([^(]*)\(")
_WRAPPER_RE = re.compile(r"echo \w+;\s*bash\s+.*/tmp/script\S+\.sh\s*$")


def _command_of(line: str) -> str | None:
    """Return the command of a `# Command:` line without its `; echo` suffix; None for other lines."""
    if not line.startswith("# Command:"):
        return None
    return _SCRIPT_FINISHED_RE.sub("", line.replace("# Command:", "")).strip()


def _expected_command(expected: str) -> str:
    """Return the literal command from a `# wait_serial expected:` value."""
    if qr := _QR_PREFIX_RE.match(expected):
        return qr.group(1)
    expected = _SCRIPT_FINISHED_RE.sub("", expected.strip("\"'"))
    return expected.replace("\\$", "$").replace("\\\\", "\\")


def parse_command_from_text_data(text_data: str) -> str | None:
    """Parse the actual command from a step's text_data."""
    lines = text_data.split("\n")
    for line in lines:
        cmd = _command_of(line)
        if cmd is not None and not _BOILERPLATE_RE.match(cmd):
            return cmd
    for line in lines:
        if line.startswith("# wait_serial expected:"):
            return _expected_command(line.replace("# wait_serial expected:", "").strip())
    return None


def filter_text_data_lines(text_data: str) -> str:
    """Remove openQA script-runner boilerplate # Command: lines from text_data before display."""
    kept = []
    for line in ESC_RE.sub("", text_data).split("\n"):
        cmd = _command_of(line)
        if cmd is None or not _BOILERPLATE_RE.match(cmd):
            kept.append(line)
    return "\n".join(kept)


def parse_exit_code_from_text_data(text_data: str) -> str | None:
    """Parse exit code from text_data field (# Exit code: N line)."""
    for line in text_data.split("\n"):
        if line.startswith("# Exit code:"):
            return line.replace("# Exit code:", "").strip()
    return None


def _died_quoted_command(text: str) -> str | None:
    """P1: command 'X' timed out / failed."""
    match = re.search(r"command '([^']+)' (?:timed out|failed)", text)
    return match.group(1) if match else None


def _died_godot(text: str) -> str | None:
    """P2: Waiting for Godot: CMD at FILE; an ssh `$'...'` wrapper yields its inner command."""
    match = re.search(r"Waiting for Godot:\s+(.+?)\s+at\s+", text)
    if not match:
        return None
    inner = re.search(r"\$'([^']+)'", match.group(1))
    return (inner.group(1) if inner else match.group(1))[:200]


def _died_returned_with(text: str) -> str | None:
    """P3: FUNC returned with N, preferring the Perl call's quoted arguments."""
    match = re.search(r"([a-z0-9_-]+) returned with \d+", text, re.IGNORECASE)
    if not match:
        return None
    func = re.search(r'::\w+\("([^"]+)"\)', text) or re.search(r"::\w+\('([^']+)'\)", text)
    return func.group(1)[:200] if func else match.group(1)


def _died_needle(text: str) -> str | None:
    """P4: the full "no candidate needle with tag(s) ..." phrase, for log search."""
    match = re.search(r"no candidate needle with tag\(s\) '[^']+'", text)
    return match.group(0) if match else None


def _died_call(text: str) -> str | None:
    """P5: Perl / testapi function call on the `# Test died:` line itself."""
    match = re.search(r"# Test died:.*?([a-zA-Z_]\w*(?:::[a-zA-Z_]\w*)*)\s*\(([^)]*)\)", text)
    if not match:
        return None
    args = match.group(2).strip()
    return f"{match.group(1)}({args[:120]})" if args else match.group(1)


_DIED_PARSERS = (_died_quoted_command, _died_godot, _died_returned_with, _died_needle, _died_call)


def parse_command_from_test_died(text_data: str) -> str | None:
    """Parse the command from 'Test died' error messages, trying five patterns in priority order."""
    return next((cmd for parse in _DIED_PARSERS if (cmd := parse(text_data))), None)


def ipa_search_key(name: str) -> str:
    """Derive a serial-log search key from an IPA module name.

    IPA names look like ``SLES_Azure_test_sles_azure_csp_cli`` while the pytest
    output only has the ``test_*`` part, so drop everything before it.
    """
    idx = name.find("test_")
    return name[idx:] if idx > 0 else name


def _step_fields(text_data: str) -> list[str]:
    """`command:`, `marker:` and `exit code:` parts found in a step's text_data."""
    command = marker = exit_code = None
    for line in text_data.split("\n"):
        if (cmd := _command_of(line)) is not None and command is None:
            command = None if _BOILERPLATE_RE.match(cmd) else cmd
        elif line.startswith("# wait_serial expected:") and marker is None:
            marker = line.replace("# wait_serial expected:", "").strip()
        elif line.startswith("# Exit code:") and exit_code is None:
            exit_code = line.replace("# Exit code:", "").strip()
    fields = (("command", command), ("marker", marker), ("exit code", exit_code))
    return [f"{name}: {value}" for name, value in fields if value is not None]


def format_step_summary_line(detail: JsonDict) -> str:
    """Format a step as `#N [FAIL]  title  command: C  marker: M  exit code: E`.

    Without those fields the first text line (120 chars) or the screenshot name is shown.
    Registration codes are not masked here.
    """
    result = detail.get("result", "")
    title = detail.get("display_title", "")
    text_data = detail.get("text_data", "")
    screenshot = detail.get("screenshot", "")
    prefix = f"#{detail.get('num', '?')}{' [FAIL]' if result == 'fail' else ''}{f'  {title}' if title else ''}"
    if fields := _step_fields(text_data) if text_data else []:
        return f"{prefix}  {'  '.join(fields)}"
    if text_data:
        return f"{prefix}  {text_data.split(chr(10))[0][:120]}"
    return f"{prefix}  (screenshot: {screenshot})" if screenshot else prefix


def _search_command(lines: list[str]) -> str | None:
    """Return the first `# Command:` of a step unless it is the script-runner wrapper."""
    for line in lines:
        if line.startswith("# Command:"):
            cmd = _SCRIPT_FINISHED_RE.sub("", line.replace("# Command:", "").strip())
            return None if _WRAPPER_RE.match(cmd) else cmd
    return None


def extract_search_string_from_step(detail: JsonDict | None) -> str | None:
    """Extract a string from a step's text_data to look up in autoinst-log.txt.

    Order: `# Command:`, `# wait_serial expected:` (qr prefix or string), the command of a `# Test died:`.
    """
    text_data = (detail or {}).get("text_data", "")
    if not text_data:
        return None
    lines = text_data.split("\n")
    if cmd := _search_command(lines):
        return cmd
    for line in lines:
        if line.startswith("# wait_serial expected:"):
            expected = line.replace("# wait_serial expected:", "").strip()
            qr = _QR_PREFIX_RE.match(expected)
            return qr.group(1) if qr else expected.strip("\"'")
    return parse_command_from_test_died(text_data) if "# Test died:" in text_data else None


def _is_framework_line(line: str) -> bool:
    """Return True for websocket EVENT lines and framework backtrace frames."""
    return line.startswith("EVENT {") or (" called at " in line and bool(_NOISE_FRAME_RE.search(line)))


def _collapse_zypp(text: str) -> list[str]:
    """Drop framework noise; reduce zypper log blocks to their distinct THROW messages with a count."""
    out: list[str] = []
    zypp: dict[str, list[Any]] = {}
    for line in text.split("\n"):
        if _is_framework_line(line):
            continue
        if not _ZYPP_LINE_RE.match(line):
            out.append(line)
        elif m := _ZYPP_THROW_RE.search(line):
            msg = m.group(1).strip()
            key = _URL_RE.sub(lambda u: u.group(1), msg)
            if key in zypp:
                zypp[key][1] += 1
            else:
                zypp[key] = [msg, 1]
                out.append(key)
    return [f"{zypp[ln][0]}" + (f" (x{zypp[ln][1]})" if zypp[ln][1] > 1 else "") if ln in zypp else ln for ln in out]


def _drop_redundant_footer(lines: list[str]) -> list[str]:
    """Drop a trailing `--- # stack trace` footer when its frames are already shown above."""
    if "--- # stack trace" not in lines:
        return lines
    i = lines.index("--- # stack trace")
    tail = [ln for ln in lines[i + 1 :] if ln.strip()]
    rest = "\n".join(lines[:i])
    return lines[:i] if tail and all(ln in rest for ln in tail) else lines


def _squeeze_blank(lines: list[str]) -> list[str]:
    """Collapse blank-line runs, strip trailing spaces and trailing blank lines."""
    out: list[str] = []
    for ln in lines:
        if ln.strip() or (out and out[-1].strip()):
            out.append(ln.rstrip())
    while out and not out[-1].strip():
        out.pop()
    return out


def clean_died_text(text: str, max_lines: int = _DIED_MAX_LINES) -> str:
    """Strip noise from a step's failure text (die message plus backtrace).

    Drops framework backtrace frames, websocket EVENT lines and terminal
    escapes, strips ?credentials= query strings, and collapses zypper log
    blocks to their distinct THROW messages (with a count); caps the result.
    """
    text = re.sub(r"\?credentials=\w+", "", ESC_RE.sub("", text))
    lines = trim_stack_frames(_squeeze_blank(_drop_redundant_footer(_collapse_zypp(text))))
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], f"... ({len(lines) - max_lines} more lines)"]
    return "\n".join(lines)
