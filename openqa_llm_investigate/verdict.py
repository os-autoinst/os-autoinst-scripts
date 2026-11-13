# Copyright SUSE LLC
"""Read the BISECT and Restart verdicts and override them where the job's history rules them out."""

from __future__ import annotations

import re

BISECT_RE = re.compile(r"^(?:[\s*_`>#-]|\d+\.)*BISECT[*_`]*:[\s*_`]*(YES|NO)(?![a-z0-9])", re.IGNORECASE)
_RESTART_RE = re.compile(r"^(?:[\s*_`>#-]|\d+\.)*Restart[*_`]*:[\s*_`]*(YES|NO)(?![a-z0-9])", re.IGNORECASE)
_BISECT_STREAK = 5
_RESTART_STREAK = 3


def _last_answer(verdict: str, pattern: re.Pattern[str]) -> bool | None:
    for ln in reversed(verdict.splitlines()):
        if m := pattern.match(ln):
            return m.group(1).upper() == "YES"
    return None


def bisect_verdict(verdict: str) -> bool | None:
    """Return whether the last `BISECT: YES|NO` line of the verdict is YES, or None if it has none."""
    return _last_answer(verdict, BISECT_RE)


def restart_verdict(verdict: str) -> bool | None:
    """Return whether the last `Restart: YES|NO` line of the verdict is YES, or None if it has none."""
    return _last_answer(verdict, _RESTART_RE)


def _yes_to_no(verdict: str, pattern: re.Pattern[str], no: str) -> str:
    lines = verdict.splitlines()
    return "\n".join(no if (m := pattern.match(ln)) and m.group(1).upper() == "YES" else ln for ln in lines)


def enforce_history_rules(verdict: str, streak: int | None) -> str:
    """Rewrite a BISECT YES to NO after 5 failures in a row and a Restart YES after 3; small models answer YES anyway.

    `streak` is the failures in a row up to the job, None if the job did not fail or is not in the fetched history.
    After such a streak a failure is rarely new, so a restart rarely helps, and a bisection only if it keeps failing.
    """
    no = f"NO \u2014 failed at least {streak} times in a row"
    if (streak or 0) >= _BISECT_STREAK:
        verdict = _yes_to_no(verdict, BISECT_RE, f"BISECT: {no}")
    if (streak or 0) >= _RESTART_STREAK:
        verdict = _yes_to_no(verdict, _RESTART_RE, f"Restart: {no}")
    return verdict
