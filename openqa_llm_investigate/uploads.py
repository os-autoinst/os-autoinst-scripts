# Copyright SUSE LLC
"""Select and excerpt files a job uploaded (journals, widget dumps, consoles, cloudregister)."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import TYPE_CHECKING

from .text import ESC_RE, redact

if TYPE_CHECKING:
    from .http import OpenQA

_UPLOAD_MAX_LINES = 30
_UPLOAD_MAX_FILES = 4
_CONSOLE_MAX_LINES = 20
_CLOUDREG_MAX_LINES = 15
_LINE_MAX_CHARS = 200
_UPLOAD_TEXT_RE = re.compile(r"(?:\.txt|\.log|-widgets\.json|^[^.]*)$")
_JOURNAL_SIGNAL_RE = re.compile(r"\b(?:error|failed|denied|denies|avc|segfault|fatal|panic|oops)\b", re.IGNORECASE)
_BOX_RE = re.compile(r"^[\s│╷╵]+")
_SKIP_UPLOAD_RE = re.compile(r"console|zypper\.log|rpm[-_](?:qa|list)")
_NORM_PREFIX_RES = (
    re.compile(r"^\w{3} \d\d [\d:.]+ \S+ "),
    re.compile(r"^\[[\s\d.]+\]\s*"),
    re.compile(r"^\d{4}-\d\d-\d\d [\d:,.]+:? "),
)
_CONSOLE_SIGNAL_RE = re.compile(
    r"Failed to start|Dependency failed|\[FAILED\]|[Ee]mergency mode|segfault|\bdenied\b"
    r"|Out of memory|I/O error|cloud-init.*(?:ERROR|WARNING|Traceback)|Traceback \(most recent"
)
_CONSOLE_BENIGN_RE = re.compile(
    r"Failed to populate /etc|GPT: Use GNU Parted|NUMA: Failed to initiali[sz]e|no suitable video mode"
)
_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]+\t")
_CLOUDREG_SIGNAL_RE = re.compile(r"ERROR|WARNING|Traceback|[Ee]xception")
_FILE_LINK_RE = re.compile(r'/file/([^"\'?#]+)')


def select_uploaded_logs(names: list[str], module: str) -> list[str]:
    """Pick uploaded text files relevant to a module: `<module>-*`/`<module>_*` and full_journal.txt.

    Only .txt/.log, -widgets.json and extension-less files (e.g. terraform output) qualify.
    """
    picked: list[str] = []
    for n in names:
        if n in picked or not _UPLOAD_TEXT_RE.search(n) or _SKIP_UPLOAD_RE.search(n):
            continue
        if n == "full_journal.txt" or n.startswith((f"{module}-", f"{module}_")):
            picked.append(n)
    return picked[:_UPLOAD_MAX_FILES]


def _widget_strings(node: object, out: list[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k in {"title", "text", "label"} and isinstance(v, str) and v.strip():
                if (k, v) not in out:
                    out.append((k, v.strip()))
            else:
                _widget_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _widget_strings(v, out)


def summarize_widgets(text: str) -> list[tuple[str, str]]:
    """Extract (title|text|label, value) pairs from a *-widgets.json dump ([] if unparsable)."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    _widget_strings(data, out)
    return out


def _dedupe_lines(lines: list[str], cap: int) -> list[str]:
    """Deduplicate lines ignoring timestamps/numbers as `line (xN)`, keeping the last `cap`."""
    first: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for ln in lines:
        key = ln
        for rx in _NORM_PREFIX_RES:
            key = rx.sub("", key)
        key = re.sub(r"[0-9]+", "N", key)
        first.setdefault(key, ln)
        counts[key] += 1
    return [first[k] if n == 1 else f"{first[k]} (x{n})" for k, n in counts.items()][-cap:]


def _journal_excerpt(text: str) -> list[str]:
    """Return failure-signal journal lines, deduplicated with (xN) counts."""
    lines = [ESC_RE.sub("", ln).rstrip()[:_LINE_MAX_CHARS] for ln in text.split("\n")]
    return _dedupe_lines([ln for ln in lines if _JOURNAL_SIGNAL_RE.search(ln)], _UPLOAD_MAX_LINES)


def select_console_files(names: list[str]) -> list[str]:
    """Return console files to scan: newest `*console-end.txt` (else destroy-console.txt) + `*console-beginning.txt`."""
    end = sorted(n for n in names if n.endswith("console-end.txt"))
    main = end[-1:] or [n for n in names if n.endswith("destroy-console.txt")][:1]
    return main + [n for n in names if n.endswith("console-beginning.txt")][:1]


def console_warnings(text: str) -> list[str]:
    """Return error-level lines of a terminal log, minus known benign ones, deduplicated."""
    lines = [_INSTANCE_ID_RE.sub("", ESC_RE.sub("", ln)).rstrip()[:_LINE_MAX_CHARS] for ln in text.split("\n")]
    hits = [ln for ln in lines if _CONSOLE_SIGNAL_RE.search(ln) and not _CONSOLE_BENIGN_RE.search(ln)]
    return _dedupe_lines(hits, _CONSOLE_MAX_LINES)


def list_job_files(oqa: OpenQA, job_id: str) -> list[str]:
    """Return the names of the job's result/uploaded files from downloads_ajax ([] on failure)."""
    return list(dict.fromkeys(_FILE_LINK_RE.findall(oqa.text_or_empty(f"/tests/{job_id}/downloads_ajax"))))


def collect_job_level_logs(
    oqa: OpenQA, job_id: str, names: list[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return console warnings and cloudregister.txt excerpts as ({console: lines}, {file: lines})."""
    consoles = {
        n: warnings for n in select_console_files(names) if (warnings := console_warnings(oqa.job_file(job_id, n)))
    }
    others: dict[str, list[str]] = {}
    if "cloudregister.txt" in names:
        text = oqa.job_file(job_id, "cloudregister.txt")
        if hits := [ln.rstrip()[:_LINE_MAX_CHARS] for ln in text.split("\n") if _CLOUDREG_SIGNAL_RE.search(ln)]:
            others["cloudregister.txt"] = _dedupe_lines(hits, _CLOUDREG_MAX_LINES)
    return consoles, others


def _log_excerpt(name: str, text: str) -> list[str]:
    """Return the failure-signal lines of an uploaded file with one line of context ([] without signal)."""
    if name == "full_journal.txt":
        return _journal_excerpt(text)
    lines = [_BOX_RE.sub("", ESC_RE.sub("", ln)).rstrip()[:_LINE_MAX_CHARS] for ln in text.split("\n")]
    keep = sorted({j for i, ln in enumerate(lines) if _JOURNAL_SIGNAL_RE.search(ln) for j in (i - 1, i, i + 1)})
    return [lines[j] for j in keep if 0 <= j < len(lines) and lines[j].strip()][-_UPLOAD_MAX_LINES:]


def collect_uploaded_logs(oqa: OpenQA, job_id: str, names: list[str], module: str) -> dict[str, list[str]]:
    """Fetch the module's relevant uploaded files and return {name: excerpt lines} (silent on failure)."""
    result: dict[str, list[str]] = {}
    for n in select_uploaded_logs(names, module):
        text = oqa.job_file(job_id, n)
        if n.endswith("-widgets.json"):
            if pairs := summarize_widgets(text):
                result[n] = [f"{k}: {v}" for k, v in pairs][:_UPLOAD_MAX_LINES]
        elif text.strip() and (excerpt := _log_excerpt(n, text)):
            result[n] = excerpt
    return result


def format_uploaded_logs(logs: dict[str, list[str]], label: str = "uploaded log") -> list[str]:
    """Return report lines for {name: excerpt} sections."""
    out: list[str] = []
    for name, lines in logs.items():
        out += [f"--- {label}: {name} ---", *map(redact, lines), "--- End of Log Section ---", ""]
    return out
