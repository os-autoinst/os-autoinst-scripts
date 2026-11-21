# Copyright SUSE LLC
"""Extract and group the failing steps of openQA test modules."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .text import (
    has_command,
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
# os-autoinst fetches a script with `curl .../current_script > FILE` and then runs `clear`; a bare `clear` is no signal
_TRANSFER_CMD_RE = re.compile(r"^(?:clear|curl -f -v \S+/current_script > \S+)$")
_COMMAND_MARKERS = ("# Command:", "# wait_serial", "# Result:", "# Test died:")
_REPEATS_SHOWN = 5
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


def _failure_kind(step: JsonDict, cmd: str) -> str:
    """Name what `cmd` of a failing step is: a needle check, a command, a wait_serial marker or a plain step."""
    text = step.get("text_data") or ""
    if cmd.startswith("no candidate needle"):
        return "needle check"
    if "# Test died:" in text or has_command(text):
        return "command"
    return "wait_serial marker" if step.get("display_title") == "wait_serial" else "step"


def failing_cluster(all_details: list[JsonDict]) -> tuple[list[JsonDict], list[int]] | None:
    """Return the last failing cluster outside the post-fail hook, with the first nums of its earlier identical retries.

    Only that cluster failed the module; the other failing steps were non-fatal.
    """
    post_start = post_fail_start(all_details)
    clusters = [
        c
        for cl in group_failures_by_proximity(all_details)
        if (c := [d for d in cl if not is_secondary(d, post_start)])
    ]
    collapsed = collapse_repeated_clusters(clusters)
    return collapsed[-1] if collapsed else None


def failing_command(cluster: list[JsonDict]) -> tuple[JsonDict, str, str | None] | None:
    """Return the (step, command, exit code) that failed the module, None if its cluster has no command.

    Starts at the last `# Test died:` step (else the last one) and walks back through the cluster.
    """
    died = [i for i, d in enumerate(cluster) if "# Test died:" in (d.get("text_data") or "")]
    for step in reversed(cluster[: (died[-1] if died else len(cluster) - 1) + 1]):
        entry = _step_command(step)
        if entry is not None and not entry[0].startswith("(exit "):
            return step, *entry
    return None


def failing_step(cluster: list[JsonDict]) -> JsonDict:
    """Return the step to mark as the failure: the one with the failing command, else the last died/last step."""
    if found := failing_command(cluster):
        return found[0]
    return next((d for d in reversed(cluster) if "# Test died:" in (d.get("text_data") or "")), cluster[-1])


def failing_command_line(cluster: list[JsonDict]) -> str | None:
    """Describe the command that failed the module (None if the cluster has none)."""
    if not (found := failing_command(cluster)):
        return None
    step, cmd, exit_code = found
    return f'The module has been failed by: "{cmd}" {_failure_kind(step, cmd)}' + (
        f" (exit {exit_code})." if exit_code is not None else "."
    )


def is_noise_step(step: JsonDict) -> bool:
    """Return True for passed steps with no information: os-autoinst's script transfer, polls, bare screenshots."""
    if step.get("result") == "fail" or step.get("needle"):
        return False
    text = step.get("text_data", "") or ""
    if not text:
        return not step.get("display_title")
    for ln in text.split("\n"):
        if ln.startswith("# Command:"):
            return bool(_TRANSFER_CMD_RE.match(ln[10:].strip()))
    return False


def is_command_step(step: JsonDict) -> bool:
    """Return True for steps that ran a command: their text_data has a `# Result:` section."""
    return "# Result:" in (step.get("text_data") or "")


def is_record_info(step: JsonDict) -> bool:
    """Return True for passed steps with a title and free text, like record_info."""
    text = step.get("text_data") or ""
    return (
        step.get("result") != "fail"
        and bool(step.get("display_title"))
        and bool(text.strip())
        and not any(marker in text for marker in _COMMAND_MARKERS)
    )


def previous_modules(all_modules: list[JsonDict], module: JsonDict, n: int = 5) -> tuple[list[JsonDict], int]:
    """Return the up to `n` modules that ran before `module` (found by identity) and the number of earlier ones."""
    idx = next((i for i, m in enumerate(all_modules) if m is module), None)
    if idx is None:
        return [], 0
    start = max(0, idx - n)
    return all_modules[start:idx], start


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
