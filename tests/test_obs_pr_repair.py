# Copyright SUSE LLC
"""Tests for obs-pr-repair: workflow parsing, repository checks and the repair actions."""

import argparse
import base64
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "obs-pr-repair"
loader = importlib.machinery.SourceFileLoader("obs_pr_repair", str(SCRIPT_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
sut = importlib.util.module_from_spec(spec)
sys.modules["obs_pr_repair"] = sut
loader.exec_module(sut)

SAMPLE_OPENQA_WORKFLOW = """---
pr:
  steps:
    - branch_package:
        source_project: devel:openQA
        source_package: openQA
        target_project: devel:openQA:GitHub
        add_repositories: disabled
    - configure_repositories:
        project: devel:openQA:GitHub
        repositories:
          - name: openSUSE_Tumbleweed
            paths:
              - target_project: openSUSE:Factory
                target_repository: snapshot
              - target_project: devel:openQA
                target_repository: openSUSE_Tumbleweed
            architectures: [ x86_64 ]
          - name: SLE_15_SP7_Backports
            architectures: [ x86_64 ]
          - name: '16.0'
            architectures: [ x86_64 ]
"""

SAMPLE_OS_AUTOINST_WORKFLOW = """---
pr:
  steps:
    - branch_package:
        source_project: devel:openQA
        source_package: os-autoinst
        target_project: devel:openQA:GitHub
        add_repositories: disabled
    - configure_repositories:
        project: devel:openQA:GitHub
        repositories:
          - name: openSUSE_Tumbleweed
            architectures: [ x86_64 ]
"""


def make_completed_process(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["mock"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


EMPTY_PRJ_META = '<project name="p"><url>https://github.com/os-autoinst/openQA/pull/7985</url></project>'

HEALTHY_RESULTS_XML = (
    '<resultlist><result project="p" repository="TW" arch="x86_64">'
    '<status package="openQA" code="succeeded"/></result></resultlist>'
)


def openqa_context(pr_number: int = 7985) -> sut.ProjectContext:
    return sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=pr_number,
        workflow=sut.parse_workflow_content(SAMPLE_OPENQA_WORKFLOW),
        obs_project=f"devel:openQA:GitHub:os-autoinst:openQA:PR-{pr_number}",
    )


MISSING_PRJ_STDERR = "Server returned an error: HTTP Error 404: Not Found"


def osc_meta_response(_cmd: Sequence[str], state: dict) -> subprocess.CompletedProcess[str]:
    """Return the stored project meta, or the OBS 404 when the project is missing."""
    if state.get("project_missing"):
        return make_completed_process(returncode=1, stderr=MISSING_PRJ_STDERR)
    return make_completed_process(stdout=state["meta"])


def osc_ls_response(cmd: Sequence[str], state: dict) -> subprocess.CompletedProcess[str]:
    """Return the canned package listing for a project query, the tracked files for a package query."""
    return make_completed_process(stdout=state["files"] if len(cmd) == 4 else "openQA\n")


def osc_branch_response(_cmd: Sequence[str], state: dict) -> subprocess.CompletedProcess[str]:
    """Mark the package as non-empty and return the branch result driven by branch_code."""
    state["files"] = "_service\nopenQA.spec\n"
    return make_completed_process(returncode=state.get("branch_code", 0))


def osc_api_response(cmd: Sequence[str], state: dict) -> subprocess.CompletedProcess[str]:
    """Record the submitted prj meta file, return OK for the PUT and a success for other calls."""
    if cmd[-1].endswith("_meta"):
        state["meta"] = Path(cmd[5]).read_text(encoding="utf-8")
        return make_completed_process(stdout="OK")
    return make_completed_process(returncode=0)


def osc_results_response(cmd: Sequence[str], state: dict) -> subprocess.CompletedProcess[str]:
    """Return the stored results XML for an --xml query, the plain one-line table otherwise."""
    return make_completed_process(stdout=state["results_xml"] if "--xml" in cmd else "openQA  x  TW x86_64\n")


def gh_view_response() -> subprocess.CompletedProcess[str]:
    """Return the canned head ref of okurz/openQA for a gh pr view query."""
    payload = {"headRefOid": "abc12345def", "headRepository": {"nameWithOwner": "okurz/openQA"}}
    return make_completed_process(stdout=json.dumps(payload))


OSC_HANDLERS = {
    ("meta", "prj"): osc_meta_response,
    "ls": osc_ls_response,
    "branch": osc_branch_response,
    "api": osc_api_response,
    "results": osc_results_response,
}


def make_state_runner(
    state: dict, calls: list[list[str]]
) -> Callable[[Sequence[str]], subprocess.CompletedProcess[str]]:
    """Return a runner that records every command and answers it from the shared state."""

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "view"]:
            return gh_view_response()
        key = tuple(cmd[1:3]) if cmd[1] == "meta" else cmd[1]
        handler = OSC_HANDLERS.get(key)
        return handler(cmd, state) if handler else make_completed_process(returncode=0)

    return fake_runner


@pytest.mark.parametrize(
    (
        "desc",
        "workflow_yaml",
        "expected_target",
        "expected_src_prj",
        "expected_src_pkg",
        "expected_repos",
    ),
    [
        (
            "openQA workflow extracts correct targets and all three repositories",
            SAMPLE_OPENQA_WORKFLOW,
            "devel:openQA:GitHub",
            "devel:openQA",
            "openQA",
            ["openSUSE_Tumbleweed", "SLE_15_SP7_Backports", "16.0"],
        ),
        (
            "os-autoinst workflow extracts correct package and single repository",
            SAMPLE_OS_AUTOINST_WORKFLOW,
            "devel:openQA:GitHub",
            "devel:openQA",
            "os-autoinst",
            ["openSUSE_Tumbleweed"],
        ),
        (
            "empty or missing steps returns empty string fields and empty repo list",
            "---\npr: {}\n",
            "",
            "",
            "",
            [],
        ),
    ],
)
def test_parse_workflow_content(
    desc: str,
    workflow_yaml: str,
    expected_target: str,
    expected_src_prj: str,
    expected_src_pkg: str,
    expected_repos: list[str],
) -> None:
    config = sut.parse_workflow_content(workflow_yaml)
    assert config.target_project == expected_target, desc
    assert config.source_project == expected_src_prj, desc
    assert config.source_package == expected_src_pkg, desc
    assert config.target_repositories == expected_repos, desc


def test_parse_workflow_content_repository_details() -> None:
    config = sut.parse_workflow_content(SAMPLE_OPENQA_WORKFLOW)
    assert [repo.name for repo in config.repositories] == [
        "openSUSE_Tumbleweed",
        "SLE_15_SP7_Backports",
        "16.0",
    ]
    assert config.repositories[0].paths == [
        ("openSUSE:Factory", "snapshot"),
        ("devel:openQA", "openSUSE_Tumbleweed"),
    ]
    assert config.repositories[0].architectures == ["x86_64"]
    assert config.repositories[1].paths == []


@pytest.mark.parametrize(
    ("entries", "expected_names", "expected_paths"),
    [
        (
            [
                {
                    "name": "TW",
                    "paths": [{"target_project": "a", "target_repository": "b"}],
                }
            ],
            ["TW"],
            [("a", "b")],
        ),
        ([{"name": 17}], ["17"], []),
        (
            [
                "string",
                {"no_name": 1},
                {"name": "TW", "paths": [{"target_project": "a"}]},
            ],
            ["TW"],
            [],
        ),
        ([], [], []),
    ],
)
def test_parse_repository_definitions_skips_invalid_entries(
    entries: list[object],
    expected_names: list[str],
    expected_paths: list[tuple[str, str]],
) -> None:
    repos = sut.parse_repository_definitions(entries)
    assert [repo.name for repo in repos] == expected_names
    assert [path for repo in repos for path in repo.paths] == expected_paths


@pytest.mark.parametrize(
    ("desc", "url", "expected"),
    [
        (
            "SSH remote with git suffix extracts owner and repo",
            "git@github.com:os-autoinst/openQA.git",
            ("os-autoinst", "openQA"),
        ),
        (
            "SSH remote without git suffix extracts owner and repo",
            "git@github.com:os-autoinst/os-autoinst",
            ("os-autoinst", "os-autoinst"),
        ),
        (
            "HTTPS remote with git suffix extracts owner and repo",
            "https://github.com/os-autoinst/openQA.git",
            ("os-autoinst", "openQA"),
        ),
        (
            "HTTPS remote without git suffix extracts owner and repo",
            "https://github.com/someuser/test-repo",
            ("someuser", "test-repo"),
        ),
        (
            "non-github remote returns None",
            "https://gitlab.com/group/repo.git",
            None,
        ),
    ],
)
def test_parse_github_remote_url(desc: str, url: str, expected: tuple[str, str] | None) -> None:
    assert sut.parse_github_remote_url(url) == expected, desc


@pytest.mark.parametrize(
    ("desc", "target_prj", "owner", "repo", "pr_number", "expected"),
    [
        (
            "openQA PR canonical project path",
            "devel:openQA:GitHub",
            "os-autoinst",
            "openQA",
            7968,
            "devel:openQA:GitHub:os-autoinst:openQA:PR-7968",
        ),
        (
            "os-autoinst PR canonical project path",
            "devel:openQA:GitHub",
            "os-autoinst",
            "os-autoinst",
            123,
            "devel:openQA:GitHub:os-autoinst:os-autoinst:PR-123",
        ),
    ],
)
def test_generate_obs_project_name(
    desc: str, target_prj: str, owner: str, repo: str, pr_number: int, expected: str
) -> None:
    assert sut.generate_obs_project_name(target_prj, owner, repo, pr_number) == expected, desc


def test_detect_github_repo_from_origin() -> None:
    runner = MagicMock(return_value=make_completed_process(stdout="git@github.com:os-autoinst/openQA.git\n"))
    owner, repo = sut.detect_github_repo(runner=runner)
    assert (owner, repo) == ("os-autoinst", "openQA")


def test_detect_github_repo_fallback_gh() -> None:
    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "remote":
            return make_completed_process(returncode=1)
        if cmd[0] == "gh":
            return make_completed_process(stdout="os-autoinst/openQA\n")
        return make_completed_process(returncode=1)

    owner, repo = sut.detect_github_repo(runner=fake_runner)
    assert (owner, repo) == ("os-autoinst", "openQA")


def test_detect_github_repo_failure() -> None:
    runner = MagicMock(return_value=make_completed_process(returncode=1))
    with pytest.raises(ValueError, match="Could not detect GitHub repository"):
        sut.detect_github_repo(runner=runner)


def test_detect_pr_number_success() -> None:
    runner = MagicMock(return_value=make_completed_process(stdout="7968\n"))
    assert sut.detect_pr_number(runner=runner) == 7968


def test_detect_pr_number_failure() -> None:
    runner = MagicMock(return_value=make_completed_process(returncode=1, stderr="no pr"))
    with pytest.raises(ValueError, match="Could not detect active PR number"):
        sut.detect_pr_number(runner=runner)


def test_resolve_context_cli_overrides(tmp_path: Path) -> None:
    workflow_file = tmp_path / "workflows.yml"
    workflow_file.write_text(SAMPLE_OPENQA_WORKFLOW, encoding="utf-8")

    parser = sut.create_parser()
    args = parser.parse_args([
        "--repo",
        "custom-owner/custom-repo",
        "--pr",
        "PR-42",
        "--workflow",
        str(workflow_file),
    ])

    context = sut.resolve_context(args)
    assert context.owner == "custom-owner"
    assert context.repo == "custom-repo"
    assert context.pr_number == 42
    assert context.obs_project == "devel:openQA:GitHub:custom-owner:custom-repo:PR-42"
    assert context.workflow.source_package == "openQA"


def test_resolve_context_explicit_project_override() -> None:
    parser = sut.create_parser()
    args = parser.parse_args([
        "--repo",
        "owner/repo",
        "--pr",
        "1",
        "--project",
        "home:someone:my-override",
        "--target-project",
        "devel:openQA:GitHub",
    ])
    context = sut.resolve_context(args)
    assert context.obs_project == "home:someone:my-override"


def test_verify_prerequisites_all_passing() -> None:
    runner = MagicMock(return_value=make_completed_process(returncode=0))
    with patch("shutil.which", return_value="/usr/bin/tool"):
        sut.verify_prerequisites(runner=runner)


@pytest.mark.parametrize(
    ("_desc", "which_side_effect", "cmd_failure", "expected_error"),
    [
        (
            "missing osc executable raises RuntimeError",
            lambda cmd: None if cmd == "osc" else "/usr/bin/gh",
            None,
            "Required tool 'osc' is not installed",
        ),
        (
            "osc whois authentication failure raises RuntimeError",
            lambda cmd: "/usr/bin/" + cmd,
            "osc",
            "OBS authentication failed",
        ),
        (
            "gh auth status failure raises RuntimeError",
            lambda cmd: "/usr/bin/" + cmd,
            "gh",
            "GitHub authentication failed",
        ),
    ],
)
def test_verify_prerequisites_failures(
    _desc: str, which_side_effect: object, cmd_failure: str | None, expected_error: str
) -> None:
    def fake_runner(_cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd_failure and _cmd[0] == cmd_failure:
            return make_completed_process(returncode=1, stderr="Authentication error")
        return make_completed_process(returncode=0)

    with (
        patch("shutil.which", side_effect=which_side_effect),
        pytest.raises(RuntimeError, match=expected_error),
    ):
        sut.verify_prerequisites(runner=fake_runner)


def test_check_project_meta_found() -> None:
    xml_data = '<project name="test"><repository name="repo1"/><repository name="repo2"/></project>'
    runner = MagicMock(return_value=make_completed_process(stdout=xml_data, returncode=0))
    exists, repos = sut.check_project_meta("test", runner=runner)
    assert exists is True
    assert repos == ["repo1", "repo2"]


def test_check_project_meta_not_found() -> None:
    runner = MagicMock(
        return_value=make_completed_process(
            stderr="Server returned an error: HTTP Error 404: Not Found",
            returncode=1,
        )
    )
    exists, repos = sut.check_project_meta("nonexistent", runner=runner)
    assert exists is False
    assert repos == []


@pytest.mark.parametrize(
    ("desc", "report", "expected_status", "expected_action_keyword"),
    [
        (
            "nonexistent project classified as NOT_FOUND",
            sut.DiagnosticReport(project_exists=False),
            sut.HealthStatus.NOT_FOUND,
            "--recreate",
        ),
        (
            "project with zero repositories classified as BROKEN_EMPTY_PROJECT",
            sut.DiagnosticReport(project_exists=True, repositories=[], packages=[]),
            sut.HealthStatus.BROKEN_EMPTY_PROJECT,
            "--configure-repos",
        ),
        (
            "project with repositories but no package files classified as BROKEN_UNBRANCHED_PACKAGE",
            sut.DiagnosticReport(
                project_exists=True,
                repositories=["openSUSE_Tumbleweed"],
                packages=["openQA"],
                package_files=[],
            ),
            sut.HealthStatus.BROKEN_UNBRANCHED_PACKAGE,
            "--repair-branch",
        ),
        (
            "project with repositories and only branch request classified as BROKEN_UNBRANCHED_PACKAGE",
            sut.DiagnosticReport(
                project_exists=True,
                repositories=["openSUSE_Tumbleweed"],
                packages=["openQA"],
                package_files=["_branch_request"],
            ),
            sut.HealthStatus.BROKEN_UNBRANCHED_PACKAGE,
            "--repair-branch",
        ),
        (
            "project with only branch request and all excluded results classified as BROKEN_EXCLUDED_OR_DISABLED",
            sut.DiagnosticReport(
                project_exists=True,
                repositories=["openSUSE_Tumbleweed"],
                packages=["openQA"],
                package_files=["_branch_request"],
                all_results_excluded_or_disabled=True,
            ),
            sut.HealthStatus.BROKEN_EXCLUDED_OR_DISABLED,
            "--repair-branch",
        ),
        (
            "project with source files and all excluded results classified as BROKEN_EXCLUDED_OR_DISABLED",
            sut.DiagnosticReport(
                project_exists=True,
                repositories=["openSUSE_Tumbleweed"],
                packages=["openQA"],
                package_files=["_service", "_branch_request"],
                all_results_excluded_or_disabled=True,
            ),
            sut.HealthStatus.BROKEN_EXCLUDED_OR_DISABLED,
            "--recreate",
        ),
        (
            "project with repositories and files classified as HEALTHY",
            sut.DiagnosticReport(
                project_exists=True,
                repositories=["openSUSE_Tumbleweed"],
                packages=["openQA"],
                package_files=["_service", "_branch_request"],
            ),
            sut.HealthStatus.HEALTHY,
            "--rebuild",
        ),
    ],
)
def test_classify_health(
    desc: str,
    report: sut.DiagnosticReport,
    expected_status: sut.HealthStatus,
    expected_action_keyword: str,
) -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=1,
        workflow=sut.WorkflowConfig(
            target_project="devel:openQA:GitHub",
            source_project="devel:openQA",
            source_package="openQA",
        ),
        obs_project="devel:openQA:GitHub:os-autoinst:openQA:PR-1",
    )
    sut.classify_health(report, ctx)
    assert report.status == expected_status, desc
    assert expected_action_keyword in report.suggested_action, desc


def test_diagnose_flow() -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=7968,
        workflow=sut.WorkflowConfig(
            target_project="devel:openQA:GitHub",
            source_project="devel:openQA",
            source_package="openQA",
        ),
        obs_project="devel:openQA:GitHub:os-autoinst:openQA:PR-7968",
    )

    xml_data = '<project name="test"><repository name="TW"/></project>'

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["meta", "prj"]:
            return make_completed_process(stdout=xml_data)
        if cmd[1] == "ls" and len(cmd) == 3:
            return make_completed_process(stdout="openQA\nopenQA:test\n")
        if cmd[1] == "ls" and len(cmd) == 4:
            return make_completed_process(stdout="_service\n_branch_request\n# comment\n")
        if cmd[1] == "results":
            return make_completed_process(stdout="openQA: succeeded\n")
        return make_completed_process()

    report = sut.diagnose(ctx, runner=fake_runner)
    assert report.project_exists is True
    assert report.repositories == ["TW"]
    assert report.packages == ["openQA", "openQA:test"]
    assert report.package_files == ["_service", "_branch_request"]
    assert report.status == sut.HealthStatus.HEALTHY
    formatted = sut.format_report(report, ctx)
    assert "HEALTHY" in formatted
    assert "devel:openQA:GitHub:os-autoinst:openQA:PR-7968" in formatted


@pytest.mark.parametrize(
    ("desc", "xml_content", "returncode", "expected"),
    [
        (
            "all packages excluded returns True",
            (
                '<resultlist><result project="p" repository="r" arch="a">'
                '<status package="pkg" code="excluded"/></result></resultlist>'
            ),
            0,
            True,
        ),
        (
            "all packages disabled returns True",
            (
                '<resultlist><result project="p" repository="r" arch="a">'
                '<status package="pkg" code="disabled"/></result></resultlist>'
            ),
            0,
            True,
        ),
        (
            "mixed excluded and disabled returns True",
            (
                '<resultlist><result project="p" repository="r1" arch="a">'
                '<status package="pkg" code="excluded"/></result>'
                '<result project="p" repository="r2" arch="a">'
                '<status package="pkg" code="disabled"/></result></resultlist>'
            ),
            0,
            True,
        ),
        (
            "repository-level code disabled without status elements returns True",
            '<resultlist><result project="p" repository="r" arch="a" code="disabled"/></resultlist>',
            0,
            True,
        ),
        (
            "repository-level state disabled without status elements returns True",
            '<resultlist><result project="p" repository="r" arch="a" state="disabled"/></resultlist>',
            0,
            True,
        ),
        (
            "at least one active package build returns False",
            (
                '<resultlist><result project="p" repository="r" arch="a">'
                '<status package="pkg" code="succeeded"/>'
                '<status package="pkg:test" code="excluded"/></result></resultlist>'
            ),
            0,
            False,
        ),
        (
            "failed build returns False",
            (
                '<resultlist><result project="p" repository="r" arch="a">'
                '<status package="pkg" code="failed"/></result></resultlist>'
            ),
            0,
            False,
        ),
        (
            "building status returns False",
            (
                '<resultlist><result project="p" repository="r" arch="a">'
                '<status package="pkg" code="building"/></result></resultlist>'
            ),
            0,
            False,
        ),
        (
            "empty resultlist returns False",
            "<resultlist></resultlist>",
            0,
            False,
        ),
        (
            "invalid XML returns False",
            "<not-xml",
            0,
            False,
        ),
        (
            "command failure returns False",
            "",
            1,
            False,
        ),
    ],
)
def test_check_all_results_excluded_or_disabled(
    desc: str, xml_content: str, returncode: int, *, expected: bool
) -> None:
    runner = MagicMock(return_value=make_completed_process(stdout=xml_content, returncode=returncode))
    assert sut.check_all_results_excluded_or_disabled("test-proj", runner=runner) is expected, desc


def test_diagnose_flow_excluded_or_disabled() -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=7963,
        workflow=sut.WorkflowConfig(
            target_project="devel:openQA:GitHub",
            source_project="devel:openQA",
            source_package="openQA",
        ),
        obs_project="devel:openQA:GitHub:os-autoinst:openQA:PR-7963",
    )

    prj_meta_xml = '<project name="test"><repository name="TW"/><repository name="16.0"/></project>'
    results_xml = (
        '<resultlist><result project="test" repository="TW" arch="x86_64">'
        '<status package="openQA" code="excluded"/></result></resultlist>'
    )
    results_text = "openQA\n x  TW x86_64 (published)\n"

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["meta", "prj"]:
            return make_completed_process(stdout=prj_meta_xml)
        if cmd[1] == "ls" and len(cmd) == 3:
            return make_completed_process(stdout="openQA\n")
        if cmd[1] == "ls" and len(cmd) == 4:
            return make_completed_process(stdout="_branch_request\n")
        if cmd[1] == "results" and "--xml" in cmd:
            return make_completed_process(stdout=results_xml)
        if cmd[1] == "results" and "--show-excluded" in cmd:
            return make_completed_process(stdout=results_text)
        return make_completed_process()

    report = sut.diagnose(ctx, runner=fake_runner)
    assert report.project_exists is True
    assert report.repositories == ["TW", "16.0"]
    assert report.packages == ["openQA"]
    assert report.package_files == ["_branch_request"]
    assert report.all_results_excluded_or_disabled is True
    assert report.status == sut.HealthStatus.BROKEN_EXCLUDED_OR_DISABLED
    assert "--repair-branch" in report.suggested_action
    formatted = sut.format_report(report, ctx)
    assert "BROKEN_EXCLUDED_OR_DISABLED" in formatted
    assert "no source files" in formatted
    assert "x  TW x86_64" in formatted


FULL_PRJ_META = (
    '<project name="p">'
    '<repository name="openSUSE_Tumbleweed"/>'
    '<repository name="SLE_15_SP7_Backports"/>'
    '<repository name="16.0"/>'
    "</project>"
)


def test_configure_repositories_adds_missing_build_targets() -> None:
    ctx = openqa_context()
    state = {
        "meta": EMPTY_PRJ_META,
        "files": "_service\n",
        "results_xml": HEALTHY_RESULTS_XML,
    }
    calls: list[list[str]] = []
    runner = make_state_runner(state, calls)

    assert sut.configure_repositories(ctx, runner=runner) is True

    api_calls = [cmd for cmd in calls if cmd[1] == "api"]
    assert len(api_calls) == 1
    assert api_calls[0][:4] == ["osc", "api", "-X", "PUT"]
    assert api_calls[0][-1] == "/source/devel:openQA:GitHub:os-autoinst:openQA:PR-7985/_meta"
    assert '<repository name="openSUSE_Tumbleweed">' in state["meta"]
    assert '<path project="openSUSE:Factory"' in state["meta"]
    assert 'repository="snapshot"' in state["meta"]
    assert '<repository name="16.0">' in state["meta"]
    assert state["meta"].count("<arch>x86_64</arch>") == 3


def test_configure_repositories_skips_existing_repositories() -> None:
    ctx = openqa_context()
    calls: list[list[str]] = []

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return make_completed_process(stdout=FULL_PRJ_META)

    assert sut.configure_repositories(ctx, runner=fake_runner) is True
    assert all(cmd[1] != "api" for cmd in calls)


def test_configure_repositories_missing_project() -> None:
    ctx = openqa_context()
    runner = MagicMock(return_value=make_completed_process(returncode=1, stderr="HTTP Error 404: Not Found"))
    assert sut.configure_repositories(ctx, runner=runner) is False


def test_configure_repositories_without_workflow_repositories() -> None:
    ctx = sut.ProjectContext(
        owner="o",
        repo="r",
        pr_number=1,
        workflow=sut.WorkflowConfig("tgt", "src", "pkg"),
        obs_project="proj",
    )
    runner = MagicMock()
    assert sut.configure_repositories(ctx, runner=runner) is False
    runner.assert_not_called()


def test_configure_repositories_put_failure() -> None:
    ctx = openqa_context()

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["meta", "prj"]:
            return make_completed_process(stdout=EMPTY_PRJ_META)
        return make_completed_process(returncode=1, stderr="api fail")

    assert sut.configure_repositories(ctx, runner=fake_runner) is False


def test_repair_after_cycle_project_not_recreated() -> None:
    ctx = openqa_context()
    sleeps: list[float] = []

    def fake_runner(_cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return make_completed_process(returncode=1, stderr="HTTP Error 404: Not Found")

    ok = sut.repair_after_cycle(
        ctx,
        runner=fake_runner,
        sleep=sleeps.append,
        timeout=20,
        interval=10,
    )
    assert ok is False
    assert sleeps == [10, 10]


def test_repair_after_cycle_configures_missing_build_targets() -> None:
    ctx = openqa_context()
    state = {
        "meta": EMPTY_PRJ_META,
        "files": "_service\n",
        "results_xml": HEALTHY_RESULTS_XML,
    }
    calls: list[list[str]] = []
    ok = sut.repair_after_cycle(ctx, runner=make_state_runner(state, calls), sleep=lambda _s: None)
    assert ok is True
    assert '<repository name="16.0">' in state["meta"]
    assert not any(cmd[1] == "branch" for cmd in calls)


def test_repair_after_cycle_rebranches_empty_package() -> None:
    ctx = openqa_context()
    state = {"meta": FULL_PRJ_META, "files": "", "results_xml": HEALTHY_RESULTS_XML}
    calls: list[list[str]] = []
    ok = sut.repair_after_cycle(ctx, runner=make_state_runner(state, calls), sleep=lambda _s: None)
    assert ok is True
    branches = [cmd for cmd in calls if cmd[1] == "branch"]
    assert branches == [["osc", "branch", "-f", "devel:openQA", "openQA", ctx.obs_project, "openQA"]]
    assert any(cmd[-1].endswith("/openQA/_branch_request") for cmd in calls)


def test_repair_after_cycle_branch_failure() -> None:
    ctx = openqa_context()
    state = {
        "meta": FULL_PRJ_META,
        "files": "",
        "results_xml": HEALTHY_RESULTS_XML,
        "branch_code": 1,
    }
    calls: list[list[str]] = []
    assert sut.repair_after_cycle(ctx, runner=make_state_runner(state, calls), sleep=lambda _s: None) is False


def test_recreate_project_abort_on_prompt() -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=1,
        workflow=sut.WorkflowConfig("", "", ""),
        obs_project="proj",
    )
    runner = MagicMock()
    with patch("builtins.input", return_value="n"):
        success = sut.recreate_project(ctx, auto_confirm=False, runner=runner)
    assert success is False
    runner.assert_not_called()


def test_recreate_project_auto_confirm() -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=123,
        workflow=sut.WorkflowConfig("", "", ""),
        obs_project="devel:openQA:GitHub:os-autoinst:openQA:PR-123",
    )
    calls: list[list[str]] = []

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return make_completed_process(returncode=0)

    with patch.object(sut, "repair_after_cycle", return_value=True) as mock_repair:
        success = sut.recreate_project(ctx, auto_confirm=True, runner=fake_runner)
    assert success is True
    assert mock_repair.called
    assert calls[0][:3] == ["osc", "rdelete", "-r"]
    assert calls[0][-1] == ctx.obs_project
    assert calls[1] == ["gh", "pr", "close", "123", "--repo", "os-autoinst/openQA"]
    assert calls[2] == ["gh", "pr", "reopen", "123", "--repo", "os-autoinst/openQA"]


def test_repair_branch() -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=7968,
        workflow=sut.WorkflowConfig(
            target_project="devel:openQA:GitHub",
            source_project="devel:openQA",
            source_package="openQA",
        ),
        obs_project="devel:openQA:GitHub:os-autoinst:openQA:PR-7968",
    )
    calls: list[list[str]] = []

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if cmd[0] == "gh" and cmd[1:3] == ["pr", "view"]:
            payload = {
                "headRefOid": "abc12345def",
                "headRepository": {"nameWithOwner": "okurz/openQA"},
            }
            return make_completed_process(stdout=json.dumps(payload))
        return make_completed_process(returncode=0)

    success = sut.repair_branch(ctx, runner=fake_runner)
    assert success is True
    assert calls[0] == [
        "osc",
        "branch",
        "-f",
        "devel:openQA",
        "openQA",
        "devel:openQA:GitHub:os-autoinst:openQA:PR-7968",
        "openQA",
    ]
    assert calls[1][:3] == ["gh", "pr", "view"]
    assert calls[2][:4] == ["osc", "api", "-X", "PUT"]
    assert calls[2][-1] == "/source/devel:openQA:GitHub:os-autoinst:openQA:PR-7968/openQA/_branch_request"


def test_rebuild_project() -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=1,
        workflow=sut.WorkflowConfig("", "", ""),
        obs_project="target:project",
    )
    runner = MagicMock(return_value=make_completed_process(returncode=0))
    success = sut.rebuild_project(ctx, runner=runner)
    assert success is True
    runner.assert_called_once_with(["osc", "rebuild", "--all", "target:project"])


def test_main_multiple_actions_error() -> None:
    exit_code = sut.main(["--recreate", "--rebuild", "--skip-auth-check"])
    assert exit_code == 1
    assert sut.main(["--configure-repos", "--rebuild", "--skip-auth-check"]) == 1


def test_main_exception_returns_one() -> None:
    with patch.object(sut, "resolve_context", side_effect=RuntimeError("boom")):
        assert sut.main(["--repo", "os-autoinst/openQA", "--pr", "123", "--skip-auth-check"]) == 1


def test_default_runner_handles_missing_command() -> None:
    res = sut.default_runner(["nonexistent_cmd_12345"])
    assert res.returncode == 127
    assert "nonexistent_cmd_12345" in res.stderr


def test_find_and_parse_workflow_file(tmp_path: Path) -> None:
    obs_dir = tmp_path / ".obs"
    obs_dir.mkdir()
    wf = obs_dir / "workflows.yml"
    wf.write_text(SAMPLE_OPENQA_WORKFLOW, encoding="utf-8")

    sub_dir = tmp_path / "sub" / "dir"
    sub_dir.mkdir(parents=True)

    found = sut.find_workflow_file(sub_dir)
    assert found == wf

    parsed = sut.parse_workflow_file(wf)
    assert parsed.source_package == "openQA"

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert sut.find_workflow_file(empty_dir) == wf


@pytest.mark.parametrize(
    ("_desc", "args_list", "expected_err"),
    [
        (
            "invalid repository format raises ValueError",
            ["--repo", "invalid_repo", "--pr", "1"],
            "Invalid repo format",
        ),
        (
            "invalid PR number raises ValueError",
            ["--repo", "owner/repo", "--pr", "not_a_number"],
            "Invalid PR number",
        ),
        (
            "missing target project without workflow raises ValueError",
            ["--repo", "owner/repo", "--pr", "1", "--target-project", ""],
            "Target project could not be resolved",
        ),
    ],
)
def test_resolve_context_validation_errors(_desc: str, args_list: list[str], expected_err: str) -> None:
    parser = sut.create_parser()
    args = parser.parse_args(args_list)
    with pytest.raises(ValueError, match=expected_err):
        sut.resolve_context(args)


def test_check_project_meta_parse_error() -> None:
    runner = MagicMock(return_value=make_completed_process(stdout="not xml", returncode=0))
    exists, repos = sut.check_project_meta("test", runner=runner)
    assert exists is True
    assert repos == []


def test_list_packages_and_files_failure() -> None:
    runner = MagicMock(return_value=make_completed_process(returncode=1))
    assert sut.list_packages("test", runner=runner) == []
    assert sut.list_package_files("test", "pkg", runner=runner) == []


@pytest.mark.parametrize(
    ("desc", "close_code", "reopen_code", "expected_success"),
    [
        ("close failure returns False", 1, 0, False),
        ("reopen failure returns False", 0, 1, False),
    ],
)
def test_recreate_project_gh_failures(desc: str, close_code: int, reopen_code: int, *, expected_success: bool) -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=1,
        workflow=sut.WorkflowConfig("", "", ""),
        obs_project="proj",
    )

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if len(cmd) > 2 and cmd[2] == "close":
            return make_completed_process(returncode=close_code, stderr="err")
        if len(cmd) > 2 and cmd[2] == "reopen":
            return make_completed_process(returncode=reopen_code, stderr="err")
        return make_completed_process(returncode=0)

    success = sut.recreate_project(ctx, auto_confirm=True, runner=fake_runner)
    assert success is expected_success, desc


def test_fetch_pr_head_commit_failure() -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=1,
        workflow=sut.WorkflowConfig("", "", ""),
        obs_project="proj",
    )
    runner = MagicMock(return_value=make_completed_process(returncode=1, stderr="gh error"))
    with pytest.raises(RuntimeError, match="Failed to inspect PR"):
        sut.fetch_pr_head_commit(ctx, runner=runner)


@pytest.mark.parametrize(
    ("desc", "branch_code", "api_code", "expected_success"),
    [
        ("osc branch failure returns False", 1, 0, False),
        ("osc api failure returns False", 0, 1, False),
    ],
)
def test_repair_branch_failures(desc: str, branch_code: int, api_code: int, *, expected_success: bool) -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=1,
        workflow=sut.WorkflowConfig("tgt", "src", "pkg"),
        obs_project="tgt:proj",
    )

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "branch":
            return make_completed_process(returncode=branch_code, stderr="branch fail")
        if cmd[0] == "gh":
            payload = {
                "headRefOid": "abc",
                "headRepository": {"nameWithOwner": "owner/repo"},
            }
            return make_completed_process(stdout=json.dumps(payload))
        if cmd[1] == "api":
            return make_completed_process(returncode=api_code, stderr="api fail")
        return make_completed_process(returncode=0)

    success = sut.repair_branch(ctx, runner=fake_runner)
    assert success is expected_success, desc


def test_rebuild_project_failure() -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=1,
        workflow=sut.WorkflowConfig("", "", ""),
        obs_project="proj",
    )
    runner = MagicMock(return_value=make_completed_process(returncode=1, stderr="rebuild fail"))
    assert sut.rebuild_project(ctx, runner=runner) is False


def test_main_dispatch_flows() -> None:
    base_args = [
        "--repo",
        "os-autoinst/openQA",
        "--pr",
        "7968",
        "--target-project",
        "devel:openQA:GitHub",
        "--skip-auth-check",
    ]

    with patch.object(sut, "recreate_project", return_value=True) as mock_rec:
        assert sut.main([*base_args, "--recreate", "-y"]) == 0
        assert mock_rec.called

    with patch.object(sut, "repair_branch", return_value=True) as mock_rep:
        assert sut.main([*base_args, "--repair-branch"]) == 0
        assert mock_rep.called

    with patch.object(sut, "configure_repositories", return_value=True) as mock_cfg:
        assert sut.main([*base_args, "--configure-repos"]) == 0
        assert mock_cfg.called

    with patch.object(sut, "rebuild_project", return_value=True) as mock_reb:
        assert sut.main([*base_args, "--rebuild"]) == 0
        assert mock_reb.called

    healthy_report = sut.DiagnosticReport(
        project_exists=True,
        repositories=["TW"],
        packages=["openQA"],
        package_files=["_service"],
        status=sut.HealthStatus.HEALTHY,
    )
    with patch.object(sut, "diagnose", return_value=healthy_report):
        assert sut.main(base_args) == 0

    unhealthy_report = sut.DiagnosticReport(
        project_exists=False,
        status=sut.HealthStatus.NOT_FOUND,
    )
    with patch.object(sut, "diagnose", return_value=unhealthy_report):
        assert sut.main(base_args) == 2


@pytest.mark.parametrize(
    ("desc", "pkg", "returncode", "stdout", "expected"),
    [
        (
            "successful fetch returns parsed dictionary",
            "openQA",
            0,
            '{"action": "opened", "pull_request": {"head": {"sha": "abc1234"}}}',
            {"action": "opened", "pull_request": {"head": {"sha": "abc1234"}}},
        ),
        (
            "empty source package returns None",
            "",
            0,
            '{"action": "opened"}',
            None,
        ),
        (
            "command failure returns None",
            "openQA",
            1,
            "",
            None,
        ),
        (
            "invalid json returns None",
            "openQA",
            0,
            "not-json",
            None,
        ),
        (
            "non-dict json returns None",
            "openQA",
            0,
            '["not", "a", "dict"]',
            None,
        ),
    ],
)
def test_diagnostics_fetch_branch_request(
    desc: str, pkg: str, returncode: int, stdout: str, expected: dict[str, object] | None
) -> None:
    ctx = sut.ProjectContext(
        owner="os-autoinst",
        repo="openQA",
        pr_number=1,
        workflow=sut.WorkflowConfig(target_project="tgt", source_project="src", source_package=pkg),
        obs_project="tgt:proj",
    )
    runner = MagicMock(return_value=make_completed_process(returncode=returncode, stdout=stdout))
    assert sut.fetch_branch_request(ctx, runner=runner) == expected, desc


@pytest.mark.parametrize(
    ("desc", "data", "expected_sha"),
    [
        (
            "nested pull_request head sha",
            {"pull_request": {"head": {"sha": "sha123"}}},
            "sha123",
        ),
        (
            "direct head sha",
            {"head": {"sha": "sha456"}},
            "sha456",
        ),
        (
            "top level sha",
            {"sha": "sha789"},
            "sha789",
        ),
        (
            "empty dict returns empty string",
            {},
            "",
        ),
        (
            "none input returns empty string",
            None,
            "",
        ),
    ],
)
def test_diagnostics_extract_branch_request_sha(desc: str, data: dict[str, object] | None, expected_sha: str) -> None:
    assert sut.extract_branch_request_sha(data) == expected_sha, desc


@pytest.mark.parametrize(
    ("desc", "returncode", "stdout", "expected_len"),
    [
        (
            "valid checks list returned",
            0,
            '[{"name": "test", "bucket": "pass"}]',
            1,
        ),
        (
            "empty stdout returns empty list",
            0,
            "",
            0,
        ),
        (
            "invalid json returns empty list",
            0,
            "not json",
            0,
        ),
        (
            "non-list json returns empty list",
            0,
            '{"name": "single"}',
            0,
        ),
    ],
)
def test_diagnostics_check_commit_status_checks(desc: str, returncode: int, stdout: str, expected_len: int) -> None:
    ctx = openqa_context()
    runner = MagicMock(return_value=make_completed_process(returncode=returncode, stdout=stdout))
    res = sut.check_commit_status_checks(ctx, runner=runner)
    assert len(res) == expected_len, desc


@pytest.mark.parametrize(
    ("desc", "check", "expected"),
    [
        (
            "link with build.opensuse.org is obs check",
            {"link": "https://build.opensuse.org/package/show/foo", "name": "build"},
            True,
        ),
        (
            "name starting with OBS is obs check",
            {"link": "", "name": "OBS: openQA - 16.0"},
            True,
        ),
        (
            "name with continuous-integration/obs-helper is obs check",
            {"link": "", "name": "continuous-integration/obs-helper"},
            True,
        ),
        (
            "regular check is not obs check",
            {"link": "https://circleci.com/gh/foo", "name": "ci/circleci: fullstack"},
            False,
        ),
    ],
)
def test_diagnostics_is_obs_status_check(desc: str, check: dict[str, str], *, expected: bool) -> None:
    assert sut.is_obs_status_check(check) is expected, desc


@pytest.mark.parametrize(
    ("desc", "updated_at", "grace_seconds", "expected"),
    [
        ("empty timestamp returns False", "", 900, False),
        ("invalid timestamp returns False", "not-a-date", 900, False),
        (
            "recent update within grace period returns False",
            datetime.now(UTC).isoformat(),
            900,
            False,
        ),
        (
            "old update past grace period returns True",
            "2020-01-01T00:00:00Z",
            900,
            True,
        ),
    ],
)
def test_diagnostics_is_pr_grace_period_expired(
    desc: str, updated_at: str, grace_seconds: int, *, expected: bool
) -> None:
    assert sut.is_pr_grace_period_expired(updated_at, grace_seconds=grace_seconds) is expected, desc


def test_diagnostics_stale_sha_classification() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(
        project_exists=True,
        repositories=["openSUSE_Tumbleweed", "SLE_15_SP7_Backports", "16.0"],
        packages=["openQA"],
        package_files=["_service", "_branch_request"],
        pr_head_sha="77bfa16e90b10c31a33f54792ca7ceea26578f71",
        obs_branch_sha="611ee923331a69146536b7a4a16f9b54ed302990",
    )
    sut.classify_health(report, ctx)
    assert report.status == sut.HealthStatus.BROKEN_STALE_SHA
    assert "--repair-branch" in report.suggested_action


def test_diagnostics_missing_repositories_classification() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(
        project_exists=True,
        repositories=["openSUSE_Tumbleweed"],
        missing_repositories=["SLE_15_SP7_Backports", "16.0"],
        packages=["openQA"],
        package_files=["_service", "_branch_request"],
    )
    sut.classify_health(report, ctx)
    assert report.status == sut.HealthStatus.MISSING_REPOSITORIES
    assert "--configure-repos" in report.suggested_action


def test_diagnostics_stale_sha_and_missing_repos_combined() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(
        project_exists=True,
        repositories=["openSUSE_Tumbleweed"],
        missing_repositories=["16.1"],
        packages=["openQA"],
        package_files=["_service", "_branch_request"],
        pr_head_sha="77bfa16e",
        obs_branch_sha="611ee923",
    )
    sut.classify_health(report, ctx)
    assert report.status == sut.HealthStatus.BROKEN_STALE_SHA
    assert "--repair-branch" in report.suggested_action
    assert "--configure-repos" in report.suggested_action


def test_diagnostics_missing_checks_classification() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(
        project_exists=True,
        repositories=["openSUSE_Tumbleweed", "SLE_15_SP7_Backports", "16.0"],
        packages=["openQA"],
        package_files=["_service", "_branch_request"],
        pr_head_sha="77bfa16e",
        obs_branch_sha="77bfa16e",
        obs_checks_count=0,
        pr_updated_at="2020-01-01T00:00:00Z",
    )
    sut.classify_health(report, ctx)
    assert report.status == sut.HealthStatus.MISSING_CHECKS
    assert "--repair-branch or --rebuild" in report.suggested_action


def test_diagnostics_format_report_displays_all_fields() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(
        project_exists=True,
        repositories=["openSUSE_Tumbleweed"],
        missing_repositories=["16.1"],
        packages=["openQA"],
        package_files=["_service", "_branch_request"],
        pr_head_sha="77bfa16e",
        obs_branch_sha="611ee923",
        obs_checks_count=2,
        total_checks_count=5,
        status=sut.HealthStatus.BROKEN_STALE_SHA,
        suggested_action="Run with --repair-branch.",
    )
    formatted = sut.format_report(report, ctx)
    assert "PR Head SHA:           77bfa16e" in formatted
    assert "OBS Branch SHA:        611ee923 (STALE)" in formatted
    assert "Missing Repositories:  16.1" in formatted
    assert "Status Checks:         2 OBS checks (out of 5 total)" in formatted


@pytest.mark.parametrize(
    ("desc", "returncode", "stdout", "expected_found"),
    [
        (
            "successful fetch decodes base64 content",
            0,
            base64.b64encode(SAMPLE_OPENQA_WORKFLOW.encode("utf-8")).decode("ascii"),
            True,
        ),
        (
            "command failure returns None",
            1,
            "",
            False,
        ),
        (
            "invalid base64 content returns None",
            0,
            "not-base64???",
            False,
        ),
    ],
)
def test_diagnostics_fetch_repo_workflow(desc: str, returncode: int, stdout: str, *, expected_found: bool) -> None:
    runner = MagicMock(return_value=make_completed_process(returncode=returncode, stdout=stdout))
    wf = sut.fetch_repo_workflow("os-autoinst", "openQA", runner=runner)
    assert (wf is not None) is expected_found, desc


@pytest.mark.parametrize(
    ("desc", "status", "dry_run", "expected_actions", "expected_success"),
    [
        (
            "not found dry run returns recreate_project",
            sut.HealthStatus.NOT_FOUND,
            True,
            ["recreate_project"],
            True,
        ),
        (
            "empty project dry run returns configure_repositories and repair_branch",
            sut.HealthStatus.BROKEN_EMPTY_PROJECT,
            True,
            ["configure_repositories", "repair_branch"],
            True,
        ),
        (
            "excluded or disabled dry run returns repair_branch",
            sut.HealthStatus.BROKEN_EXCLUDED_OR_DISABLED,
            True,
            ["repair_branch"],
            True,
        ),
        (
            "stale sha with missing repos dry run returns both actions",
            sut.HealthStatus.BROKEN_STALE_SHA,
            True,
            ["configure_repositories", "repair_branch"],
            True,
        ),
        (
            "missing repositories dry run returns configure_repositories",
            sut.HealthStatus.MISSING_REPOSITORIES,
            True,
            ["configure_repositories"],
            True,
        ),
        (
            "missing checks dry run returns rebuild_project",
            sut.HealthStatus.MISSING_CHECKS,
            True,
            ["rebuild_project"],
            True,
        ),
        (
            "healthy status returns empty actions",
            sut.HealthStatus.HEALTHY,
            False,
            [],
            True,
        ),
    ],
)
def test_auto_repair_dry_run_and_healthy(
    desc: str,
    status: sut.HealthStatus,
    *,
    dry_run: bool,
    expected_actions: list[str],
    expected_success: bool,
) -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(
        project_exists=True,
        status=status,
        pr_head_sha="abc12345",
        missing_repositories=["16.1"] if status == sut.HealthStatus.BROKEN_STALE_SHA else [],
    )
    runner = MagicMock(return_value=make_completed_process())
    success, actions = sut.auto_repair(ctx, report, runner=runner, dry_run=dry_run)
    assert success is expected_success, desc
    assert actions == expected_actions, desc
    assert not runner.called, desc


def test_auto_repair_not_found_live_execution() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(project_exists=False, status=sut.HealthStatus.NOT_FOUND)
    with patch.object(sut, "recreate_project", return_value=True) as mock_recreate:
        success, actions = sut.auto_repair(ctx, report, dry_run=False)
        assert success is True
        assert actions == ["recreate_project"]
        assert mock_recreate.called


def test_auto_repair_empty_project_live_execution() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(project_exists=True, status=sut.HealthStatus.BROKEN_EMPTY_PROJECT)
    with (
        patch.object(sut, "configure_repositories", return_value=True) as mock_cfg,
        patch.object(sut, "repair_branch", return_value=True) as mock_rep,
    ):
        success, actions = sut.auto_repair(ctx, report, dry_run=False)
        assert success is True
        assert actions == ["configure_repositories", "repair_branch"]
        assert mock_cfg.called
        assert mock_rep.called


def test_auto_repair_excluded_or_disabled_fallback_flow() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(project_exists=True, status=sut.HealthStatus.BROKEN_EXCLUDED_OR_DISABLED)
    with (
        patch.object(sut, "repair_branch", return_value=False),
        patch.object(sut, "recreate_project", return_value=True) as mock_recreate,
    ):
        success, actions = sut.auto_repair(ctx, report, dry_run=False)
        assert success is True
        assert actions == ["recreate_project"]
        assert mock_recreate.called

    with (
        patch.object(sut, "repair_branch", return_value=False),
        patch.object(sut, "recreate_project", return_value=False),
    ):
        success, actions = sut.auto_repair(ctx, report, dry_run=False)
        assert success is False
        assert actions == []


def test_auto_repair_stale_sha_live_execution() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(
        project_exists=True,
        status=sut.HealthStatus.BROKEN_STALE_SHA,
        missing_repositories=["16.1"],
        pr_head_sha="77bfa16e",
    )
    with (
        patch.object(sut, "configure_repositories", return_value=True) as mock_cfg,
        patch.object(sut, "repair_branch", return_value=True) as mock_rep,
    ):
        success, actions = sut.auto_repair(ctx, report, dry_run=False)
        assert success is True
        assert actions == ["configure_repositories", "repair_branch"]
        assert mock_cfg.called
        assert mock_rep.called


def test_auto_repair_missing_checks_live_execution() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(project_exists=True, status=sut.HealthStatus.MISSING_CHECKS)
    with patch.object(sut, "rebuild_project", return_value=True) as mock_reb:
        success, actions = sut.auto_repair(ctx, report, dry_run=False)
        assert success is True
        assert actions == ["rebuild_project"]
        assert mock_reb.called


@pytest.mark.parametrize(
    ("desc", "comments", "sha", "expected_skip"),
    [
        ("empty sha does not skip", [], "", False),
        (
            "matching bot comment within 24 hours skips",
            [
                {
                    "body": f"{sut.BOT_MARKER}\nPR Head SHA: `sha12345`",
                    "createdAt": datetime.now(UTC).isoformat(),
                }
            ],
            "sha12345",
            True,
        ),
        (
            "matching bot comment older than 24 hours does not skip",
            [
                {
                    "body": f"{sut.BOT_MARKER}\nPR Head SHA: `sha12345`",
                    "createdAt": "2020-01-01T00:00:00Z",
                }
            ],
            "sha12345",
            False,
        ),
        (
            "matching bot comments at max count skips regardless of age",
            [
                {"body": f"{sut.BOT_MARKER} sha12345", "createdAt": "2020-01-01T00:00:00Z"},
                {"body": f"{sut.BOT_MARKER} sha12345", "createdAt": "2020-01-01T00:00:00Z"},
                {"body": f"{sut.BOT_MARKER} sha12345", "createdAt": "2020-01-01T00:00:00Z"},
            ],
            "sha12345",
            True,
        ),
        (
            "comment for different sha does not skip",
            [
                {
                    "body": f"{sut.BOT_MARKER}\nPR Head SHA: `other_sha`",
                    "createdAt": datetime.now(UTC).isoformat(),
                }
            ],
            "sha12345",
            False,
        ),
        (
            "non-bot comment does not skip",
            [
                {
                    "body": "Human comment mentioning sha12345",
                    "createdAt": datetime.now(UTC).isoformat(),
                }
            ],
            "sha12345",
            False,
        ),
    ],
)
def test_comment_should_skip_deduplication(
    desc: str, comments: list[dict[str, str]], sha: str, *, expected_skip: bool
) -> None:
    assert sut.should_skip_comment(comments, sha) is expected_skip, desc


def test_comment_fetch_pr_comments_success_and_failures() -> None:
    ctx = openqa_context()
    runner = MagicMock(return_value=make_completed_process(stdout='{"comments": [{"body": "hello"}]}'))
    res = sut.fetch_pr_comments(ctx, runner=runner)
    assert len(res) == 1

    runner_err = MagicMock(return_value=make_completed_process(returncode=1, stderr="error"))
    assert sut.fetch_pr_comments(ctx, runner=runner_err) == []

    runner_bad_json = MagicMock(return_value=make_completed_process(stdout="not-json"))
    assert sut.fetch_pr_comments(ctx, runner=runner_bad_json) == []


def test_comment_post_pr_comment_flow() -> None:
    ctx = openqa_context()
    report = sut.DiagnosticReport(
        project_exists=True,
        status=sut.HealthStatus.BROKEN_STALE_SHA,
        pr_head_sha="77bfa16e",
    )

    assert sut.post_pr_comment(ctx, report, actions_taken=[]) is True

    runner_dry = MagicMock()
    with patch.object(sut, "fetch_pr_comments", return_value=[]):
        assert sut.post_pr_comment(ctx, report, ["repair_branch"], runner=runner_dry, dry_run=True) is True
        assert not runner_dry.called

    existing_bot_comments = [
        {
            "body": f"{sut.BOT_MARKER}\nPR Head SHA: `77bfa16e`",
            "createdAt": datetime.now(UTC).isoformat(),
        }
    ]
    with patch.object(sut, "fetch_pr_comments", return_value=existing_bot_comments):
        runner_dedup = MagicMock()
        assert sut.post_pr_comment(ctx, report, ["repair_branch"], runner=runner_dedup, dry_run=False) is True
        assert not runner_dedup.called

    with patch.object(sut, "fetch_pr_comments", return_value=[]):
        runner_success = MagicMock(return_value=make_completed_process(returncode=0))
        assert sut.post_pr_comment(ctx, report, ["repair_branch"], runner=runner_success, dry_run=False) is True
        assert runner_success.called

    with patch.object(sut, "fetch_pr_comments", return_value=[]):
        runner_failure = MagicMock(return_value=make_completed_process(returncode=1, stderr="gh fail"))
        assert sut.post_pr_comment(ctx, report, ["repair_branch"], runner=runner_failure, dry_run=False) is False


def test_auto_repair_cli_dispatch() -> None:
    base_args = [
        "--repo",
        "os-autoinst/openQA",
        "--pr",
        "7985",
        "--workflow",
        str(Path(__file__).resolve().parent / "fixtures" / "workflows.yml"),
        "--skip-auth-check",
    ]
    report = sut.DiagnosticReport(
        project_exists=True,
        status=sut.HealthStatus.BROKEN_STALE_SHA,
        pr_head_sha="abc12345",
    )
    with (
        patch.object(sut, "diagnose", return_value=report),
        patch.object(sut, "auto_repair", return_value=(True, ["repair_branch"])) as mock_repair,
        patch.object(sut, "post_pr_comment", return_value=True) as mock_comment,
    ):
        exit_code = sut.main([*base_args, "--auto-repair", "--comment", "--dry-run"])
        assert exit_code == 0
        assert mock_repair.called
        assert mock_comment.called

    exit_bad = sut.main([*base_args, "--auto-repair", "--rebuild"])
    assert exit_bad == 1


@pytest.mark.parametrize(
    ("desc", "returncode", "stdout", "org", "expected"),
    [
        (
            "code search returns repositories list",
            0,
            json.dumps({"items": [{"repository": {"full_name": "os-autoinst/openQA"}}]}),
            "os-autoinst",
            ["os-autoinst/openQA"],
        ),
        (
            "duplicate repositories are filtered out",
            0,
            json.dumps({
                "items": [
                    {"repository": {"full_name": "os-autoinst/openQA"}},
                    {"repository": {"full_name": "os-autoinst/openQA"}},
                ]
            }),
            "os-autoinst",
            ["os-autoinst/openQA"],
        ),
        (
            "empty search results for os-autoinst fall back to default repos",
            0,
            json.dumps({"items": []}),
            "os-autoinst",
            ["os-autoinst/openQA", "os-autoinst/os-autoinst", "os-autoinst/os-autoinst-scripts"],
        ),
        (
            "empty search results for other org return empty list",
            0,
            json.dumps({"items": []}),
            "other-org",
            [],
        ),
        (
            "command failure for os-autoinst falls back to default repos",
            1,
            "",
            "os-autoinst",
            ["os-autoinst/openQA", "os-autoinst/os-autoinst", "os-autoinst/os-autoinst-scripts"],
        ),
        (
            "invalid json for os-autoinst falls back to default repos",
            0,
            "not-json",
            "os-autoinst",
            ["os-autoinst/openQA", "os-autoinst/os-autoinst", "os-autoinst/os-autoinst-scripts"],
        ),
    ],
)
def test_discover_org_repositories(desc: str, returncode: int, stdout: str, org: str, expected: list[str]) -> None:
    runner = MagicMock(return_value=make_completed_process(returncode=returncode, stdout=stdout))
    assert sut.discover_org_repositories(org, runner=runner) == expected, desc


@pytest.mark.parametrize(
    ("desc", "returncode", "stdout", "expected"),
    [
        (
            "successful fetch returns list of pr numbers",
            0,
            json.dumps([{"number": 10}, {"number": 20}, {"number": "invalid"}, {}]),
            [10, 20],
        ),
        (
            "command failure returns empty list",
            1,
            "",
            [],
        ),
        (
            "empty stdout returns empty list",
            0,
            "",
            [],
        ),
        (
            "invalid json returns empty list",
            0,
            "invalid-json",
            [],
        ),
        (
            "non-list json returns empty list",
            0,
            json.dumps({"error": "forbidden"}),
            [],
        ),
    ],
)
def test_fetch_open_prs(desc: str, returncode: int, stdout: str, expected: list[int]) -> None:
    runner = MagicMock(return_value=make_completed_process(returncode=returncode, stdout=stdout))
    assert sut.fetch_open_prs("os-autoinst/openQA", runner=runner) == expected, desc


def test_sweep_pr_context_failure() -> None:
    options = argparse.Namespace(auto_repair=False, comment=False, dry_run=False)
    runner = MagicMock(return_value=make_completed_process(returncode=1, stderr="fail"))
    with patch.object(sut, "resolve_context", side_effect=RuntimeError("context err")):
        status = sut.sweep_pr("os-autoinst/openQA", 1, options, runner=runner)
        assert status == sut.HealthStatus.UNKNOWN


def test_sweep_pr_healthy_flow() -> None:
    options = argparse.Namespace(auto_repair=True, comment=True, dry_run=False)
    report = sut.DiagnosticReport(project_exists=True, status=sut.HealthStatus.HEALTHY)
    ctx = openqa_context()
    with (
        patch.object(sut, "resolve_context", return_value=ctx),
        patch.object(sut, "diagnose", return_value=report),
        patch.object(sut, "auto_repair") as mock_repair,
    ):
        status = sut.sweep_pr("os-autoinst/openQA", 7985, options)
        assert status == sut.HealthStatus.HEALTHY
        assert not mock_repair.called


def test_sweep_pr_unhealthy_with_auto_repair_and_comment() -> None:
    options = argparse.Namespace(auto_repair=True, comment=True, dry_run=False)
    report = sut.DiagnosticReport(project_exists=True, status=sut.HealthStatus.BROKEN_STALE_SHA)
    ctx = openqa_context()
    with (
        patch.object(sut, "resolve_context", return_value=ctx),
        patch.object(sut, "diagnose", return_value=report),
        patch.object(sut, "auto_repair", return_value=(True, ["repair_branch"])) as mock_repair,
        patch.object(sut, "post_pr_comment") as mock_comment,
    ):
        status = sut.sweep_pr("os-autoinst/openQA", 7985, options)
        assert status == sut.HealthStatus.BROKEN_STALE_SHA
        assert mock_repair.called
        assert mock_comment.called


def test_sweep_organization_dispatch_modes() -> None:
    with (
        patch.object(sut, "discover_org_repositories", return_value=["os-autoinst/openQA"]),
        patch.object(sut, "fetch_open_prs", return_value=[7985]),
        patch.object(sut, "sweep_pr", return_value=sut.HealthStatus.HEALTHY),
    ):
        res1 = sut.sweep_organization("os-autoinst")
        assert res1 == {"os-autoinst/openQA": [(7985, sut.HealthStatus.HEALTHY)]}

        ns = argparse.Namespace(org="os-autoinst", auto_repair=True, comment=False, dry_run=True)
        res2 = sut.sweep_organization(ns)
        assert res2 == {"os-autoinst/openQA": [(7985, sut.HealthStatus.HEALTHY)]}


def test_run_action_org_sweep_return_codes() -> None:
    ns_unhealthy = argparse.Namespace(org="os-autoinst", auto_repair=False, comment=False, dry_run=False)
    unhealthy_sweep = {"os-autoinst/openQA": [(1, sut.HealthStatus.BROKEN_STALE_SHA)]}
    with patch.object(sut, "sweep_organization", return_value=unhealthy_sweep):
        assert sut.run_action(None, ns_unhealthy) == 1

    ns_repair = argparse.Namespace(org="os-autoinst", auto_repair=True, comment=False, dry_run=False)
    with patch.object(sut, "sweep_organization", return_value=unhealthy_sweep):
        assert sut.run_action(None, ns_repair) == 0

    healthy_sweep = {"os-autoinst/openQA": [(1, sut.HealthStatus.HEALTHY)]}
    with patch.object(sut, "sweep_organization", return_value=healthy_sweep):
        assert sut.run_action(None, ns_unhealthy) == 0

    ns_no_org = argparse.Namespace(org=None)
    assert sut.run_action(None, ns_no_org) == 1


def test_find_workflow_file_git_root_boundary(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    child = tmp_path / "subdir"
    child.mkdir()
    assert sut.find_workflow_file(start_dir=child) is None


def test_detect_github_repo_fallback_origin_non_github() -> None:
    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["remote", "get-url"]:
            return make_completed_process(stdout="https://gitlab.com/other/project.git\n")
        if cmd[1:3] == ["remote", "-v"]:
            return make_completed_process(
                stdout="origin gitlab\nupstream git@github.com:os-autoinst/openQA.git (fetch)\n"
            )
        return make_completed_process()

    owner, repo = sut.detect_github_repo(runner=fake_runner)
    assert owner == "os-autoinst"
    assert repo == "openQA"


def test_resolve_base_workflow_dir_and_defaults(tmp_path: Path) -> None:
    args_dir = argparse.Namespace(workflow=str(tmp_path), repo=None, source_project=None, source_package=None)
    with patch.object(sut, "find_workflow_file", return_value=None):
        wf = sut.resolve_base_workflow(args_dir, "os-autoinst", "openQA")
        assert not wf.target_project
        assert wf.source_package == "openQA"


def test_fetch_project_meta_parse_error() -> None:
    runner = MagicMock(return_value=make_completed_process(stdout="not-valid-xml"))
    assert sut.fetch_project_meta("proj", runner=runner) is None


def test_check_all_results_missing_code_and_state() -> None:
    xml_content = (
        "<resultlist>"
        '<result project="p" repository="r" arch="a"><status package="pkg"/></result>'
        '<result project="p" repository="r2" arch="a"/>'
        "</resultlist>"
    )
    runner = MagicMock(return_value=make_completed_process(stdout=xml_content))
    assert sut.check_all_results_excluded_or_disabled("proj", runner=runner) is False


def test_extract_branch_request_sha_non_string_fields() -> None:
    assert not sut.extract_branch_request_sha({"pull_request": {"head": {"sha": 123}}})
    assert not sut.extract_branch_request_sha({"head": {"sha": 456}})
    assert not sut.extract_branch_request_sha({"sha": 789})


def test_fetch_pr_details_command_failure() -> None:
    ctx = openqa_context()
    runner = MagicMock(return_value=make_completed_process(returncode=1, stderr="err"))
    assert sut.fetch_pr_details(ctx, runner=runner) == {}


def test_repair_after_cycle_config_repos_fails() -> None:
    ctx = openqa_context()
    empty_report = sut.DiagnosticReport(project_exists=True, repositories=[], package_files=["_service"])
    with (
        patch.object(sut, "diagnose", return_value=empty_report),
        patch.object(sut, "configure_repositories", return_value=False),
    ):
        assert sut.repair_after_cycle(ctx) is False


def test_recreate_project_confirm_yes_and_rdelete_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = openqa_context()
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "rdelete":
            return make_completed_process(returncode=1, stderr="Server 500 internal error")
        return make_completed_process(returncode=0)

    with patch.object(sut, "repair_after_cycle", return_value=True):
        assert sut.recreate_project(ctx, auto_confirm=False, runner=fake_runner) is True


def test_auto_repair_handler_branches_detailed() -> None:
    ctx = openqa_context()
    rep_empty = sut.DiagnosticReport(project_exists=True, status=sut.HealthStatus.BROKEN_EMPTY_PROJECT)
    with (
        patch.object(sut, "configure_repositories", return_value=False),
        patch.object(sut, "repair_branch", return_value=True),
    ):
        ok, acts = sut.auto_repair(ctx, rep_empty)
        assert ok is True
        assert acts == ["repair_branch"]

    with (
        patch.object(sut, "configure_repositories", return_value=True),
        patch.object(sut, "repair_branch", return_value=False),
    ):
        ok, acts = sut.auto_repair(ctx, rep_empty)
        assert ok is True
        assert acts == ["configure_repositories"]

    rep_excl = sut.DiagnosticReport(project_exists=True, status=sut.HealthStatus.BROKEN_EXCLUDED_OR_DISABLED)
    with patch.object(sut, "repair_branch", return_value=True):
        ok, acts = sut.auto_repair(ctx, rep_excl)
        assert ok is True
        assert acts == ["repair_branch"]

    rep_stale = sut.DiagnosticReport(
        project_exists=True,
        status=sut.HealthStatus.BROKEN_STALE_SHA,
        missing_repositories=[],
        pr_head_sha="abc12345",
    )
    ok_dry, acts_dry = sut.auto_repair(ctx, rep_stale, dry_run=True)
    assert ok_dry is True
    assert acts_dry == ["repair_branch"]

    rep_stale_missing = sut.DiagnosticReport(
        project_exists=True,
        status=sut.HealthStatus.BROKEN_STALE_SHA,
        missing_repositories=["16.1"],
        pr_head_sha="abc12345",
    )
    with (
        patch.object(sut, "configure_repositories", return_value=False),
        patch.object(sut, "repair_branch", return_value=True),
    ):
        ok, acts = sut.auto_repair(ctx, rep_stale_missing)
        assert ok is True
        assert acts == ["repair_branch"]

    with (
        patch.object(sut, "configure_repositories", return_value=False),
        patch.object(sut, "repair_branch", return_value=False),
    ):
        ok, acts = sut.auto_repair(ctx, rep_stale_missing)
        assert ok is False
        assert acts == []

    rep_miss_repo = sut.DiagnosticReport(
        project_exists=True,
        status=sut.HealthStatus.MISSING_REPOSITORIES,
        missing_repositories=["16.1"],
    )
    with patch.object(sut, "configure_repositories", return_value=False):
        ok, acts = sut.auto_repair(ctx, rep_miss_repo)
        assert ok is False
        assert acts == []


def test_comment_should_skip_empty_and_invalid_created_at() -> None:
    comments = [
        {"body": f"{sut.BOT_MARKER} sha123", "createdAt": ""},
        {"body": f"{sut.BOT_MARKER} sha123", "createdAt": "not-a-timestamp"},
    ]
    assert sut.should_skip_comment(comments, "sha123") is False


def test_main_org_sweep_and_verify_prerequisites() -> None:
    with (
        patch.object(sut, "verify_prerequisites") as mock_prereq,
        patch.object(sut, "sweep_organization", return_value={"os-autoinst/openQA": []}),
    ):
        assert sut.main(["--org", "os-autoinst"]) == 0
        assert mock_prereq.called

    with (
        patch.object(sut, "resolve_context") as mock_resolve,
        patch.object(sut, "diagnose") as mock_diag,
    ):
        mock_resolve.return_value = openqa_context()
        mock_diag.return_value = sut.DiagnosticReport(status=sut.HealthStatus.HEALTHY)
        assert sut.main(["--skip-auth-check", "--auto-repair"]) == 0


def test_main_entrypoint() -> None:
    with (
        patch.object(sut, "main", return_value=0),
        patch.object(sys, "exit") as mock_exit,
    ):
        main_spec = importlib.util.spec_from_file_location(
            "__main__",
            SCRIPT_PATH,
            loader=importlib.machinery.SourceFileLoader("__main__", str(SCRIPT_PATH)),
        )
        assert main_spec
        assert main_spec.loader
        main_mod = importlib.util.module_from_spec(main_spec)
        main_spec.loader.exec_module(main_mod)
        assert mock_exit.called


def test_find_workflow_file_no_git_and_no_workflow_in_root() -> None:
    with patch("pathlib.Path.resolve", return_value=Path("/nonexistent_test_dir")):
        assert sut.find_workflow_file(start_dir=Path("/")) is None


def test_detect_github_repo_fallback_to_gh_cli() -> None:
    def fake_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["remote", "get-url"]:
            return make_completed_process(stdout="https://gitlab.com/other/project.git\n")
        if cmd[1:3] == ["remote", "-v"]:
            return make_completed_process(stdout="\nsingle_word\norigin https://gitlab.com/repo.git (fetch)\n")
        if cmd[0] == "gh":
            return make_completed_process(stdout="os-autoinst/openQA\n")
        return make_completed_process()

    owner, repo = sut.detect_github_repo(runner=fake_runner)
    assert owner == "os-autoinst"
    assert repo == "openQA"


def test_resolve_base_workflow_no_workflow_no_repo() -> None:
    args_empty = argparse.Namespace(workflow=None, repo=None, source_project=None, source_package=None)
    with patch.object(sut, "find_workflow_file", return_value=None):
        wf = sut.resolve_base_workflow(args_empty, "os-autoinst", "openQA")
        assert not wf.target_project


def test_extract_branch_request_sha_head_not_dict() -> None:
    assert not sut.extract_branch_request_sha({"pull_request": {"head": "not-a-dict"}})


def test_sweep_pr_auto_repair_without_comment() -> None:
    options = argparse.Namespace(auto_repair=True, comment=False, dry_run=False)
    report = sut.DiagnosticReport(project_exists=True, status=sut.HealthStatus.BROKEN_STALE_SHA)
    ctx = openqa_context()
    with (
        patch.object(sut, "resolve_context", return_value=ctx),
        patch.object(sut, "diagnose", return_value=report),
        patch.object(sut, "auto_repair", return_value=(True, ["repair_branch"])) as mock_repair,
        patch.object(sut, "post_pr_comment") as mock_comment,
    ):
        status = sut.sweep_pr("os-autoinst/openQA", 7985, options)
        assert status == sut.HealthStatus.BROKEN_STALE_SHA
        assert mock_repair.called
        assert not mock_comment.called
