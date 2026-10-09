# Copyright SUSE LLC
"""Summarize a job's metadata and the results of similar previous runs and sibling jobs."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from operator import itemgetter
from urllib.parse import quote

from .failures import build_module_cmd_list, extract_failed_modules
from .http import AnalyzerError, JsonDict, OpenQA
from .text import redact

_HEADER_SETTINGS = ("DISTRI", "VERSION", "FLAVOR", "ARCH", "BUILD", "MACHINE", "BACKEND")
_HEADER_OPTIONAL = ("QEMURAM", "UEFI", "ENCRYPT", "BOOTFROM", "HDD_1", "BOOT_HDD_IMAGE")
_HISTORY_FAILED = ("failed", "incomplete", "timeout_exceeded", "parallel_failed")
_HISTORY_NEIGHBOURS = 5
_HISTORY_MAX_FETCHES = 8
_HISTORY_MAX_GROUPS = 6
_COMMAND_MAX_CHARS = 80
_BY_ID = itemgetter("id")
_BEST_EFFORT_ERRORS = (AnalyzerError, AttributeError, KeyError, TypeError, ValueError)


@dataclass
class _Lookups:
    """Server access plus the shared budget of per-job detail fetches."""

    oqa: OpenQA
    fetched: int = 0


def fetch_job(oqa: OpenQA, job_id: str) -> JsonDict:
    """Return the job from the API ({} when it cannot be fetched)."""
    try:
        return oqa.json(f"/api/v1/jobs/{job_id}").get("job", {})
    except (AnalyzerError, AttributeError):
        return {}


def job_header(job: JsonDict, details_json: JsonDict) -> list[str]:
    """Return test/result/reason, module counts and key settings lines (empty if unknown)."""
    settings = job.get("settings") or {}
    lines = []
    if test := job.get("test") or settings.get("TEST"):
        lines.append(f"Test: {test}")
    if job.get("result"):
        lines.append(f"Result: {job['result']} (state {job.get('state', 'unknown')})")
    if job.get("reason"):
        lines.append(f"Reason: {job['reason']}")
    if settings.get("JOB_DESCRIPTION"):
        lines.append(f"Description: {settings['JOB_DESCRIPTION']}")
    if shown := [f"{k}={settings[k]}" for k in (*_HEADER_SETTINGS, *_HEADER_OPTIONAL) if settings.get(k)]:
        lines.append("Settings: " + " ".join(shown))
    if results := [m.get("result") for m in details_json.get("modules", [])]:
        lines.append("Modules: " + " / ".join(f"{results.count(r)} {r}" for r in ("failed", "softfailed", "skipped")))
    return [*map(redact, lines), ""] if lines else []


def _failed_commands(oqa: OpenQA, job_id: int | str, limit: int = 2) -> list[str]:
    """Return the first failed commands of a job from its details (best-effort, [] on error)."""
    try:
        details = oqa.json(f"/tests/{job_id}/details_ajax")
        cmds = [cmd for m in extract_failed_modules(details) for cmd, _ in build_module_cmd_list(m.get("details", []))]
    except (AnalyzerError, AttributeError, TypeError):
        return []
    cmds = [" ".join(c.split()) for c in cmds[:limit]]
    return [c if len(c) <= _COMMAND_MAX_CHARS else c[: _COMMAND_MAX_CHARS - 1] + "…" for c in cmds]


def _failure_groups(lookups: _Lookups, jobs: list[JsonDict], *, with_test: bool = False) -> str:
    """Describe failed jobs, identical failures collapsed to 'Nx'.

    Commands come from the latest job of a group, while the shared fetch budget lasts.
    """
    groups: dict[tuple[bool, str, str, tuple[str, ...]], list[JsonDict]] = {}
    for j in jobs:
        key = (bool(j.get("same_flavor")), j["test"] if with_test else "", j["result"], tuple(j["modules"]))
        groups.setdefault(key, []).append(j)
    # same-flavor kinds first, so they win the group cap and the fetch budget
    ranked = sorted(groups.items(), key=lambda kv: (not kv[0][0], -len(kv[1])))[:_HISTORY_MAX_GROUPS]
    latest = [max(js, key=_BY_ID) for _, js in ranked]
    to_fetch = [j["id"] for (key, _), j in zip(ranked, latest, strict=True) if key[3]]
    to_fetch = to_fetch[: max(_HISTORY_MAX_FETCHES - lookups.fetched, 0)]
    lookups.fetched += len(to_fetch)
    with ThreadPoolExecutor(max_workers=_HISTORY_MAX_FETCHES) as pool:
        commands = dict(zip(to_fetch, pool.map(lambda i: _failed_commands(lookups.oqa, i), to_fetch), strict=True))
    parts = []
    for ((_, test, result, mods), js), last in zip(ranked, latest, strict=True):
        text = (f"{len(js)}x " if len(js) > 1 else "") + " ".join(filter(None, [test, result]))
        if mods:
            text += f" in {', '.join(mods)}"
            if cmds := commands.get(last["id"]):
                text += " (" + "; ".join(f"`{c}`" for c in cmds) + ")"
        build = f", build {last['build']}" if last.get("build") else ""
        parts.append(f"{text} [job {last['id']}{build}]")
    if len(groups) > _HISTORY_MAX_GROUPS:
        parts.append(f"+{len(groups) - _HISTORY_MAX_GROUPS} more")
    return "; ".join(parts)


def _runs_text(label: str, runs: list[JsonDict], lookups: _Lookups) -> str:
    ok = Counter(j["result"] for j in runs if j["result"] not in _HISTORY_FAILED)
    parts = [f"{n} {r}" for r, n in ok.items()]
    failed = [
        {
            "id": j["id"],
            "result": j["result"],
            "test": "",
            "build": j.get("build"),
            "modules": j.get("failedmodules") or [],
        }
        for j in runs
        if j["result"] in _HISTORY_FAILED
    ]
    if failed:
        parts.append(_failure_groups(lookups, failed))
    return f"{label} {len(runs)} run(s): {', '.join(parts)}"


def _neighbour_summary(lookups: _Lookups, job_id: str) -> str | None:
    """Return the result of the previous/next runs shown in openQA's 'Next & previous results'."""
    data = lookups.oqa.json(f"/tests/{job_id}/ajax")["data"]
    cur = int(job_id)
    done = {j["id"]: j for j in data if j.get("state") == "done" and j["id"] != cur}
    prev = sorted((j for i, j in done.items() if i < cur), key=_BY_ID, reverse=True)
    nxt = sorted((j for i, j in done.items() if i > cur), key=_BY_ID)
    parts = [
        _runs_text(label, runs[:_HISTORY_NEIGHBOURS], lookups)
        for label, runs in (("previous", prev), ("next", nxt))
        if runs
    ]
    return "Previous/next runs: " + "; ".join(parts) + "." if parts else None


def _sibling_failures(others: list[JsonDict], flavor: str | None) -> list[JsonDict]:
    return [
        {
            "id": j["id"],
            "result": j["result"],
            "test": j.get("test", ""),
            "same_flavor": bool(flavor) and (j.get("settings") or {}).get("FLAVOR") == flavor,
            "modules": [m["name"] for m in j.get("modules", []) if m.get("result") == "failed"],
        }
        for j in others
        if j["result"] in _HISTORY_FAILED
    ]


def _sibling_summary(lookups: _Lookups, job_id: str, job: JsonDict) -> str | None:
    """Return the result of the other jobs of the same job group and build.

    Failures of the job's own FLAVOR are listed (and get commands) first. Filtered by VERSION
    too, as groups with per-version build counters reuse BUILD values.
    """
    settings = job.get("settings") or {}
    gid, build, flavor = job.get("group_id"), settings.get("BUILD"), settings.get("FLAVOR")
    if not gid or not build:
        return None
    version = f"&version={quote(str(settings['VERSION']))}" if settings.get("VERSION") else ""
    query = f"groupid={gid}&build={quote(str(build))}{version}&latest=1&limit=500"
    jobs = lookups.oqa.json(f"/api/v1/jobs?{query}")["jobs"]
    if not (others := [j for j in jobs if str(j["id"]) != str(job_id)]):
        return None
    failed = _sibling_failures(others, flavor)
    ok = sum(1 for j in others if j["result"] in {"passed", "softfailed"})
    pending = sum(1 for j in others if j["result"] == "none")
    where = f"in group {gid} build {build}"
    rest = f"{ok} passed/softfailed" + (f", {pending} not finished" if pending else "")
    if failed:
        groups = _failure_groups(lookups, failed, with_test=True)
        n_same = sum(j["same_flavor"] for j in failed)
        same = f" ({n_same} in flavor {flavor})" if n_same else ""
        return f"Same group+build: {len(failed)} of {len(others)} other jobs {where} failed{same}: {groups}; {rest}."
    if ok == len(others):
        return f"Same group+build: everything else succeeded, all {ok} other jobs {where} passed or softfailed."
    return f"Same group+build: no other job {where} failed ({rest})."


def run_history(oqa: OpenQA, job_id: str, job: JsonDict) -> list[str]:
    """Return up to two sentences on similar previous runs and sibling jobs.

    Best-effort: a section whose data cannot be fetched or parsed is omitted.
    """
    lookups = _Lookups(oqa)
    lines = []
    for summarize in (lambda: _neighbour_summary(lookups, job_id), lambda: _sibling_summary(lookups, job_id, job)):
        try:
            if line := summarize():
                lines.append(redact(line))
        except _BEST_EFFORT_ERRORS:
            continue
    return [*lines, ""] if lines else []
