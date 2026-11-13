# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.actions."""

from __future__ import annotations

from typing import Any

from openqa_llm_investigate import actions as ac

_TS = "[2026-11-12T11:34:06.440889Z] [debug] [pid:42]"
_SEND = "testapi::send_key"
_X11 = "testapi::x11_start_program"


def _call(step: int, chain: str, call: str) -> str:
    return f"{_TS} [step:c,m,{step}] tests/c/m.pm:1 called {chain}\n{_TS} <<< testapi::{call}\n"


def _log(*calls: str, module: str = "m") -> str:
    return f"{_TS} \x1b[1;34m||| starting {module} tests/c/m.pm\x1b[0m\n" + "".join(calls)


def _texts(log: str, module: str = "m") -> list[str]:
    return [a.text for a in ac.module_actions(log, module)]


def test_input_calls_are_listed_with_their_step() -> None:
    log = _log(
        _call(1, _SEND, 'send_key(key="ret", wait_screen_change=0)'),
        _call(2, "testapi::type_string", 'type_string(string="ls -l", max_interval=13)'),
        _call(3, "testapi::select_console", 'select_console(testapi_console="root-console")'),
        _call(4, _X11, 'x11_start_program(program="konsole", target_match="desktop")'),
        _call(5, "testapi::assert_screen", 'assert_screen(mustmatch="x", timeout=30)'),
    )
    assert ac.module_actions(log, "m") == [
        ac.Action(1, "send_key ret"),
        ac.Action(2, 'type_string "ls -l"'),
        ac.Action(3, "select_console root-console"),
        ac.Action(4, "x11_start_program konsole"),
    ]


def test_calls_made_by_another_testapi_call_are_left_out() -> None:
    nested = f"{_X11} -> lib/susedistribution.pm:292 called {_SEND}"
    log = _log(
        _call(1, _X11, 'x11_start_program(program="konsole")'),
        _call(1, nested, 'send_key(key="alt-f2")'),
        _call(1, f"{_X11} -> lib/s.pm:299 called testapi::type_string", 'type_string(string="konsole")'),
        _call(2, "testapi::type_password", 'type_string(string="[masked]")'),
        _call(3, "testapi::assert_screen", 'send_key(key="ret")'),
    )
    assert _texts(log) == ["x11_start_program konsole"]


def test_wait_screen_change_block_and_send_key_until_needlematch_are_kept() -> None:
    block = f"testapi::wait_screen_change -> tests/c/m.pm:3 called {_SEND}"
    until = "testapi::send_key_until_needlematch"
    log = _log(_call(1, block, 'send_key(key="ret")'), _call(2, until, 'send_key(key="down", wait_screen_change=0)'))
    assert _texts(log) == ["send_key ret", "send_key down"]


def test_enter_cmd_is_named_and_its_newline_dropped() -> None:
    log = _log(
        _call(1, "testapi::enter_cmd", 'type_string(string="reboot\\n", max_interval=250)'),
        _call(2, "testapi::type_string", 'type_string(string="ls\\n")'),
    )
    assert _texts(log) == ['enter_cmd "reboot"', 'type_string "ls\\n"']


def test_clicks_use_the_last_mouse_position() -> None:
    click = 'mouse_click(button="left", cursor_down="0.15")'
    log = _log(
        _call(1, "testapi::mouse_set", "mouse_set(x=10, y=20)"),
        _call(1, "testapi::mouse_click", click),
        _call(2, "testapi::mouse_set", "mouse_set(x=30, y=40)"),
        _call(3, "testapi::assert_and_click", "mouse_set(x=50, y=60)"),
        _call(3, "testapi::assert_and_click", click),
    )
    assert ac.module_actions(log, "m") == [
        ac.Action(1, "click (10,20)"),
        ac.Action(3, "clicked (50,60)", clicked=True),
    ]
    assert _texts(_log(_call(1, "testapi::mouse_click", click))) == ["click"]
    assert _texts(_log(_call(1, "testapi::assert_and_click", click))) == []


def test_long_strings_are_cut() -> None:
    log = _log(_call(1, "testapi::type_string", f'type_string(string="{"a" * 100}")'))
    assert _texts(log) == [f'type_string "{"a" * 80}..."']


def test_only_the_last_run_before_the_post_fail_hook_counts() -> None:
    first = _log(_call(1, _SEND, 'send_key(key="old")'))
    second = _log(
        _call(1, _SEND, 'send_key(key="new")'),
        _call(2, "basetest::run_post_fail -> lib/b.pm:1 called " + _SEND, 'send_key(key="late")'),
        module="m",
    )
    assert _texts(first + second) == ["send_key new"]
    finished = _log(_call(1, _SEND, 'send_key(key="a")')) + f"{_TS} ||| finished m tests/c/m.pm\n"
    assert _texts(finished + _call(2, _SEND, 'send_key(key="b")')) == ["send_key a"]


def test_other_modules_and_unrelated_lines_are_ignored() -> None:
    log = _log(
        f'{_TS} <<< testapi::send_key(key="orphan")\n',
        f"{_TS} [step:c,m,1] tests/c/m.pm:1 called testapi::send_key\nplain line\n",
    )
    assert _texts(log) == []
    assert _texts(log, "other") == []
    assert ac.module_actions("", "m") == []


def test_collapse_counts_repeats_and_patterns() -> None:
    assert ac.collapse(["a", "b", "b", "c"]) == ["a", "b (x2)", "c"]
    assert ac.collapse(["a"] * 5 + ["b"]) == ["a (x5)", "b"]
    assert ac.collapse(["x", "d", "r", "d", "r", "d", "r", "y"]) == ["x", "[d, r] (x3)", "y"]
    assert ac.collapse(["a", "b", "a", "b"]) == ["a", "b", "a", "b"]
    assert ac.collapse([]) == []


def _shown(*nums: int, needle: bool = False) -> list[dict[str, Any]]:
    return [{"num": n, **({"needle": "n"} if needle else {})} for n in nums]


def test_step_notes_go_to_the_next_shown_step() -> None:
    shown = _shown(2, 5)
    notes = ac.step_notes([ac.Action(1, "send_key a"), ac.Action(4, "send_key b"), ac.Action(9, "send_key c")], shown)
    assert notes == {id(shown[0]): ["input before: send_key a"], id(shown[1]): ["input before: send_key b, send_key c"]}


def test_step_notes_click_goes_to_the_needle_step_before() -> None:
    shown = [{"num": 1}, *_shown(4, needle=True)]
    notes = ac.step_notes([ac.Action(5, "clicked (1,2)", clicked=True), ac.Action(5, "send_key x")], shown)
    assert notes == {id(shown[1]): ["input before: send_key x", "clicked (1,2)"]}
    assert ac.step_notes([ac.Action(5, "clicked (1,2)", clicked=True)], _shown(1, 4)) == {}
    assert ac.step_notes([ac.Action(1, "send_key a")], []) == {}


def test_step_notes_keep_the_newest_items() -> None:
    shown = _shown(100)
    notes = ac.step_notes([ac.Action(n, f"send_key k{n}") for n in range(8)], shown)
    assert notes[id(shown[0])] == [
        "input before: ... (2 earlier omitted), " + ", ".join(f"send_key k{n}" for n in range(2, 8))
    ]
