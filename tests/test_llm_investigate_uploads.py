# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.uploads."""

from __future__ import annotations

from typing import Any

from openqa_llm_investigate import uploads


def test_uploaded_logs_select_by_module_prefix_and_journal() -> None:
    names = [
        "m-widgets.json",
        "m_output",
        "other.txt",
        "m-shot.png",
        "full_journal.txt",
    ]
    assert uploads.select_uploaded_logs(names, "m") == [
        "m-widgets.json",
        "m_output",
        "full_journal.txt",
    ]


def test_uploaded_logs_summarize_widgets() -> None:
    text = '{"widgets": [{"title": "T", "widgets": [{"text": "Body"}, {"label": "Yes"}]}]}'
    assert uploads.summarize_widgets(text) == [
        ("title", "T"),
        ("text", "Body"),
        ("label", "Yes"),
    ]
    assert uploads.summarize_widgets("not json") == []


def test_uploaded_logs_journal_excerpt_dedupes() -> None:
    text = "\n".join(f"Oct 08 10:00:0{i} h x[{i}]: Failed to start" for i in range(3))
    assert uploads._journal_excerpt(text + "\nok line") == ["Oct 08 10:00:00 h x[0]: Failed to start (x3)"]


_CONSOLE_LINES = [
    "i-0abc\tUEFI firmware",
    "error: ../../grub-core/video/video.c:762:no suitable video mode found.",
    "[    0.000001][    T0] NUMA: Failed to initialise from firmware",
    "[   12.7][    T1] systemd[1]: Failed to populate /etc with preset unit settings",
    "[FAILED] Failed to start Foo Service.",
    "[FAILED] Failed to start Foo Service.",
    "[  OK  ] Started Bar.",
    "[   15.3] cloud-init[1442]: 2026 - util.py[WARNING]: Failed to x",
    "Welcome to emergency mode! After logging in",
]
_CONSOLE = "\n".join(_CONSOLE_LINES)


def test_console_logs_select_console_files_newest_end_wins() -> None:
    names = [
        "destroy-console.txt",
        "a-console-end.txt",
        "z-console-end.txt",
        "prepare_instance-console-beginning.txt",
    ]
    assert uploads.select_console_files(names) == [
        "z-console-end.txt",
        "prepare_instance-console-beginning.txt",
    ]
    assert uploads.select_console_files(["destroy-console.txt"]) == ["destroy-console.txt"]
    assert uploads.select_console_files(["x.txt"]) == []


def test_console_logs_console_warnings_filters_benign_and_dedupes() -> None:
    out = uploads.console_warnings(_CONSOLE)
    assert out == [
        "[FAILED] Failed to start Foo Service. (x2)",
        "[   15.3] cloud-init[1442]: 2026 - util.py[WARNING]: Failed to x",
        "Welcome to emergency mode! After logging in",
    ]


_DOWNLOADS = (
    '<a href="/tests/1/file/a.txt">x</a> <a href="/tests/1/file/a.txt?x=1">y</a>'
    '<a href="/tests/1/file/b-console-end.txt">'
)


def test_list_job_files(make_openqa: Any) -> None:
    assert uploads.list_job_files(make_openqa({"/tests/1/downloads_ajax": _DOWNLOADS}), "1") == [
        "a.txt",
        "b-console-end.txt",
    ]
    assert uploads.list_job_files(make_openqa({}), "1") == []


def test_collect_uploaded_logs_widgets_and_terraform_output(make_openqa: Any) -> None:
    oqa = make_openqa({
        "/tests/1/file/m-widgets.json": '{"title": "Warning", "text": "Missing /boot/efi"}',
        "/tests/1/file/m-tf_apply_output": "noise\n╷\n│ Error: Machine type 'c3' does not exist\n│ more\n╵\n",
    })
    names = ["m-widgets.json", "m-tf_apply_output", "other.txt"]
    assert uploads.collect_uploaded_logs(oqa, "1", names, "m") == {
        "m-widgets.json": ["title: Warning", "text: Missing /boot/efi"],
        "m-tf_apply_output": ["Error: Machine type 'c3' does not exist", "more"],
    }


def test_collect_uploaded_logs_missing_files_are_silent(make_openqa: Any) -> None:
    assert not uploads.collect_uploaded_logs(make_openqa({}), "1", ["m-widgets.json", "full_journal.txt"], "m")


def test_collect_job_level_logs_scans_newer_console_and_cloudregister(make_openqa: Any) -> None:
    oqa = make_openqa({
        "/tests/1/file/destroy-console.txt": "[FAILED] Failed to start OLD.\n",
        "/tests/1/file/destroy-console-end.txt": _CONSOLE,
        "/tests/1/file/cloudregister.txt": "ok\n2026-10-07 22:03:22,403: x: ERROR: register failed\n",
    })
    names = ["destroy-console.txt", "destroy-console-end.txt", "cloudregister.txt"]
    consoles, others = uploads.collect_job_level_logs(oqa, "1", names)
    assert list(consoles) == ["destroy-console-end.txt"]
    assert "[FAILED] Failed to start Foo Service. (x2)" in consoles["destroy-console-end.txt"]
    assert others == {"cloudregister.txt": ["2026-10-07 22:03:22,403: x: ERROR: register failed"]}


def test_collect_job_level_logs_missing_files_are_silent(make_openqa: Any) -> None:
    assert uploads.collect_job_level_logs(make_openqa({}), "1", ["destroy-console.txt", "cloudregister.txt"]) == (
        {},
        {},
    )


def test_format_uploaded_logs_masks_codes() -> None:
    assert uploads.format_uploaded_logs({"a": ["INTERNAL-USE-ONLY-cafe-beef"]}, "console warnings") == [
        "--- console warnings: a ---",
        "INTERNAL-USE-ONLY-****-****",
        "--- End of Log Section ---",
        "",
    ]


def test_uploaded_logs_select_only_text_files() -> None:
    names = ["m-a.txt", "m-b.log", "m-c.json", "m-d.yaml", "m-out", "m-widgets.json", "m-x.tar.gz"]
    assert uploads.select_uploaded_logs(names, "m") == ["m-a.txt", "m-b.log", "m-out", "m-widgets.json"]


def test_log_excerpt_keeps_signal_with_one_line_context() -> None:
    text = "a\nb\nc\nfailed here\nd\ne\nf"
    assert uploads._log_excerpt("m.txt", text) == ["c", "failed here", "d"]


def test_log_excerpt_without_signal_is_empty(make_openqa: Any) -> None:
    oqa = make_openqa({"/tests/1/file/m-out.txt": "password: secret\nall good\n"})
    assert not uploads.collect_uploaded_logs(oqa, "1", ["m-out.txt"], "m")


def test_log_excerpt_truncates_long_lines() -> None:
    assert uploads._log_excerpt("m.txt", "error " + "x" * 500) == ["error " + "x" * 194]


def test_summarize_widgets_dedupes_and_skips_scalars() -> None:
    text = '{"a": [{"title": "T"}, {"title": "T"}, 1, null, {"text": " "}]}'
    assert uploads.summarize_widgets(text) == [("title", "T")]


def test_collect_uploaded_logs_journal(make_openqa: Any) -> None:
    oqa = make_openqa({"/tests/1/file/full_journal.txt": "ok\nfoo: segfault at 0\n"})
    assert uploads.collect_uploaded_logs(oqa, "1", ["full_journal.txt"], "m") == {
        "full_journal.txt": ["foo: segfault at 0"]
    }
