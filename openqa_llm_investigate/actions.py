# Copyright SUSE LLC
"""Find the input a test sent (keys, typed text, clicks) in autoinst-log.txt and attach it to the module's steps."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from .text import ESC_RE

if TYPE_CHECKING:
    from .http import JsonDict

_STEP_RE = re.compile(r"\[step:[^,\]]*,[^,\]]*,(\d+)\] \S+ called (\S.*)$")
_CALL_RE = re.compile(r"<<< testapi::(\w+)\((.*)\)$")
_ARG_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|[^,]*)')
_CHAIN_RE = re.compile(r"called (\S+)")
_BLOCKS = {"wait_screen_change", "assert_screen_change"}
_TRANSPARENT = {"send_key_until_needlematch"}
_FIRST_VALUE = {"x11_start_program": "program", "select_console": "testapi_console"}
_NEWLINE = "\\n"
_STRING_MAX = 80
_BLOCK_MAX = 4
_ITEMS_MAX = 6


class Action(NamedTuple):
    """An input call; `num` is the step it precedes, or the one it clicked on with `clicked`."""

    num: int
    text: str
    clicked: bool = False


def _value(args: dict[str, str], key: str) -> str:
    value = args.get(key, "").strip().strip('"')
    return value if len(value) <= _STRING_MAX else f"{value[:_STRING_MAX]}..."


def _text(name: str, args: dict[str, str], mouse: tuple[str, str] | None) -> str | None:
    """Return how the call `name` is shown, None if it is not input worth showing."""
    if name == "send_key":
        return f"send_key {_value(args, 'key')}"
    if name in {"type_string", "enter_cmd"}:
        typed = _value(args, "string")
        return f'{name} "{typed.removesuffix(_NEWLINE) if name == "enter_cmd" else typed}"'
    if name in _FIRST_VALUE:
        return f"{name} {_value(args, _FIRST_VALUE[name])}"
    if name == "mouse_click":
        return f"click ({mouse[0]},{mouse[1]})" if mouse else "click"
    return None


def _module_log(autoinst: str, module: str) -> str:
    """Return the log lines of the last run of `module`, up to its post-fail hook."""
    starts = [m.end() for m in re.finditer(rf"\|\|\| starting {re.escape(module)} ", autoinst)]
    if not starts:
        return ""
    segment = autoinst[starts[-1] :]
    ends = (i for i in (segment.find("run_post_fail"), segment.find("||| finished ")) if i >= 0)
    return ESC_RE.sub("", segment[: min(ends, default=len(segment))])


def _action(
    num: int, chain: list[str], name: str, args: dict[str, str], mouse: tuple[str, str] | None
) -> Action | None:
    """Return the action of the logged call `name`, None if it was made by another testapi call or is not input.

    The last item of `chain` is the call the test made, which differs from `name` when testapi made the call itself.
    """
    *ancestors, last = chain
    wrapper = last.removeprefix("testapi::") if last.startswith("testapi::") else name
    if name == "mouse_click" and wrapper == "assert_and_click" and mouse:
        return Action(num, f"clicked ({mouse[0]},{mouse[1]})", clicked=True)
    if name == "type_string" and wrapper == "enter_cmd":
        name = wrapper
    elif wrapper in _TRANSPARENT:
        wrapper = name
    nested = {a.removeprefix("testapi::") for a in ancestors if a.startswith("testapi::")} - _BLOCKS
    text = None if wrapper != name or nested else _text(name, args, mouse)
    return Action(num, text) if text else None


def module_actions(autoinst: str, module: str) -> list[Action]:
    """Return the input calls of the last run of `module` in autoinst-log.txt, before its post-fail hook.

    Calls made by another testapi call (like the `alt-f2` of x11_start_program) are left out, except in the
    block of wait_screen_change and in send_key_until_needlematch, which logs only its key presses. A type_string
    of enter_cmd is shown as enter_cmd, and the click of assert_and_click is marked as `clicked`.
    """
    out: list[Action] = []
    step: tuple[int, list[str]] | None = None
    mouse: tuple[str, str] | None = None
    for line in _module_log(autoinst, module).split("\n"):
        if m := _STEP_RE.search(line):
            step = int(m[1]), _CHAIN_RE.findall(f"called {m[2]}")
        elif (m := _CALL_RE.search(line)) and step:
            args = dict(_ARG_RE.findall(m[2]))
            if m[1] == "mouse_set":
                mouse = args.get("x", "?"), args.get("y", "?")
            elif action := _action(step[0], step[1], m[1], args, mouse):
                out.append(action)
            step = None
    return out


def collapse(items: list[str]) -> list[str]:
    """Count repeats: `a, a, a` becomes `a (x3)` and `a, b, a, b, a, b` becomes `[a, b] (x3)`."""
    out: list[str] = []
    i = 0
    while i < len(items):
        size, reps = 1, 1
        for k in range(1, _BLOCK_MAX + 1):
            block = items[i : i + k]
            r = 1
            while len(block) == k and items[i + r * k : i + (r + 1) * k] == block:
                r += 1
            if r >= (2 if k == 1 else 3) and k * r > size * reps:
                size, reps = k, r
        body = ", ".join(items[i : i + size])
        out.append(body if reps == 1 else f"{body if size == 1 else f'[{body}]'} (x{reps})")
        i += size * reps
    return out


def step_notes(actions: list[Action], shown: list[JsonDict]) -> dict[int, list[str]]:
    """Return the note lines of the steps in `shown` (id(step) -> lines): the input before each step, a click on it.

    Input before a step that is not shown (a bare screenshot) goes to the next shown one.
    """
    if not shown:
        return {}
    before: dict[int, list[str]] = {}
    clicks: dict[int, list[str]] = {}
    for action in actions:
        if action.clicked:
            target = next((d for d in reversed(shown) if d.get("num", 0) < action.num and d.get("needle")), None)
            if target is not None:
                clicks.setdefault(id(target), []).append(action.text)
        else:
            target = next((d for d in shown if d.get("num", 0) >= action.num), shown[-1])
            before.setdefault(id(target), []).append(action.text)
    notes: dict[int, list[str]] = {}
    for key in before.keys() | clicks.keys():
        items = collapse(before.get(key, []))
        if len(items) > _ITEMS_MAX:
            items = [f"... ({len(items) - _ITEMS_MAX} earlier omitted)", *items[-_ITEMS_MAX:]]
        notes[key] = ([f"input before: {', '.join(items)}"] if items else []) + clicks.get(key, [])
    return notes
