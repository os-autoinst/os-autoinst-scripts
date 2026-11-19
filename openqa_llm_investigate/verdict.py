# Copyright SUSE LLC
"""Read the BISECT verdict and override BISECT and Restart where the job's history rules them out."""

from __future__ import annotations

import re

_MARKUP = r"(?:[\s*_`>#-]|\d+\.)*"
_CANONICAL = {"classification": "Classification", "bisect": "BISECT", "restart": "Restart"}
_LABELS = "|".join(_CANONICAL.values())
BISECT_RE = re.compile(rf"^{_MARKUP}BISECT[*_`]*:[\s*_`]*(YES|NO)(?![a-z0-9])", re.IGNORECASE)
BISECT_LABEL_RE = re.compile(rf"^{_MARKUP}BISECT[*_`]*:", re.IGNORECASE)
_RESTART_RE = re.compile(rf"^{_MARKUP}Restart[*_`]*:[\s*_`]*(YES|NO)(?![a-z0-9])", re.IGNORECASE)
_LABEL_RE = re.compile(rf"(?:^|(?<=\s)){_MARKUP}({_LABELS})(?![a-z0-9])[*_`]*:[ \t]*", re.IGNORECASE)
_QUOTED_LABEL_RE = re.compile(r":\s*[*_`]*$")
# the choice lists the verdict question asks with, so an echoed list is told apart from a chosen answer
_CHOICES = r"(?:YES|NO|Product bug|Test issue|Infrastructure problem)"
_CHOICE_ECHO_RE = re.compile(rf"\s*{_CHOICES}\s*(?:\||/|\bor\b)\s*{_CHOICES}(?![a-z0-9])", re.IGNORECASE)
# strip emphasis only at a word edge, so an underscore, asterisk or code span of a reason survives
_EMPHASIS_RE = re.compile(r"(?<![\w*_])[*_]+|[*_]+(?![\w*_])")
_DETERMINISTIC_STREAK = 3
_BISECT_NO = "BISECT: NO \u2014 {reason}, so not a new regression"
_RESTART_NO = "Restart: NO \u2014 {reason}, so a rerun is likely to fail"


def _counts_as_answer(reply: str, m: re.Match[str], accepted: list[re.Match[str]]) -> bool:
    """Return whether the label answers rather than quotes, since prose before it on its line is no answer."""
    label_at = reply.rindex(m.group(1), m.start(), m.end())
    line_start = reply.rfind("\n", 0, label_at) + 1
    if _QUOTED_LABEL_RE.search(reply[line_start:label_at]):
        return False
    if re.fullmatch(_MARKUP, reply[line_start:label_at]):
        return True
    same_line = [a for a in accepted if a.end() >= line_start and reply.find("\n", a.end(), label_at) < 0]
    return bool(same_line) and m.group(1).lower() not in {a.group(1).lower() for a in same_line}


def normalize_verdict(reply: str) -> str:
    """Return the canonical Classification, BISECT and Restart lines of the reply, one line per label.

    Small models merge the three lines into one, wrap the labels in markup and echo the choice list of the prompt, so
    the shape cannot be trusted. A label counts when it starts a line or follows another label's answer on that line,
    so `Other jobs: BISECT: YES would be wrong` is skipped; its answer ends at the next label or at the newline, so a
    signature is dropped; the last answer wins; and an echoed `YES | NO` counts as no answer.
    """
    labels: list[re.Match[str]] = []
    found = list(_LABEL_RE.finditer(reply))
    for m in found:
        if _counts_as_answer(reply, m, labels):
            labels.append(m)
    answers: dict[str, str] = {}
    for m in labels:
        end = min((f.start() for f in found if f.start() > m.end()), default=len(reply))
        newline = reply.find("\n", m.end(), end)
        answer = _EMPHASIS_RE.sub("", reply[m.end() : newline if newline >= 0 else end]).strip()
        answers[_CANONICAL[m.group(1).lower()]] = "" if _CHOICE_ECHO_RE.match(answer) else answer.strip()
    return "\n".join(f"{label}: {answers[label]}" for label in _CANONICAL.values() if label in answers) or reply


def bisect_verdict(verdict: str) -> bool | None:
    """Return whether the last `BISECT: YES|NO` line of the verdict is YES, or None if it has none."""
    for ln in reversed(verdict.splitlines()):
        if m := BISECT_RE.match(ln):
            return m.group(1).upper() == "YES"
    return None


def _yes_to_no(verdict: str, pattern: re.Pattern[str], no: str) -> str:
    lines = verdict.splitlines()
    return "\n".join(no if (m := pattern.match(ln)) and m.group(1).upper() == "YES" else ln for ln in lines)


def enforce_history_rules(verdict: str, streak: int | None) -> str:
    """Rewrite a BISECT or Restart YES to NO after a streak of failures; small models answer YES despite the streak.

    `streak` is the failures in a row up to the job, None if the job did not fail or is not in the fetched history.
    After such a streak a failure is rarely new, so neither a bisection nor a restart is likely to help. The reason
    is phrased per verdict because a reader who sees the same sentence twice cannot tell which verdict it justifies.
    """
    if (streak or 0) < _DETERMINISTIC_STREAK:
        return verdict
    reason = f"failed at least {streak} times in a row"
    verdict = _yes_to_no(verdict, BISECT_RE, _BISECT_NO.format(reason=reason))
    return _yes_to_no(verdict, _RESTART_RE, _RESTART_NO.format(reason=reason))
