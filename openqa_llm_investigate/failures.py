# Copyright SUSE LLC
"""Extract and group the failing steps of openQA test modules."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .text import (
    ESC_RE,
    parse_command_from_test_died,
    parse_command_from_text_data,
    parse_exit_code_from_text_data,
    redact,
)

if TYPE_CHECKING:
    from .http import JsonDict

_XFSTESTS_SUMMARY_TITLES = {"INFO", "output", "out.bad", "full", "dmesg"}
# Testapi functions that match screen needles; their needle tags make a richer command
_TESTAPI_SCREEN_CALLS = frozenset({
    "assert_screen",
    "check_screen",
    "assert_and_click",
    "wait_screen_change",
    "assert_screen_change",
    "wait_still_screen",
    "assert_still_screen",
})
_HOUSEKEEPING_CMD_RE = re.compile(r"^(?:scp\b|rm -rf\b|curl\b.*/uploadlog/|test -d \S+ && rm\b)")
_REPEATS_SHOWN = 5
CLUSTER_MAX = 5
STEP_GROUP_MAX = 10
CLUSTER_STEPS_SHOWN = 8


def _first_fail_num(module: JsonDict) -> float:
    """Return the num of the module's first failing step, infinity when it has none."""
    failed = [d.get("num", 0) for d in module.get("details", []) if d.get("result") == "fail"]
    return min(failed) if failed else float("inf")


def extract_failed_modules(details_json: JsonDict, *, include_softfailed: bool = False) -> list[JsonDict]:
    """Return the failed (optionally also softfailed) modules, earliest failing step first.

    A missing "modules" key (openQA omits it for a job that died before any
    module ran) counts as no modules. Details are sorted by step num.
    """
    results = ("failed", "softfailed") if include_softfailed else ("failed",)
    failed_modules = [m for m in details_json.get("modules", []) if m.get("result") in results]
    for m in failed_modules:
        if "details" in m:
            m["details"] = sorted(m["details"], key=lambda d: d.get("num", 0))
    failed_modules.sort(key=_first_fail_num)
    return failed_modules


def extract_test_died_errors(module: JsonDict) -> list[JsonDict]:
    """Return the details containing '# Test died:' messages."""
    return [d for d in module.get("details", []) if "# Test died:" in (d.get("text_data") or "")]


def extract_xfstests_result_steps(all_details: list[JsonDict]) -> list[JsonDict]:
    """Return the xfstests wrapper summary steps (INFO, output, out.bad, full, dmesg) in order.

    The xfstests openQA wrapper records them at the end of each module with
    the result, the expected/actual diff and optionally dmesg.
    """
    return [
        d for d in all_details if d.get("display_title") in _XFSTESTS_SUMMARY_TITLES and d.get("text_data") is not None
    ]


def xfstests_lines(all_details: list[JsonDict]) -> list[str]:
    """Render the xfstests summary steps; the serial log adds nothing beyond them."""
    steps = extract_xfstests_result_steps(all_details)
    if not steps:
        return []
    lines = ["xfstests result:"]
    for step in steps:
        title = step["display_title"]
        body = (step.get("text_data") or "").strip()
        if not body or body.endswith("not exist"):
            continue
        lines += [body, ""] if title == "INFO" else [f"--- {title} ---", redact(body), ""]
    return lines


def _screen_command(title: str, text_data: str) -> str:
    """Return `title('tags')` for a needle failure, else the bare title."""
    match = re.search(r"[Nn]eedle '([^']+)'", text_data) or re.search(
        r"no candidate needle with tag\(s\) '([^']+)'", text_data
    )
    return f"{title}('{match.group(1)}')" if title in _TESTAPI_SCREEN_CALLS and match else title


def _step_command(detail: JsonDict) -> tuple[str, str | None] | None:
    """Return (command, exit code) for a failing step, None when it has none."""
    text_data = detail.get("text_data", "") or ""
    if "# Test died:" in text_data:
        cmd = parse_command_from_test_died(text_data)
        return (redact(cmd), None) if cmd else None
    if detail.get("display_title") == "wait_serial":
        cmd = parse_command_from_text_data(text_data)
        exit_code = parse_exit_code_from_text_data(text_data)
        if cmd:
            return redact(cmd), exit_code
        return (f"(exit {exit_code})", exit_code) if exit_code is not None else None
    title = detail.get("display_title", "")
    return (_screen_command(title, text_data), None) if title else None


def build_module_cmd_list(
    all_details: list[JsonDict], repeats: dict[str, list[int]] | None = None
) -> list[tuple[str, str | None]]:
    """Return (command, exit code) per distinct failing-step command, in first-seen order.

    `repeats`, when given, is filled with command -> nums of every failing step it stands for.
    """
    seen: set[str] = set()
    entries: list[tuple[str, str | None]] = []
    reps = repeats if repeats is not None else {}
    for detail in all_details:
        entry = _step_command(detail) if detail.get("result") == "fail" else None
        if entry is None:
            continue
        reps.setdefault(entry[0], []).append(detail.get("num", 0))
        if entry[0] not in seen:
            seen.add(entry[0])
            entries.append(entry)
    return entries


def module_cmd_lines(all_details: list[JsonDict]) -> list[str]:
    """Render `# Cmd[N]: COMMAND (exit CODE)` lines, with a repeat comment where a command recurs."""
    repeats: dict[str, list[int]] = {}
    lines = []
    for n, (cmd, exit_code) in enumerate(build_module_cmd_list(all_details, repeats), start=1):
        lines.append(f"# Cmd[{n}]: {cmd}" + (f" (exit {exit_code})" if exit_code is not None else ""))
        if len(nums := repeats.get(cmd, [])) > 1:
            shown = ", ".join(f"#{x}" for x in nums[:_REPEATS_SHOWN])
            more = f" ... (+{len(nums) - _REPEATS_SHOWN})" if len(nums) > _REPEATS_SHOWN else ""
            lines.append(f"# Cmd[{n}] repeated {len(nums)} times: {shown}{more}")
    return lines


def is_noise_step(step: JsonDict) -> bool:
    """Return True for passed steps with no information: upload/cleanup housekeeping, bare screenshots."""
    if step.get("result") == "fail":
        return False
    text = step.get("text_data", "") or ""
    if not text:
        return not step.get("display_title")
    for ln in text.split("\n"):
        if ln.startswith("# Command:"):
            return bool(_HOUSEKEEPING_CMD_RE.match(ln[10:].strip()))
    return False


def _failure_key(detail: JsonDict) -> str:
    """Normalise a failing step so repeats differing only in numbers/ids compare equal."""
    text = detail.get("text_data", "") or ""
    text = re.sub(r"^\w{3} \d\d [\d:.]+ \S+ ", "", text)
    text = re.sub(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", "MAC", text)
    return f"{detail.get('display_title', '')}|{re.sub(r'[0-9]+', 'N', text)}"


def group_repeated_failures(cluster: list[JsonDict]) -> list[tuple[JsonDict, list[int]]]:
    """Group a cluster's failing steps by normalised text: (first step, nums of the repeats)."""
    groups: dict[str, tuple[JsonDict, list[int]]] = {}
    for d in cluster:
        key = _failure_key(d)
        if key in groups:
            groups[key][1].append(d.get("num", 0))
        else:
            groups[key] = (d, [])
    return list(groups.values())


def repeat_note(nums: list[int]) -> str:
    """Describe the repeats of a failure that is shown once."""
    shown = ", ".join(f"#{n}" for n in nums[:_REPEATS_SHOWN])
    more = f", ... (+{len(nums) - _REPEATS_SHOWN})" if len(nums) > _REPEATS_SHOWN else ""
    return f"The failure below repeats {len(nums)} more times: {shown}{more}"


def collapse_repeated_clusters(clusters: list[list[JsonDict]]) -> list[tuple[list[JsonDict], list[int]]]:
    """Keep the last of clusters with identical failures: (cluster, first step nums of earlier retries)."""
    groups: dict[frozenset[str], list[list[JsonDict]]] = {}
    for c in clusters:
        groups.setdefault(frozenset(_failure_key(d) for d in c), []).append(c)
    last = {id(g[-1]): [c[0].get("num", 0) for c in g[:-1]] for g in groups.values()}
    return [(c, last[id(c)]) for c in clusters if id(c) in last]


def group_failures_by_proximity(details: list[JsonDict], gap: int = 5) -> list[list[JsonDict]]:
    """Group failing steps into clusters whose consecutive nums differ by at most `gap`.

    `details` must be sorted by num; steps with a result other than 'fail' are ignored.
    """
    clusters: list[list[JsonDict]] = []
    for detail in (d for d in details if d.get("result") == "fail"):
        if clusters and detail.get("num", 0) - clusters[-1][-1].get("num", 0) <= gap:
            clusters[-1].append(detail)
        else:
            clusters.append([detail])
    return clusters


def get_context_steps(
    all_details: list[JsonDict], cluster_min_num: int, cluster_max_num: int, before: int = 3, after: int = 2
) -> tuple[list[JsonDict], list[JsonDict]]:
    """Return up to `before` steps before and `after` steps after a failure cluster.

    Steps come from all details (passed ones included) by list position, not num arithmetic.
    """
    nums = [d.get("num", 0) for d in all_details]
    first_idx = next((i for i, n in enumerate(nums) if n >= cluster_min_num), len(nums))
    last_idx = next((i for i in range(len(nums) - 1, -1, -1) if nums[i] <= cluster_max_num), -1)
    return all_details[max(0, first_idx - before) : first_idx], all_details[last_idx + 1 : last_idx + 1 + after]


def module_key(module: JsonDict) -> str:
    """Return a key identifying a module by category and name."""
    return f"{module.get('category')}/{module.get('name')}"


def post_fail_start(details: list[JsonDict]) -> int | None:
    """Return the step number where the post-fail hook starts ('Post-fail' marker step), if any."""
    nums = [d.get("num", 0) for d in details if d.get("display_title") == "Post-fail"]
    return min(nums) if nums else None


def is_secondary(step: JsonDict, post_start: int | None) -> bool:
    """Return True for failing steps caused by (inside) the post-fail hook, not the test itself."""
    if "# Post fail hook died" in (step.get("text_data") or ""):
        return True
    return post_start is not None and step.get("num", 0) > post_start


def secondary_line(step: JsonDict, repeats: list[int]) -> str:
    """Render a failure of the post-fail hook as one line."""
    text = re.sub(r"^# (?:Post fail hook died|Test died): ", "", (step.get("text_data") or "").strip())
    first = ESC_RE.sub("", next((ln for ln in text.split("\n") if ln.strip()), ""))[:200]
    more = f" (x{len(repeats) + 1})" if repeats else ""
    title = " ".join(str(step.get("display_title", "")).split())
    return f"Secondary failure (post-fail hook) #{step.get('num', '?')} {title}: {first}{more}".rstrip()
