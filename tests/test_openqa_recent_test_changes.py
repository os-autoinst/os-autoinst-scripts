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
    assert [sorted(c.files) for c in commits] == [["lib/x.pm"], ["lib/x.pm", "tests/a/mod.pm"]]
    assert commits[1].date == "2026-10-20"
    assert rtc.recent_commits(repo, "HEAD", STARTED, ["tests/b/nothing.pm"]) == []


def test_recent_commits_pinned_to_the_tested_hash(repo: pathlib.Path, history: dict[str, str]) -> None:
    late = STARTED + dt.timedelta(days=2)
    files = ["tests/a/mod.pm"]
    assert [c.sha for c in rtc.recent_commits(repo, "HEAD", late, files)] == [history["future"], history["a"]]
    assert rtc.recent_commits(repo, history["b"], late, files) == [
        rtc.Commit(history["a"], "2026-10-20", "Fix mod", ["tests/a/mod.pm"])
    ]


def test_tested_rev(repo: pathlib.Path, history: dict[str, str]) -> None:
    assert rtc.tested_rev(repo, {"TEST_GIT_HASH": history["b"]}) == history["b"]
    assert rtc.tested_rev(repo, {"TEST_GIT_HASH": "0" * 40}) == "HEAD"
    assert rtc.tested_rev(repo, {"TEST_GIT_HASH": "--output=/tmp/x"}) == "HEAD"
    assert rtc.tested_rev(repo, {}) == "HEAD"


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


def test_format_comment_cap_and_link() -> None:
    commits = [rtc.Commit(f"{i}" * 40, "2026-10-21", f"c{i}", ["tests/a.pm", "lib/b.pm"]) for i in range(7)]
    lines = rtc.format_comment("https://github.com/o/r/commit/", commits).splitlines()
    assert lines[0].startswith(rtc.COMMENT_MARKER)
    assert lines[1] == f"* [0000000](https://github.com/o/r/commit/{'0' * 40}) 2026-10-21 c0: `tests/a.pm`, `lib/b.pm`"
    assert len(lines) == 1 + rtc.COMMITS_MAX + 1
    assert lines[-1] == "* ... and 2 more commits"
    assert rtc.format_comment(None, commits[:1]).splitlines()[1].startswith("* `0000000` ")


def test_build_comment(history: dict[str, str], make_openqa: Callable[[Mapping[str, Any]], OpenQA]) -> None:
    details = _details("/var/lib/openqa/share/tests/sle/lib/x.pm", "/var/lib/openqa/share/tests/sle/tests/a/mod.pm")
    oqa = make_openqa(_routes(_job(), details, {"TEST_GIT_HASH": history["a"]}))
    comment = rtc.build_comment(oqa, "1")
    assert comment is not None
    assert comment.splitlines()[0].startswith(rtc.COMMENT_MARKER)
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
    routes["/api/v1/jobs/1/comments"] = json.dumps([{"text": f"{rtc.COMMENT_MARKER} files ..."}])
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
