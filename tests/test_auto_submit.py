# Copyright SUSE LLC
# ruff: file-ignore[boolean-type-hint-positional-argument, compare-to-empty-string, unused-variable]
"""Unit tests for os-autoinst-obs-auto-submit."""

from __future__ import annotations

import datetime
import importlib.machinery
import importlib.util
import logging
import pathlib
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# Load the script dynamically as a module
rootpath = pathlib.Path(__file__).parent.parent.resolve()
path = rootpath / "os-autoinst-obs-auto-submit"
spec = importlib.util.spec_from_file_location(
    "auto_submit",
    path,
    loader=importlib.machinery.SourceFileLoader("auto_submit", str(path)),
)
assert spec is not None
assert spec.loader is not None
auto_submit = importlib.util.module_from_spec(spec)
sys.modules["auto_submit"] = auto_submit
spec.loader.exec_module(auto_submit)


def test_is_transient_osc_error() -> None:
    exc = subprocess.CalledProcessError(1, "osc", stderr="HTTP Error 503: Service Unavailable")
    assert auto_submit.is_transient_osc_error(exc) is True

    exc_404 = subprocess.CalledProcessError(1, "osc", stderr="HTTP Error 404: Not Found")
    assert auto_submit.is_transient_osc_error(exc_404) is False


def test_get_obs_sr_id(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(
        ["osc"],
        0,
        stdout='<collection><request id="42"/></collection>',
    )
    res = auto_submit.get_obs_sr_id("openSUSE:Factory", "proj", "pkg", "osc", dry_run=False)
    assert res == "42"


def test_get_obs_sr_id_empty(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="<collection></collection>")
    res = auto_submit.get_obs_sr_id("openSUSE:Factory", "proj", "pkg", "osc", dry_run=False)
    assert res == ""


@pytest.mark.parametrize(
    ("target", "days", "pr_json", "sr_stdout", "expected"),
    [
        ("openSUSE:Factory", 0, None, "", False),
        ("openSUSE:Factory", 1, None, "openSUSE:Factory", True),
        ("openSUSE:Factory", 1, None, "different_target", False),
        ("openSUSE:Leap:16.0", 1, [], "", False),
        (
            "openSUSE:Leap:16.0",
            3,
            [
                {
                    "updated_at": (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                    ),
                    "html_url": "https://foo/bar",
                    "user": {"login": "os-autoinst-obs-workflow"},
                    "base": {"ref": "leap-16.0"},
                },
            ],
            "",
            True,
        ),
        (
            "openSUSE:Leap:16.0",
            1,
            [
                {
                    "updated_at": (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                    ),
                    "html_url": "https://foo/bar",
                    "user": {"login": "os-autoinst-obs-workflow"},
                    "base": {"ref": "leap-16.0"},
                },
            ],
            "",
            False,
        ),
    ],
)
def test_has_pending_submission(
    mocker: MockerFixture,
    target: str,
    days: int,
    pr_json: list[dict[str, Any]] | None,
    sr_stdout: str,
    expected: bool,
) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    if pr_json is not None:
        mock_run.return_value = subprocess.CompletedProcess(["git-obs"], 0, stdout=auto_submit.json.dumps(pr_json))
    else:
        mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout=sr_stdout)

    submitter = auto_submit.AutoSubmitter(
        dst_project="proj",
        throttle_days=days,
        throttle_days_leap_16=days,
        git_user="os-autoinst-obs-workflow",
        osc_cmd_str="osc",
        git_obs_cmd_str="git-obs",
        dry_run=False,
    )
    res = submitter.has_pending_submission(
        package="openQA",
        target=target,
    )
    assert res is expected


def test_prepare_local_clone_fetches_before_switch(mocker: MockerFixture) -> None:
    """Fetch parent before creating the branch so a fork lacking it still works."""
    mock_run = mocker.patch("auto_submit.subprocess.run")
    mocker.patch("auto_submit.pathlib.Path.iterdir", return_value=[])
    mocker.patch("auto_submit.diff_has_single_added_block", return_value=(True, None))
    submitter = auto_submit.AutoSubmitter(dst_project="dst", git_cmd_str="git", dir=pathlib.Path(), dry_run=False)
    submitter._prepare_local_clone("openQA", "leap-16.0")
    git_calls = [call.args[0] for call in mock_run.call_args_list]
    assert git_calls[0] == ["git", "fetch", "parent"]
    assert git_calls[1] == ["git", "switch", "-C", "leap-16.0", "parent/leap-16.0"]


def test_prepare_local_clone_dry_run_logs_fetch_first(caplog: pytest.LogCaptureFixture) -> None:
    submitter = auto_submit.AutoSubmitter(dst_project="dst", git_cmd_str="git", dry_run=True)
    with caplog.at_level("INFO"):
        submitter._prepare_local_clone("openQA", "leap-16.0")
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == "[dry-run] Would execute: git fetch parent"
    assert messages[1] == "[dry-run] Would execute: git switch -C leap-16.0 parent/leap-16.0"
    assert messages[2] == "[dry-run] Would execute: git merge-base origin/leap-16.0 leap-16.0"
    assert messages[3] == "[dry-run] Would execute: git rev-list <merge-base>..HEAD"
    assert messages[4] == "[dry-run] Would execute: git lfs fetch parent <commits>"
    assert messages[5] == "[dry-run] Would replace files in clone with files from ../../openQA/*"


def test_prepare_local_clone_calls_fetch_lfs(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.subprocess.run")
    mock_fetch_lfs = mocker.patch("auto_submit.AutoSubmitter._fetch_lfs_objects")
    mock_copy = mocker.patch("auto_submit.AutoSubmitter._copy_files_to_clone")
    submitter = auto_submit.AutoSubmitter(dst_project="dst", git_cmd_str="git", dir=pathlib.Path(), dry_run=False)
    submitter._prepare_local_clone("openQA", "leap-16.0")

    git_calls = [call.args[0] for call in mock_run.call_args_list]
    assert git_calls[0] == ["git", "fetch", "parent"]
    assert git_calls[1] == ["git", "switch", "-C", "leap-16.0", "parent/leap-16.0"]
    mock_fetch_lfs.assert_called_once_with("leap-16.0")
    mock_copy.assert_called_once_with("openQA")


def test_prepare_local_clone_diff_not_ok(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    mock_run = mocker.patch("auto_submit.subprocess.run")
    mock_fetch_lfs = mocker.patch("auto_submit.AutoSubmitter._fetch_lfs_objects")
    mock_copy = mocker.patch("auto_submit.AutoSubmitter._copy_files_to_clone")
    submitter = auto_submit.AutoSubmitter(dst_project="dst", git_cmd_str="git", dir=pathlib.Path(), dry_run=False)
    mocker.patch(
        "auto_submit.diff_has_single_added_block",
        return_value=(False, subprocess.CompletedProcess("", 0, stdout="diff output", stderr="")),
    )
    res = submitter._prepare_local_clone("pkg", "leap-16.0")
    assert res is False
    assert caplog.records[2].getMessage() == "git diff for pkg.changes does not look ok:\ndiff output"


def test_fetch_lfs_objects_with_merge_base(mocker: MockerFixture) -> None:
    def mocked_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "merge-base":
            return subprocess.CompletedProcess(cmd, 0, stdout="0c9eddb\n", stderr="")
        if cmd[1] == "rev-list":
            return subprocess.CompletedProcess(cmd, 0, stdout="554e99\n9d1bd5\n", stderr="")
        if cmd[1:3] == ["lfs", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run = mocker.patch("auto_submit.subprocess.run", side_effect=mocked_run)
    submitter = auto_submit.AutoSubmitter(git_cmd_str="git", dry_run=False)
    submitter._fetch_lfs_objects("leap-16.0")

    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls[0] == ["git", "merge-base", "origin/leap-16.0", "leap-16.0"]
    assert calls[1] == ["git", "rev-list", "0c9eddb..HEAD"]
    assert calls[2] == ["git", "lfs", "fetch", "parent", "554e99", "9d1bd5"]


def test_fetch_lfs_objects_no_new_commits(mocker: MockerFixture) -> None:
    def mocked_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "merge-base":
            return subprocess.CompletedProcess(cmd, 0, stdout="0c9eddb\n", stderr="")
        if cmd[1] == "rev-list":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run = mocker.patch("auto_submit.subprocess.run", side_effect=mocked_run)
    submitter = auto_submit.AutoSubmitter(git_cmd_str="git", dry_run=False)
    submitter._fetch_lfs_objects("leap-16.0")

    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls[0] == ["git", "merge-base", "origin/leap-16.0", "leap-16.0"]
    assert calls[1] == ["git", "rev-list", "0c9eddb..HEAD"]
    assert len(calls) == 2


def test_fetch_lfs_objects_fallback_no_origin_branch(mocker: MockerFixture) -> None:
    def mocked_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "merge-base":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fatal: Not a valid object name")
        if cmd[1] == "rev-list":
            return subprocess.CompletedProcess(cmd, 0, stdout="c1\nc2\n", stderr="")
        if cmd[1:3] == ["lfs", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run = mocker.patch("auto_submit.subprocess.run", side_effect=mocked_run)
    submitter = auto_submit.AutoSubmitter(git_cmd_str="git", dry_run=False)
    submitter._fetch_lfs_objects("leap-16.0")

    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls[0] == ["git", "merge-base", "origin/leap-16.0", "leap-16.0"]
    assert calls[1] == ["git", "rev-list", "--not", "--remotes=origin", "HEAD"]
    assert calls[2] == ["git", "lfs", "fetch", "parent", "c1", "c2"]


def test_fetch_lfs_objects_batching(mocker: MockerFixture) -> None:
    fake_commits = [f"commit{i:03d}" for i in range(105)]

    def mocked_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "merge-base":
            return subprocess.CompletedProcess(cmd, 0, stdout="base_sha\n", stderr="")
        if cmd[1] == "rev-list":
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(fake_commits) + "\n", stderr="")
        if cmd[1:3] == ["lfs", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run = mocker.patch("auto_submit.subprocess.run", side_effect=mocked_run)
    submitter = auto_submit.AutoSubmitter(git_cmd_str="git", dry_run=False)
    submitter._fetch_lfs_objects("leap-16.0")

    calls = [call.args[0] for call in mock_run.call_args_list]
    assert calls[0] == ["git", "merge-base", "origin/leap-16.0", "leap-16.0"]
    assert calls[1] == ["git", "rev-list", "base_sha..HEAD"]
    assert calls[2] == ["git", "lfs", "fetch", "parent", *fake_commits[0:50]]
    assert calls[3] == ["git", "lfs", "fetch", "parent", *fake_commits[50:100]]
    assert calls[4] == ["git", "lfs", "fetch", "parent", *fake_commits[100:105]]
    assert len(calls) == 5


def test_fetch_lfs_objects_real_git_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that _fetch_lfs_objects downloads missing LFS objects in a real git repository."""
    # Setup a "parent" Git repository with LFS and an initial commit
    git_bin = shutil.which("git") or "/usr/bin/git"
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    subprocess.run([git_bin, "init", "-b", "leap-16.0", str(parent_dir)], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "config", "user.name", "Test User"], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "lfs", "install", "--local"], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "lfs", "track", "*.obscpio"], check=True)
    (parent_dir / "base.txt").write_text("base content\n", encoding="utf-8")
    subprocess.run([git_bin, "-C", str(parent_dir), "add", ".gitattributes", "base.txt"], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "commit", "-m", "commit 1: initial"], check=True)

    # Clone the "parent" Git repo into an "origin" repo and configure this as "origin" remote in the "parent" repo
    origin_dir = tmp_path / "origin"
    subprocess.run([git_bin, "clone", "--bare", str(parent_dir), str(origin_dir)], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "remote", "add", "origin", str(origin_dir)], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "push", "origin", "leap-16.0"], check=True)

    # Commit 2 on parent: add intermediate LFS object
    (parent_dir / "intermediate.obscpio").write_text("intermediate lfs content\n", encoding="utf-8")
    subprocess.run([git_bin, "-C", str(parent_dir), "add", "intermediate.obscpio"], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "commit", "-m", "commit 2: add intermediate"], check=True)

    # Commit 3 on parent: remove intermediate LFS object
    subprocess.run([git_bin, "-C", str(parent_dir), "rm", "intermediate.obscpio"], check=True)
    subprocess.run([git_bin, "-C", str(parent_dir), "commit", "-m", "commit 3: remove intermediate"], check=True)

    # Clone "origin" repo to another repo "local" and setup LFS as well
    local_dir = tmp_path / "local"
    subprocess.run([git_bin, "clone", str(origin_dir), str(local_dir)], check=True)
    subprocess.run([git_bin, "-C", str(local_dir), "config", "user.name", "Test User"], check=True)
    subprocess.run([git_bin, "-C", str(local_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run([git_bin, "-C", str(local_dir), "lfs", "install", "--local"], check=True)

    # Fetch "parent" commits into "local" and switch to "parent/leap-16.0"
    subprocess.run([git_bin, "-C", str(local_dir), "remote", "add", "parent", str(parent_dir)], check=True)
    subprocess.run([git_bin, "-C", str(local_dir), "fetch", "parent"], check=True)
    subprocess.run([git_bin, "-C", str(local_dir), "switch", "-C", "leap-16.0", "parent/leap-16.0"], check=True)

    # Verify that pushing now fails because intermediate.obscpio from commit 2 is missing locally
    push_res = subprocess.run(
        [git_bin, "-C", str(local_dir), "push", "origin", "leap-16.0"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert push_res.returncode != 0
    assert "intermediate.obscpio" in push_res.stdout + push_res.stderr

    # Use _fetch_lfs_objects to fetch missing LFS objects from parent
    monkeypatch.chdir(local_dir)
    submitter = auto_submit.AutoSubmitter(git_cmd_str=git_bin, dry_run=False)
    submitter._fetch_lfs_objects("leap-16.0")

    # Verify that pushing succeeds after fetching missing LFS objects via _fetch_lfs_objects
    subprocess.run([git_bin, "-C", str(local_dir), "push", "origin", "leap-16.0"], check=True)


def test_copy_files_to_clone(
    mocker: MockerFixture,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_extract = mocker.patch("auto_submit.extract_obscpio", return_value=True)
    sourcedir = tmp_path / "dst" / "pkg"
    clonedir = tmp_path / "git" / "pkg"
    sourcedir.mkdir(parents=True)
    clonedir.mkdir(parents=True)
    (sourcedir / "node_modules.spec.inc").touch()
    (sourcedir / "node_modules.obscpio").touch()
    (sourcedir / "newfile1").touch()
    (sourcedir / "newdir1").mkdir()
    (sourcedir / ".osc").mkdir()
    (clonedir / "oldfile1").touch()
    (clonedir / ".gitignore").touch()
    (clonedir / ".gitattributes").touch()
    (clonedir / "olddir1").mkdir()
    (clonedir / ".git").mkdir()

    monkeypatch.chdir(clonedir)
    submitter = auto_submit.AutoSubmitter(
        dir=tmp_path,
        dst_project="dst",
    )
    submitter._copy_files_to_clone("pkg")

    mock_extract.assert_called_once_with(sourcedir / "node_modules.obscpio", target_dir=pathlib.Path("node_modules"))
    assert (clonedir / "newfile1").exists()
    assert (clonedir / "newdir1").exists()
    assert (clonedir / ".git").exists()
    assert (clonedir / ".gitignore").exists()
    assert (clonedir / ".gitattributes").exists()
    assert not (clonedir / "node_modules.spec.inc").exists()
    assert not (clonedir / ".osc").exists()


def test_make_obs_submit_request_success(mocker: MockerFixture) -> None:
    mocker.patch("auto_submit.get_obs_sr_id", return_value="23")
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
    )
    res = submitter.make_obs_submit_request("pkg", "Factory", "3.14")
    assert res is True
    mock_run.assert_called_once_with(
        ["osc", "sr", "-s", "23", "-m", "Update to 3.14", "dst", "pkg", "Factory"],
        dry_run=False,
        mutating=True,
    )


def test_make_obs_submit_request_new(mocker: MockerFixture) -> None:
    mocker.patch("auto_submit.get_obs_sr_id", return_value="")
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
    )
    res = submitter.make_obs_submit_request("pkg", "Factory", "3.14")
    assert res is True
    mock_run.assert_called_once_with(
        ["osc", "sr", "-m", "Update to 3.14", "dst", "pkg", "Factory"],
        dry_run=False,
        mutating=True,
    )


def test_make_obs_submit_request_failure(mocker: MockerFixture) -> None:
    mocker.patch("auto_submit.get_obs_sr_id", return_value="")
    mock_run = mocker.patch("auto_submit.run_osc_cmd", side_effect=subprocess.CalledProcessError(1, "sr"))
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
    )
    res = submitter.make_obs_submit_request("pkg", "Factory", "3.14")
    assert res is False


def test_last_revision(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    sha = "c0f8ee6a233ed250dbc54c19dee50118"
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(
        ["osc"],
        0,
        stdout=f"* Update to version 162312.{sha}:\n  * fix: foo\n  * feat: bar\n  * perf: boo\n",
    )
    res = auto_submit.last_revision("proj", "pkg", "Factory", "osc")
    assert res == sha
    assert (
        re.search(r"First 4 lines of 'proj/pkg/_service:obs_scm:pkg.changes'", caplog.records[0].getMessage())
        is not None
    )

    assert caplog.records[1].getMessage() == f"Last revision for 'proj/pkg': {sha}"


def test_last_revision_none(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="")
    res = auto_submit.last_revision("proj", "pkg", "Factory", "osc")
    assert res == ""


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", "unknown"),
        ("   \n  ", "unknown"),
        ("Package pkg is not yet ready for release\nscheduled", "Package pkg is not yet ready for release\nscheduled"),
        ('{"failed_jobs": [123, 456]}', "failed openQA jobs: 123, 456"),
        ('{"failed_jobs": []}', '{"failed_jobs": []}'),
        ('{"other": 1}', '{"other": 1}'),
        ("not json { at all", "not json { at all"),
    ],
)
def test_format_skip_reason(content: str, expected: str) -> None:
    assert auto_submit._format_skip_reason(content) == expected


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("single line", "Skipping submission, reason: single line"),
        ("note\npkg1\npkg2", "Skipping submission, reason:\n  note\n  pkg1\n  pkg2"),
    ],
)
def test_log_skip_reason(reason: str, expected: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        auto_submit._log_skip_reason(reason)
    assert caplog.records[0].getMessage() == expected


def test_get_packages_to_submit_env(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {"PACKAGES": "pkg1 pkg2"})
    res = auto_submit._get_packages_to_submit("dst", "osc", dry_run=False)
    assert res == ["pkg1", "pkg2"]


def test_get_packages_to_submit_osc(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {}, clear=True)
    mock_run = mocker.patch("auto_submit.run_osc_cmd")
    mock_run.return_value = subprocess.CompletedProcess(["osc"], 0, stdout="pkg1\npkg2-test\npkg3\n")
    res = auto_submit._get_packages_to_submit("dst", "osc", dry_run=True)
    assert res == ["pkg1", "pkg3"]
    mock_run.assert_called_once_with(["osc", "ls", "dst"], dry_run=True, mutating=False)


@pytest.mark.parametrize(
    ("verbose", "quiet", "expected_level"),
    [
        (0, 0, logging.INFO),
        (1, 0, logging.DEBUG),
        (0, 1, logging.WARNING),
        (0, 2, logging.ERROR),
        (0, 3, logging.CRITICAL),
    ],
)
def test_main_logging(mocker: MockerFixture, verbose: int, quiet: int, expected_level: int) -> None:
    mocker.patch("auto_submit._run_submissions")
    mock_basic_config = mocker.patch("logging.basicConfig")
    auto_submit.main(verbose=verbose, quiet=quiet)
    mock_basic_config.assert_called_with(level=expected_level, format="%(levelname)s: %(message)s", force=True)


def test_run_submissions_force(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_exists = mocker.patch("auto_submit.pathlib.Path.exists", return_value=True)
    mock_unlink = mocker.patch("auto_submit.pathlib.Path.unlink")
    mocker.patch("auto_submit._get_packages_to_submit", return_value=["pkg1"])
    mocker.patch("auto_submit.AutoSubmitter")

    auto_submit._run_submissions(
        src_project="devel:openQA",
        dst_project="devel:openQA:tested",
        staging_project="devel:openQA:testing",
        submit_target="openSUSE:Factory",
        dry_run=False,
        force=True,
        osc_poll_interval=2,
        osc_build_start_poll_tries=90,
        throttle_days=2,
        throttle_days_leap_16=7,
        git_user="user",
        submit_target_extra="none",
        packages=None,
        skip_wait_for_build=False,
        cleanup_dir=True,
    )

    mock_unlink.assert_called_once()
    mock_run.assert_any_call(
        ["cleanup-obs-project", "devel:openQA:testing", "I am sure"],
        capture_output=False,
        text=False,
        check=True,
    )


def test_run_osc_cmd_error_logging(caplog: pytest.LogCaptureFixture, mocker: MockerFixture) -> None:
    mock_run = mocker.patch(
        "auto_submit.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["osc", "sr"], output="some stdout", stderr="some stderr"),
    )

    with caplog.at_level("ERROR"), pytest.raises(subprocess.CalledProcessError):
        auto_submit.run_osc_cmd(["osc", "sr"], dry_run=False, mutating=True)

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "Command 'osc sr' failed with exit code 1" in messages[0]
    assert "Command stdout:\nsome stdout" in messages[0]
    assert "Command stderr:\nsome stderr" in messages[0]


def test_update_package_no_changes(
    caplog: pytest.LogCaptureFixture,
    mocker: MockerFixture,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocker.patch("auto_submit.AutoSubmitter._disable_service_buildtime", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._cleanup_and_rename_files", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._find_version", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._osc_addremove_and_filter_specs", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._commit_local_changes", return_value=auto_submit.CommitResult.NO_CHANGES)
    mocker.patch("auto_submit._run_cmd", return_value=True)

    content = "Line 1\nLine 2"
    changes_file = "pkg.changes"

    (tmp_path / "dst" / "pkg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dst" / "pkg" / changes_file).write_text(content, encoding="utf-8")

    monkeypatch.chdir(tmp_path / "dst" / "pkg")
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
        targets=["openSUSE:Leap:16.0"],
    )
    caplog.set_level(logging.INFO)
    res = submitter.update_package("pkg")
    assert caplog.records[0].getMessage() == "update_package pkg"
    assert len(caplog.records) == 1
    assert res is True
    assert not (tmp_path / "git-repos" / "pkg" / changes_file).exists()


def test_update_package_failure(
    caplog: pytest.LogCaptureFixture,
    mocker: MockerFixture,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocker.patch("auto_submit.AutoSubmitter._disable_service_buildtime", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._cleanup_and_rename_files", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._find_version", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._osc_addremove_and_filter_specs", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._commit_local_changes", return_value=auto_submit.CommitResult.FAILURE)
    mocker.patch("auto_submit._run_cmd", return_value=True)

    content = "Line 1\nLine 2"
    changes_file = "pkg.changes"

    (tmp_path / "dst" / "pkg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dst" / "pkg" / changes_file).write_text(content, encoding="utf-8")

    monkeypatch.chdir(tmp_path / "dst" / "pkg")
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
        targets=["openSUSE:Leap:16.0"],
    )
    caplog.set_level(logging.INFO)
    res = submitter.update_package("pkg")
    assert caplog.records[0].getMessage() == "update_package pkg"
    assert len(caplog.records) == 1
    assert res is False
    assert not (tmp_path / "git-repos" / "pkg" / changes_file).exists()


def test_update_package(
    caplog: pytest.LogCaptureFixture,
    mocker: MockerFixture,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocker.patch("auto_submit.AutoSubmitter._disable_service_buildtime", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._cleanup_and_rename_files", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._find_version", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._osc_addremove_and_filter_specs", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._commit_local_changes", return_value=auto_submit.CommitResult.SUCCESS)
    mocker.patch("auto_submit.diff_has_single_added_block", return_value=(True, None))
    mocker.patch("auto_submit.AutoSubmitter._commit_and_push", return_value=True)
    mocker.patch("auto_submit.AutoSubmitter._create_pull_request", return_value=True)
    mocker.patch("auto_submit.AutoSubmitter.has_pending_submission", return_value=False)
    mocker.patch("auto_submit._run_cmd", return_value=True)

    def mocked_clone(package: str) -> str:
        (pathlib.Path() / package).mkdir(parents=True, exist_ok=True)
        return "owner/" + package

    mocker.patch("auto_submit.AutoSubmitter._fork_and_clone_repo", return_value=True, side_effect=mocked_clone)

    content = "Line 1\nLine 2"
    changes_file = "pkg.changes"

    (tmp_path / "dst" / "pkg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dst" / "pkg" / changes_file).write_text(content, encoding="utf-8")

    monkeypatch.chdir(tmp_path / "dst" / "pkg")
    submitter = auto_submit.AutoSubmitter(
        dir=tmp_path,
        dst_project="dst",
        osc_cmd_str="osc",
        dry_run=False,
        targets=["openSUSE:Leap:16.0"],
    )
    caplog.set_level(logging.INFO)
    res = submitter.update_package("pkg")
    assert caplog.records[0].getMessage() == "update_package pkg"
    assert caplog.records[1].getMessage() == f"First 2 lines of '{changes_file}':\n{content}"
    assert res is True
    assert (tmp_path / "git-repos" / "pkg" / changes_file).exists()


def test_handle_auto_submit_copy_files(
    mocker: MockerFixture,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocker.patch("auto_submit.AutoSubmitter._osc_co", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._is_package_up_to_date", return_value=False)
    mocker.patch("auto_submit.AutoSubmitter.update_package", return_value=True)
    mocker.patch("auto_submit.diff_has_single_added_block", return_value=(True, None))

    monkeypatch.chdir(tmp_path)
    dstdir = tmp_path / "dst" / "pkg"
    dstdir.mkdir(parents=True, exist_ok=True)
    srcdir = tmp_path / "src" / "pkg"
    srcdir.mkdir(parents=True, exist_ok=True)
    (srcdir / "newfile").write_text("new")
    (dstdir / "oldfile").write_text("old")
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        src_project="src",
        dry_run=False,
        osc_cmd_str="osc",
    )
    res = submitter.handle_auto_submit("pkg")
    assert res is True
    assert (dstdir / "newfile").exists()
    assert not (dstdir / "oldfile").exists()


def test_handle_auto_submit_diff_not_ok(
    caplog: pytest.LogCaptureFixture,
    mocker: MockerFixture,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mocker.patch("auto_submit.AutoSubmitter._osc_co", return_value="23")
    mocker.patch("auto_submit.AutoSubmitter._is_package_up_to_date", return_value=False)
    mocker.patch("auto_submit.AutoSubmitter.update_package", return_value=True)
    mocker.patch(
        "auto_submit.diff_has_single_added_block",
        return_value=(False, subprocess.CompletedProcess("", 0, stdout="diff output", stderr="")),
    )

    monkeypatch.chdir(tmp_path)
    (tmp_path / "dst" / "pkg").mkdir(parents=True, exist_ok=True)
    submitter = auto_submit.AutoSubmitter(
        dst_project="dst",
        src_project="src",
        dry_run=False,
        osc_cmd_str="osc",
    )
    res = submitter.handle_auto_submit("pkg")
    assert res is False
    assert caplog.records[0].getMessage() == "osc diff for pkg.changes does not look ok:\ndiff output"


def test_cpio(tmp_path: pathlib.Path) -> None:
    cpio = pathlib.Path() / "tests" / "data" / "test.cpio"

    auto_submit.extract_obscpio(cpio, target_dir=tmp_path)
    target = tmp_path / "cpio-dir"
    assert (target / "file1").exists()
    assert (target / "subdir" / "file2").exists()


def test_replace_node_modules_handling(
    caplog: pytest.LogCaptureFixture,
    tmp_path: pathlib.Path,
) -> None:
    caplog.set_level(logging.INFO)

    specfile = tmp_path / "openQA.spec"
    content = """
Source0:        %{name}-%{version}.tar.xz
Source1:        openQA-rpmlintrc
Source2:        node_modules.spec.inc
%include        %{_sourcedir}/node_modules.spec.inc
BuildRequires:  fdupes
"""
    specfile.write_text(content)
    expected = """
Source0:        %{name}-%{version}.tar.xz
Source1:        openQA-rpmlintrc
#!CreateArchive
Source10:       node_modules.tar.gz
BuildRequires:  fdupes
"""

    auto_submit.replace_node_modules_handling(specfile)

    assert specfile.read_text() == expected
    assert caplog.records[0].getMessage() == "Successfully updated openQA.spec"

    caplog.clear()

    content = """
Source0:        %{name}-%{version}.tar.xz
Source1:        openQA-rpmlintrc
BuildRequires:  fdupes
"""
    specfile.write_text(content)
    expected = content

    auto_submit.replace_node_modules_handling(specfile)

    assert specfile.read_text() == expected
    assert (
        caplog.records[0].getMessage() == "openQA.spec does not contain node_modules specific sources; no changes made."
    )


def test_diff_has_single_added_block(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("auto_submit._run_subprocess")
    diff_header = """
Index: os-autoinst.changes
===================================================================
--- os-autoinst.changes (revision 528)
+++ os-autoinst.changes (working copy)
@@ -1,3 +1,11 @@
+-------------------------------------------------------------------
+- Update to version 5.1790592325.87022d5:
+  * fix: one
+  * fix: two
+
    """
    diff = diff_header
    mock_run.return_value = subprocess.CompletedProcess(["dummy"], 0, stdout=diff)
    (diff_ok, res) = auto_submit.diff_has_single_added_block(["dummy"])
    assert diff_ok is True

    diff = (
        diff_header
        + """
 -------------------------------------------------------------------

@@ -5,6 +13,7 @@
   * fix: three
+  * added commit
"""
    )
    mock_run.return_value = subprocess.CompletedProcess(["dummy"], 0, stdout=diff)
    (diff_ok, res) = auto_submit.diff_has_single_added_block(["dummy"])
    assert diff_ok is False

    diff = (
        diff_header
        + """
 -------------------------------------------------------------------

@@ -5,6 +13,7 @@
   * fix: three
-  * removed commit
"""
    )
    mock_run.return_value = subprocess.CompletedProcess(["dummy"], 0, stdout=diff)
    (diff_ok, res) = auto_submit.diff_has_single_added_block(["dummy"])
    assert diff_ok is False
    assert res is None
