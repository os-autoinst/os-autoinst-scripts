# Copyright SUSE LLC
"""Unit tests for openqa-recent-test-changes."""

from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pytest_mock import MockerFixture

    from openqa_llm_investigate.http import OpenQA

rootpath = pathlib.Path(__file__).parent.parent.resolve()
loader = importlib.machinery.SourceFileLoader("recent_test_changes", f"{rootpath}/openqa-recent-test-changes")
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
rtc = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = rtc
loader.exec_module(rtc)

runner = CliRunner()
STARTED = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)
NASTY = (
    "poo#123 label:foo flag:x gh#o/r#1 bsc123#456 https://progress.opensuse.org/issues/1 https://github.com/o/r/pull/1"
)

# the patterns of OpenQA::Utils that turn comment text into bugrefs, labels and flags
_BUGREF_RE = re.compile(
    r"(?:^|(?<=\s)|(?<=,))(?:bnc|bsc|boo|bgo|bmo|brc|bko|poo|gh|kde|kdi|fdo|jsc|pio|ggo|gfs|ffo)#?[^#\s<>,]*#(?:[A-Z]+-)?\d+(?![\w\"])"
)
_LABEL_RE = re.compile(r"\b(?:label|flag):[\w:#/-]+")
_BARE_URL_RE = re.compile(r"(?<![\"(\[])https://(?:github\.com|progress\.opensuse\.org|bugzilla\.suse\.com)/")


def _parsed_by_openqa(text: str) -> list[str]:
    return [m.group() for r in (_BUGREF_RE, _LABEL_RE, _BARE_URL_RE) for m in r.finditer(text)]


def _git(repo: pathlib.Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, **({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {})}
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: pathlib.Path, date: str, subject: str, *files: str) -> str:
    for f in files:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(f"{date} {subject}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", subject, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    root = tmp_path / "share" / "tests"
    path = root / "sle"
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", "https://github.com/o/r.git")
    monkeypatch.setattr(rtc, "TESTS_ROOTS", (root,))
    return path


@pytest.fixture
def history(repo: pathlib.Path) -> dict[str, str]:
    return {
        "old": _commit(repo, "2026-10-10T10:00:00+00:00", "Old change", "tests/a/mod.pm"),
        "a": _commit(repo, "2026-10-20T10:00:00+00:00", "Fix mod", "tests/a/mod.pm", "lib/x.pm"),
        "b": _commit(repo, "2026-10-21T10:00:00+00:00", NASTY, "lib/x.pm"),
        "other": _commit(repo, "2026-10-21T11:00:00+00:00", "Unrelated", "tests/b/other.pm"),
        "future": _commit(repo, "2026-10-23T10:00:00+00:00", "After the job", "tests/a/mod.pm"),
    }


def _details(*paths: str) -> dict[str, Any]:
    trace = f"# Test died: boom at {paths[0]} line 5.\n" + "".join(f"  called at {p} line 9\n" for p in paths[1:])
    detail = {"result": "fail", "text_data": trace}
    return {"modules": [{"name": "mod", "category": "a", "result": "failed", "details": [detail]}]}


def _routes(job: dict[str, Any], details: dict[str, Any], vars_json: dict[str, Any] | None = None) -> dict[str, str]:
    return {
        "/api/v1/jobs/1": json.dumps({"job": job}),
        "/api/v1/jobs/1/comments": "[]",
        "/tests/1/details_ajax": json.dumps(details),
        "/tests/1/file/vars.json": json.dumps(vars_json or {}),
    }


def _job(**kwargs: Any) -> dict[str, Any]:
    return {"result": "failed", "test": "t", "t_started": "2026-10-22T12:00:00", "settings": {"DISTRI": "sle"}} | kwargs


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"DISTRI": "sle"}, "sle"),
        ({"DISTRI": "other", "CASEDIR": "sle"}, "sle"),
        ({"DISTRI": "sle", "CASEDIR": "https://github.com/o/r.git#branch"}, None),
        ({"DISTRI": "sle", "CASEDIR": "/abs/path"}, None),
        ({"DISTRI": "../etc"}, None),
        ({"DISTRI": "missing"}, None),
        ({}, None),
    ],
)
def test_tests_dir(repo: pathlib.Path, settings: dict[str, str], expected: str | None) -> None:
    assert rtc.tests_dir(settings) == (repo.parent / expected if expected else None)


def test_tests_dir_prefers_existing_versioned_checkout(repo: pathlib.Path) -> None:
    versioned = repo.parent / "sle-15.7"
    versioned.mkdir()
    assert rtc.tests_dir({"DISTRI": "sle", "VERSION": "15.7"}) == versioned
    assert rtc.tests_dir({"DISTRI": "sle", "VERSION": "15.6"}) == repo


def test_tests_dir_without_any_root(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(rtc, "TESTS_ROOTS", (tmp_path / "missing",))
    assert rtc.tests_dir({"DISTRI": "sle"}) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/var/lib/openqa/share/tests/sle/tests/a/mod.pm", "tests/a/mod.pm"),
        ("/usr/lib/sle/lib/x.pm", "lib/x.pm"),
        ("tests/a/mod.pm", "tests/a/mod.pm"),
        ("/var/lib/openqa/share/tests/sle/tests/a/missing.pm", None),
        ("../../tests/a/mod.pm", None),
    ],
)
@pytest.mark.usefixtures("history")
def test_resolve_path(repo: pathlib.Path, path: str, expected: str | None) -> None:
    assert rtc.resolve_path(repo, path) == expected


@pytest.mark.usefixtures("history")
def test_involved_files(repo: pathlib.Path) -> None:
    details = _details("/x/sle/lib/x.pm", "/x/sle/lib/x.pm", "/x/sle/tests/a/mod.pm", "/x/sle/lib/gone.pm")
    assert rtc.involved_files(repo, details["modules"]) == ["lib/x.pm", "tests/a/mod.pm"]


@pytest.mark.usefixtures("history")
def test_involved_files_falls_back_to_the_module_file(repo: pathlib.Path) -> None:
    module = {"name": "mod", "category": "a", "result": "failed", "details": [{"result": "fail", "text_data": "x"}]}
    assert rtc.involved_files(repo, [module]) == ["tests/a/mod.pm"]
    assert rtc.involved_files(repo, [{**module, "name": "none"}]) == []


def test_recent_commits_window_and_files(repo: pathlib.Path, history: dict[str, str]) -> None:
    commits = rtc.recent_commits(repo, "HEAD", STARTED, ["tests/a/mod.pm", "lib/x.pm"])
    assert [c.sha for c in commits] == [history["b"], history["a"]]
    assert [sorted(n for _, n in c.changes) for c in commits] == [["lib/x.pm"], ["lib/x.pm", "tests/a/mod.pm"]]
    assert commits[1].date == "2026-10-20"
    assert rtc.recent_commits(repo, "HEAD", STARTED, ["tests/b/nothing.pm"]) == []


def test_recent_commits_pinned_to_the_tested_hash(repo: pathlib.Path, history: dict[str, str]) -> None:
    late = STARTED + dt.timedelta(days=2)
    files = ["tests/a/mod.pm"]
    assert [c.sha for c in rtc.recent_commits(repo, "HEAD", late, files)] == [history["future"], history["a"]]
    pinned = rtc.recent_commits(repo, history["b"], late, files)
    assert [(c.sha, c.changes) for c in pinned] == [(history["a"], [("M", "tests/a/mod.pm")])]


def test_tested_rev(repo: pathlib.Path, history: dict[str, str]) -> None:
    assert rtc.tested_rev(repo, {"TEST_GIT_HASH": history["b"]}, "TEST", {}) == history["b"]
    assert rtc.tested_rev(repo, {"TEST_GIT_HASH": "0" * 40}, "TEST", {}) == "HEAD"
    assert rtc.tested_rev(repo, {"TEST_GIT_HASH": "--output=/tmp/x"}, "TEST", {}) == "HEAD"
    assert rtc.tested_rev(repo, {}, "TEST", {}) == "HEAD"
    assert rtc.tested_rev(repo, {"NEEDLES_GIT_HASH": history["b"]}, "NEEDLES", {}) == history["b"]
    assert rtc.tested_rev(repo, {}, "NEEDLES", {}) == "HEAD"


def test_tested_rev_is_none_for_an_unknown_refspec_commit(repo: pathlib.Path, history: dict[str, str]) -> None:
    settings = {"TEST_GIT_REFSPEC": "refs/pull/1/head"}
    assert rtc.tested_rev(repo, {"TEST_GIT_HASH": "0" * 40}, "TEST", settings) is None
    assert rtc.tested_rev(repo, {}, "TEST", settings) is None
    assert rtc.tested_rev(repo, {"TEST_GIT_HASH": history["b"]}, "TEST", settings) == history["b"]
    assert rtc.tested_rev(repo, {}, "NEEDLES", settings) == "HEAD"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/o/r.git", "https://github.com/o/r/commit/"),
        ("https://github.com/o/r/", "https://github.com/o/r/commit/"),
        ("git@github.com:o/r.git", "https://github.com/o/r/commit/"),
        ("https://user:token@github.com/o/r.git", None),
        ("/some/local/path", None),
    ],
)
def test_commit_url_base(repo: pathlib.Path, remote: str, expected: str | None) -> None:
    _git(repo, "remote", "set-url", "origin", remote)
    assert rtc.commit_url_base(repo) == expected


def test_commit_url_base_without_origin(tmp_path: pathlib.Path) -> None:
    _git(tmp_path, "init", "-q")
    assert rtc.commit_url_base(tmp_path) is None


def test_sanitize_subject_hides_everything_openqa_parses() -> None:
    assert _parsed_by_openqa(NASTY)
    assert _parsed_by_openqa(rtc.sanitize_subject(NASTY)) == []


@pytest.mark.parametrize("subject", ["Fix: foo (#123)", "Add test for foo", "Merge pull request #5 from x/y"])
def test_sanitize_subject_keeps_harmless_text(subject: str) -> None:
    assert rtc.sanitize_subject(subject) == subject


def test_sanitize_subject_is_capped() -> None:
    assert len(rtc.sanitize_subject("x" * 500)) == rtc.SUBJECT_MAX


def _commit_tuple(i: int, changes: list[tuple[str, str]] | None = None) -> rtc.Commit:
    return rtc.Commit(
        f"{i:x}" * 40, "2026-10-21", 1000 - i, f"c{i}", changes or [("M", "tests/a.pm"), ("M", "lib/b.pm")]
    )


def test_format_section_link_and_status() -> None:
    changes = [("M", "a.json"), ("A", "b.json"), ("D", "c.json")]
    section = rtc.format_section("HEAD", "https://github.com/o/r/commit/", [_commit_tuple(0, changes)], [])
    assert (
        section == f"HEAD\n* [0000000](https://github.com/o/r/commit/{'0' * 40}) 2026-10-21 c0: "
        "`a.json`, `b.json` (added), `c.json` (removed)"
    )
    assert rtc.format_section("HEAD", None, [_commit_tuple(1)], []).splitlines()[1].startswith("* `1111111` ")
    assert rtc.format_section("HEAD", None, [], ["note"]) is None


def test_format_section_caps_commits_and_files() -> None:
    commits = [_commit_tuple(i % 16) for i in range(rtc.COMMITS_MAX + 4)]
    lines = rtc.format_section(
        "HEAD", None, commits, ["Only the first 1 of 2 involved files were checked."]
    ).splitlines()
    assert len(lines) == 1 + rtc.COMMITS_MAX + 2
    assert lines[-2] == f"* ... and 4 more older commits not shown (limit {rtc.COMMITS_MAX})"
    assert lines[-1] == "* Only the first 1 of 2 involved files were checked."
    many = [("M", f"f{i}.json") for i in range(rtc.FILE_NAMES_MAX + 3)]
    line = rtc.format_section("HEAD", None, [_commit_tuple(0, many)], []).splitlines()[1]
    assert line.endswith(f"`f{rtc.FILE_NAMES_MAX - 1}.json`, +3 more files")


@pytest.mark.usefixtures("repo")
def test_format_section_keeps_the_newest_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rtc, "COMMITS_MAX", 1)
    commits = rtc.merge_commits([_commit_tuple(5)], [_commit_tuple(2), _commit_tuple(9)])
    section = rtc.format_section("HEAD", None, commits, [])
    assert section is not None
    assert "`2222222`" in section
    assert "`9999999`" not in section


def test_merge_commits_joins_changes_once_per_commit() -> None:
    first = _commit_tuple(1, [("M", "a.json")])
    second = first._replace(changes=[("M", "a.json"), ("A", "b.json")])
    merged = rtc.merge_commits([first], [_commit_tuple(2), second])
    assert [c.sha[:1] for c in merged] == ["1", "2"]
    assert merged[0].changes == [("M", "a.json"), ("A", "b.json")]


def test_cap_note() -> None:
    assert rtc._cap_note("files", rtc.FILES_MAX) == []
    assert rtc._cap_note("tags", rtc.FILES_MAX + 1) == [
        f"Only the first {rtc.FILES_MAX} of {rtc.FILES_MAX + 1} involved tags were checked."
    ]


def test_build_comment(history: dict[str, str], make_openqa: Callable[[Mapping[str, Any]], OpenQA]) -> None:
    details = _details("/var/lib/openqa/share/tests/sle/lib/x.pm", "/var/lib/openqa/share/tests/sle/tests/a/mod.pm")
    oqa = make_openqa(_routes(_job(), details, {"TEST_GIT_HASH": history["a"]}))
    comment = rtc.build_comment(oqa, "1")
    assert comment is not None
    assert comment.splitlines()[0].startswith(rtc.CODE_MARKER)
    assert f"[{history['a'][:7]}](https://github.com/o/r/commit/{history['a']}) 2026-10-20 Fix mod" in comment
    assert history["b"][:7] not in comment

    comment = rtc.build_comment(make_openqa(_routes(_job(), details)), "1")
    assert comment is not None
    assert history["b"][:7] in comment
    assert "poo# 123 label: foo" in comment
    assert _parsed_by_openqa(comment) == []


@pytest.mark.parametrize(
    "job",
    [
        _job(result="passed"),
        _job(result="softfailed"),
        _job(test="t:investigate:retry"),
        _job(settings={"DISTRI": "sle", "CASEDIR": "https://github.com/o/r.git#x"}),
        _job(settings={"DISTRI": "missing"}),
        _job(t_started="2026-09-01T00:00:00"),
    ],
)
@pytest.mark.usefixtures("history")
def test_build_comment_has_nothing_to_report(
    make_openqa: Callable[[Mapping[str, Any]], OpenQA], job: dict[str, Any]
) -> None:
    details = _details("/x/sle/lib/x.pm", "/x/sle/tests/a/mod.pm")
    assert rtc.build_comment(make_openqa(_routes(job, details)), "1") is None


@pytest.mark.usefixtures("history")
def test_build_comment_skips_existing_comment(make_openqa: Callable[[Mapping[str, Any]], OpenQA]) -> None:
    routes = _routes(_job(), _details("/x/sle/lib/x.pm"))
    assert rtc.build_comment(make_openqa(routes), "1") is not None
    routes["/api/v1/jobs/1/comments"] = json.dumps([{"text": f"{rtc.CODE_MARKER} files ..."}])
    assert rtc.build_comment(make_openqa(routes), "1") is None


@pytest.mark.usefixtures("history")
def test_build_comment_without_trace_files(make_openqa: Callable[[Mapping[str, Any]], OpenQA]) -> None:
    details = {"modules": [{"name": "none", "category": "a", "result": "failed", "details": []}]}
    assert rtc.build_comment(make_openqa(_routes(_job(), details)), "1") is None


@pytest.mark.parametrize(
    ("job_url", "expected"),
    [
        ("https://openqa.suse.de/tests/42", ("https://openqa.suse.de", "42")),
        ("42", ("https://openqa.opensuse.org", "42")),
    ],
)
def test_parse_job_url(job_url: str, expected: tuple[str, str]) -> None:
    assert rtc.parse_job_url(job_url) == expected


def test_main_posts_comment(mocker: MockerFixture) -> None:
    mocker.patch.object(rtc, "build_comment", return_value="the comment")
    post = mocker.patch.object(rtc, "post_comment")
    assert runner.invoke(rtc.app, ["https://openqa.suse.de/tests/42"]).exit_code == 0
    post.assert_called_once_with("https://openqa.suse.de", "42", "the comment")


def test_main_dry_prints_comment(mocker: MockerFixture) -> None:
    mocker.patch.object(rtc, "build_comment", return_value="the comment")
    post = mocker.patch.object(rtc, "post_comment")
    result = runner.invoke(rtc.app, ["--dry", "42"])
    assert result.stdout == "the comment\n"
    post.assert_not_called()


def test_main_is_silent_without_changes(mocker: MockerFixture) -> None:
    mocker.patch.object(rtc, "build_comment", return_value=None)
    post = mocker.patch.object(rtc, "post_comment")
    result = runner.invoke(rtc.app, ["42"])
    assert (result.exit_code, result.stdout) == (0, "")
    post.assert_not_called()


@pytest.mark.parametrize("error", [rtc.AnalyzerError("x"), subprocess.CalledProcessError(128, "git"), OSError("x")])
def test_main_fails_on_errors(mocker: MockerFixture, error: Exception) -> None:
    mocker.patch.object(rtc, "build_comment", side_effect=error)
    assert runner.invoke(rtc.app, ["42"]).exit_code == 1


def test_main_rejects_invalid_job_id() -> None:
    assert runner.invoke(rtc.app, ["abc"]).exit_code == 1


def test_post_comment(mocker: MockerFixture) -> None:
    run = mocker.patch.object(rtc.subprocess, "run")
    rtc.post_comment("https://srv", "42", "text")
    cmd = run.call_args.args[0]
    assert cmd[:2] == ["openqa-cli", "api"]
    assert cmd[-3:] == ["POST", "jobs/42/comments", "text=text"]


def test_post_comment_job_deleted_and_failure(mocker: MockerFixture) -> None:
    error = subprocess.CalledProcessError
    mocker.patch.object(rtc.subprocess, "run", side_effect=error(1, "x", stderr="404 Not Found"))
    rtc.post_comment("https://srv", "42", "text")
    mocker.patch.object(rtc.subprocess, "run", side_effect=error(1, "x", stderr="boom"))
    with pytest.raises(SystemExit):
        rtc.post_comment("https://srv", "42", "text")


@pytest.fixture
def needles(repo: pathlib.Path, history: dict[str, str]) -> dict[str, str]:
    del history  # the tests repo has its commits first, so its `git add .` does not pick up the nested repo
    path = repo / "products" / "sle" / "needles"
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", "git@gitlab.suse.de:qa/needles.git")
    shas = {
        "old": _commit(path, "2026-10-10T10:00:00+00:00", "Old needle", "gone-2026.json", "gone-2026.png"),
        "login": _commit(path, "2026-10-20T10:00:00+00:00", "Update login", "login-2026.json", "login-2026.png"),
        "new": _commit(path, "2026-10-21T10:00:00+00:00", NASTY, "other-2026.json", "other-2026.png"),
        "png": _commit(path, "2026-10-21T10:30:00+00:00", "New login image", "login-2026.png"),
    }
    (path / "gone-2026.json").write_text('{"tags": ["grub2", "x"]}\n')
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "Tag grub2", date="2026-10-21T10:45:00+00:00")
    (path / "gone-2026.json").unlink()
    (path / "gone-2026.png").unlink()
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "Remove grub2 needle", date="2026-10-21T11:00:00+00:00")
    shas["removed"] = _git(path, "rev-parse", "HEAD")
    shas["future"] = _commit(path, "2026-10-23T10:00:00+00:00", "After the job", "login-2026.json")
    return shas


def _screen_details(*steps: dict[str, Any]) -> dict[str, Any]:
    details = [{"num": n, "result": "fail", **step} for n, step in enumerate(steps, 1)]
    return {"modules": [{"name": "mod", "category": "a", "result": "failed", "details": details}]}


LOGIN_STEP = {"tags": ["grub2"], "needles": [{"name": "login-2026", "json": "login-2026.json", "error": 0.2}]}


@pytest.mark.usefixtures("needles")
@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"DISTRI": "sle"}, "products/sle/needles"),
        ({"DISTRI": "sle", "NEEDLES_DIR": "products/sle/needles"}, "products/sle/needles"),
        ({"DISTRI": "sle", "NEEDLES_DIR": "https://github.com/o/needles.git"}, None),
        ({"DISTRI": "sle", "NEEDLES_DIR": "../../etc"}, None),
        ({"DISTRI": "sle", "NEEDLES_DIR": "missing"}, None),
        ({"DISTRI": "other"}, None),
    ],
)
def test_needles_dir(repo: pathlib.Path, settings: dict[str, str], expected: str | None) -> None:
    assert rtc.needles_dir(settings, repo) == (repo / expected if expected else None)


@pytest.mark.usefixtures("needles")
def test_needles_dir_absolute_only_below_the_checkout_roots(repo: pathlib.Path, tmp_path: pathlib.Path) -> None:
    inside = repo / "products" / "sle" / "needles"
    assert rtc.needles_dir({"NEEDLES_DIR": str(inside)}, None) == inside.resolve()
    assert rtc.needles_dir({"NEEDLES_DIR": str(inside)}, repo) == inside.resolve()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _git(outside, "init", "-q")
    assert rtc.needles_dir({"NEEDLES_DIR": str(outside)}, repo) is None
    assert rtc.needles_dir({"DISTRI": "sle"}, None) is None


def test_needles_dir_without_own_git_repo(repo: pathlib.Path) -> None:
    (repo / "products" / "sle" / "needles").mkdir(parents=True)
    assert rtc.needles_dir({"DISTRI": "sle"}, repo) is None
    (repo / "needles").mkdir()
    assert rtc.needles_dir({"DISTRI": "other"}, repo) is None


def test_needles_dir_falls_back_to_the_checkout_itself(repo: pathlib.Path) -> None:
    (repo / "needles").mkdir()
    _git(repo / "needles", "init", "-q")
    assert rtc.needles_dir({"DISTRI": "sle"}, repo) == repo / "needles"


@pytest.mark.usefixtures("needles")
def test_involved_needles(repo: pathlib.Path) -> None:
    path = repo / "products" / "sle" / "needles"
    bad = {"needles": [{"json": "../x.json"}, {"json": "/abs.json"}, {"json": "missing.json"}, {"json": None}, {}]}
    steps = [
        LOGIN_STEP,
        {"tags": ["bad tag", "a.b", 5]},
        bad,
        {"text_data": "# Test died: no candidate needle with tag(s) 'x, y' matched"},
    ]
    details = _screen_details(*steps)
    details["modules"][0]["details"].append({"num": 99, "result": "ok", "tags": ["not-failed"]})
    assert rtc.involved_needles(path, details["modules"]) == (
        ["login-2026.json", "login-2026.png"],
        ["grub2", "a.b", "x", "y"],
    )


@pytest.mark.usefixtures("needles")
def test_involved_needles_without_failed_screen_steps(repo: pathlib.Path) -> None:
    path = repo / "products" / "sle" / "needles"
    module = {"name": "m", "result": "failed", "details": [{"num": 1, "result": "ok", "tags": ["grub2"]}]}
    assert rtc.involved_needles(path, [module]) == ([], [])
    assert rtc.involved_needles(path, [{"name": "m", "result": "failed"}]) == ([], [])


def test_needle_commits_by_file_and_tag(repo: pathlib.Path, needles: dict[str, str]) -> None:
    path = repo / "products" / "sle" / "needles"
    by_file = rtc.recent_commits(path, "HEAD", STARTED, ["login-2026.json", "login-2026.png"])
    assert [c.sha for c in by_file] == [needles["png"], needles["login"]]
    by_tag = rtc.tagged_commits(path, "HEAD", STARTED, ["grub2"])
    assert [(c.sha, c.changes) for c in by_tag] == [
        (needles["removed"], [("D", "gone-2026.json")]),
        (by_tag[1].sha, [("M", "gone-2026.json")]),
    ]
    assert rtc.tagged_commits(path, "HEAD", STARTED, ["grub"]) == []
    assert rtc.tagged_commits(path, "HEAD", STARTED, ["gr.b2"]) == []
    assert rtc.tagged_commits(path, "HEAD", STARTED, []) == []


@pytest.mark.usefixtures("history")
def test_needle_section(repo: pathlib.Path, needles: dict[str, str]) -> None:
    path = repo / "products" / "sle" / "needles"
    modules = _screen_details(LOGIN_STEP)["modules"]
    section = rtc.needle_section(path, modules, _job(), {"NEEDLES_GIT_HASH": needles["png"]})
    assert section is not None
    assert section.splitlines()[0] == rtc.NEEDLE_HEADER
    link = f"[{needles['png'][:7]}](https://gitlab.suse.de/qa/needles/commit/{needles['png']})"
    assert f"{link} 2026-10-21 New login image: `login-2026.png`" in section
    assert "Remove grub2 needle" not in section
    assert needles["future"][:7] not in section
    assert _parsed_by_openqa(section) == []
    full = rtc.needle_section(path, modules, _job(), {})
    assert full is not None
    assert "`gone-2026.json` (removed)" in full
    assert "Tag grub2: `gone-2026.json`" in full


@pytest.mark.usefixtures("needles")
def test_needle_section_nothing_to_report(repo: pathlib.Path) -> None:
    path = repo / "products" / "sle" / "needles"
    quiet = _screen_details({"tags": ["unrelated"], "needles": [{"json": "other-2026.json"}]})["modules"]
    assert rtc.needle_section(path, quiet, _job(t_started="2026-09-01T00:00:00"), {}) is None
    assert rtc.needle_section(path, [], _job(), {}) is None
    modules = _screen_details(LOGIN_STEP)["modules"]
    assert rtc.needle_section(path, modules, _job(settings={"NEEDLES_GIT_REFSPEC": "x"}), {}) is None


@pytest.mark.usefixtures("needles", "history")
def test_sections_note_the_file_cap(repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rtc, "FILES_MAX", 1)
    path = repo / "products" / "sle" / "needles"
    step = {"tags": ["grub2", "x"], "needles": [{"json": "login-2026.json"}, {"json": "other-2026.json"}]}
    section = rtc.needle_section(path, _screen_details(step)["modules"], _job(), {})
    assert section is not None
    assert "* Only the first 1 of 4 involved needle files were checked." in section
    assert "* Only the first 1 of 2 involved tags were checked." in section
    details = _details("/x/sle/lib/x.pm", "/x/sle/tests/a/mod.pm")
    code = rtc.code_section(repo, details["modules"], _job(), {})
    assert code is not None
    assert "* Only the first 1 of 2 involved files were checked." in code
    assert "mod.pm" not in code.splitlines()[1]


@pytest.mark.usefixtures("repo", "history")
def test_build_comment_with_both_sections(
    needles: dict[str, str],
    make_openqa: Callable[[Mapping[str, Any]], OpenQA],
) -> None:
    details = _details("/x/sle/lib/x.pm")
    details["modules"][0]["details"].append({"num": 2, "result": "fail", **LOGIN_STEP})
    comment = rtc.build_comment(make_openqa(_routes(_job(), details, {"NEEDLES_GIT_HASH": needles["login"]})), "1")
    assert comment is not None
    code, needle = comment.split("\n\n")
    assert code.startswith(rtc.CODE_MARKER)
    assert needle.startswith(rtc.NEEDLE_MARKER)
    assert "[" + needles["login"][:7] + "](https://gitlab.suse.de/qa/needles/commit/" in needle
    assert _parsed_by_openqa(comment) == []


@pytest.mark.usefixtures("repo", "needles")
def test_build_comment_needles_only_and_dedupe(make_openqa: Callable[[Mapping[str, Any]], OpenQA]) -> None:
    details = _screen_details(LOGIN_STEP)
    details["modules"][0]["name"] = "nomod"
    routes = _routes(_job(), details)
    comment = rtc.build_comment(make_openqa(routes), "1")
    assert comment is not None
    assert comment.startswith(rtc.NEEDLE_MARKER)
    routes["/api/v1/jobs/1/comments"] = json.dumps([{"text": comment}])
    assert rtc.build_comment(make_openqa(routes), "1") is None


@pytest.mark.usefixtures("needles")
def test_build_comment_needles_without_tests_checkout(
    repo: pathlib.Path, make_openqa: Callable[[Mapping[str, Any]], OpenQA]
) -> None:
    inside = repo / "products" / "sle" / "needles"
    settings = {"DISTRI": "sle", "CASEDIR": "https://github.com/o/r.git", "NEEDLES_DIR": str(inside)}
    comment = rtc.build_comment(make_openqa(_routes(_job(settings=settings), _screen_details(LOGIN_STEP))), "1")
    assert comment is not None
    assert comment.startswith(rtc.NEEDLE_MARKER)
