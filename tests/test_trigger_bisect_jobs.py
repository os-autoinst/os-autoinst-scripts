"""tests for openqa-trigger-bisect-jobs."""

import importlib.machinery
import importlib.util
import json
import pathlib
import re
from argparse import Namespace
from typing import Any
from unittest.mock import MagicMock, call, patch
from urllib.parse import urlparse

import pytest
import requests

rootpath = pathlib.Path(__file__).parent.parent.resolve()

loader = importlib.machinery.SourceFileLoader("openqa", f"{rootpath}/openqa-trigger-bisect-jobs")
spec = importlib.util.spec_from_loader(loader.name, loader)
openqa = importlib.util.module_from_spec(spec)
loader.exec_module(openqa)

Incident = openqa.Incident


def args_factory() -> Namespace:
    args = Namespace()
    args.dry_run = False
    args.verbose = 1
    args.priority_add = 100
    return args


def mocked_fetch_url(url: str, request_type: str = "text") -> Any:
    content = ""
    url = urlparse(url)

    if url.scheme in {"http", "https"}:
        path = url.geturl()
        path = path[len(url.scheme) + 3 :]
        path = "tests/data/python-requests/" + path
        with pathlib.Path(path).open(encoding="utf-8") as request:
            content = request.read()
    if request_type == "json":
        return json.loads(content)
    return content


def mocked_call(cmds: list[str], dry_run: bool = False) -> list[str]:
    return cmds


orig_fetch_url = openqa.fetch_url

cmds = [
    "https://openqa.opensuse.org/tests/7848818",
    "OS_TEST_ISSUES=21770,21926,21954,22030,22077,22085,22192",
    "TEST=foo:investigate:bisect_without_21637",
    "OPENQA_INVESTIGATE_ORIGIN=https://openqa.opensuse.org/tests/7848818",
]


def test_catch_CalledProcessError(caplog: pytest.LogCaptureFixture) -> None:
    import subprocess  # noqa: S404

    args = args_factory()
    args.url = "https://openqa.opensuse.org/tests/7848818"
    openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)
    exp_err = "returned non-zero exit status 255."
    error = subprocess.CompletedProcess(args=[], returncode=255, stderr=exp_err, stdout="")
    with patch("subprocess.run", return_value=error), pytest.raises(subprocess.CalledProcessError) as e:
        openqa.main(args)

    assert e.value.returncode == 255
    assert f"{exp_err}" in str(e.value.stderr)

    exp_err = "Current job 7848818 will fail, because the repositories for the below updates are unavailable"
    error.stderr = exp_err
    comment_process = subprocess.CompletedProcess(args=[], returncode=0, stderr="", stdout=b"doo")
    with patch("subprocess.run", side_effect=[error, comment_process]) as mocked:
        with pytest.raises(SystemExit) as e:
            openqa.main(args)
        assert re.search(
            r"jobs/.*/comments.*text=.*updates are unavailable",
            str(mocked.call_args_list[-1][0]),
        )
    assert e.value.code == 0
    assert f"{exp_err}" in caplog.text


def test_clone() -> None:
    openqa.call = MagicMock(side_effect=mocked_call)
    openqa.openqa_clone(cmds, dry_run=False)
    args = [
        "openqa-clone-job",
        "--skip-chained-deps",
        "--json-output",
        "--within-instance",
        "https://openqa.opensuse.org/tests/7848818",
        "OS_TEST_ISSUES=21770,21926,21954,22030,22077,22085,22192",
        "TEST=foo:investigate:bisect_without_21637",
        "OPENQA_INVESTIGATE_ORIGIN=https://openqa.opensuse.org/tests/7848818",
        "_GROUP=0",
    ]
    openqa.call.assert_called_once_with(args, False)


def test_comment() -> None:
    openqa.call = MagicMock(side_effect=mocked_call)
    openqa.openqa_comment(1234567, "https://openqa.opensuse.org", "foo\nbar", dry_run=False)
    args = [
        "openqa-cli",
        "api",
        "--header",
        "User-Agent: openqa-trigger-bisect-jobs (https://github.com/os-autoinst/os-autoinst-scripts)",
        "--host",
        "https://openqa.opensuse.org",
        "-X",
        "POST",
        "jobs/1234567/comments",
        "text=foo\nbar",
    ]
    openqa.call.assert_called_once_with(args, False)


def test_set_job_prio() -> None:
    openqa.call = MagicMock(side_effect=mocked_call)
    openqa.openqa_set_job_prio(1234567, "https://openqa.opensuse.org", 42, dry_run=False)
    args = [
        "openqa-cli",
        "api",
        "--header",
        "User-Agent: openqa-trigger-bisect-jobs (https://github.com/os-autoinst/os-autoinst-scripts)",
        "--host",
        "https://openqa.opensuse.org",
        "--json",
        "--data",
        '{"priority": 42}',
        "-X",
        "PUT",
        "jobs/1234567",
    ]
    openqa.call.assert_called_once_with(args, False)


def test_triggers() -> None:
    args = args_factory()
    args.url = "https://openqa.opensuse.org/tests/7848818"
    openqa.openqa_clone = MagicMock(return_value='{"7848818": 234567}')
    openqa.openqa_comment = MagicMock(return_value="")
    openqa.openqa_set_job_prio = MagicMock(return_value="")
    openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)
    openqa.main(args)
    calls = [
        call(
            [
                "https://openqa.opensuse.org/tests/7848818",
                "CRAZY_TEST_ISSUES=1,4",
                "COMMON_TEST_ISSUES=1,4,21637,21770,21926,21954,22030,22077,22085,22192",
                "TEST=foo:investigate:bisect_without_3",
                "OPENQA_INVESTIGATE_ORIGIN=https://openqa.opensuse.org/tests/7848818",
                "MAINT_TEST_REPO=",
            ],
            False,
        ),
        call(
            [
                "https://openqa.opensuse.org/tests/7848818",
                "CRAZY_TEST_ISSUES=1,3",
                "COMMON_TEST_ISSUES=1,3,21637,21770,21926,21954,22030,22077,22085,22192",
                "TEST=foo:investigate:bisect_without_4",
                "OPENQA_INVESTIGATE_ORIGIN=https://openqa.opensuse.org/tests/7848818",
                "MAINT_TEST_REPO=",
            ],
            False,
        ),
        call(
            [
                "https://openqa.opensuse.org/tests/7848818",
                "OS_TEST_ISSUES=21770,21926,21954,22030,22077,22085,22192",
                "COMMON_TEST_ISSUES=1,3,4,21770,21926,21954,22030,22077,22085,22192",
                "TEST=foo:investigate:bisect_without_21637",
                "OPENQA_INVESTIGATE_ORIGIN=https://openqa.opensuse.org/tests/7848818",
                "MAINT_TEST_REPO=",
            ],
            False,
        ),
        call(
            [
                "https://openqa.opensuse.org/tests/7848818",
                "OS_TEST_ISSUES=21637,21770,21926,21954,22030,22077,22192",
                "COMMON_TEST_ISSUES=1,3,4,21637,21770,21926,21954,22030,22077,22192",
                "TEST=foo:investigate:bisect_without_22085",
                "OPENQA_INVESTIGATE_ORIGIN=https://openqa.opensuse.org/tests/7848818",
                "MAINT_TEST_REPO=",
            ],
            False,
        ),
        call(
            [
                "https://openqa.opensuse.org/tests/7848818",
                "OS_TEST_ISSUES=21637,21770,21926,21954,22030,22077,22085",
                "COMMON_TEST_ISSUES=1,3,4,21637,21770,21926,21954,22030,22077,22085",
                "TEST=foo:investigate:bisect_without_22192",
                "OPENQA_INVESTIGATE_ORIGIN=https://openqa.opensuse.org/tests/7848818",
                "MAINT_TEST_REPO=",
            ],
            False,
        ),
    ]
    assert sorted(calls) == sorted(openqa.openqa_clone.call_args_list)
    openqa.openqa_comment.assert_called_once_with(
        7848818,
        "https://openqa.opensuse.org",
        "Automatic bisect jobs:\n\n* **foo:investigate:bisect_without_3**: https://openqa.opensuse.org/t234567\n* **foo:investigate:bisect_without_4**: https://openqa.opensuse.org/t234567\n* **foo:investigate:bisect_without_21637**: https://openqa.opensuse.org/t234567\n* **foo:investigate:bisect_without_22085**: https://openqa.opensuse.org/t234567\n* **foo:investigate:bisect_without_22192**: https://openqa.opensuse.org/t234567\n",
        False,
    )
    prio_calls = 5 * [call(234567, "https://openqa.opensuse.org/tests/7848818", 150, False)]
    assert prio_calls == openqa.openqa_set_job_prio.call_args_list


def test_problems() -> None:
    args = args_factory()
    openqa.openqa_clone = MagicMock(return_value="")
    openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)

    args.url = "http://openqa.opensuse.org/tests/123"
    try:
        openqa.main(args)
        raise AssertionError
    except json.decoder.JSONDecodeError as e:
        assert str(e) == "Expecting value: line 1 column 1 (char 0)"

    args.url = "http://openqa.opensuse.org/tests/1234"
    openqa.main(args)
    openqa.openqa_clone.assert_not_called()

    args.url = "http://openqa.opensuse.org/tests/12345"
    openqa.main(args)
    openqa.openqa_clone.assert_not_called()

    args.url = "http://openqa.opensuse.org/tests/100"
    openqa.main(args)
    openqa.openqa_clone.assert_not_called()

    args.url = "http://openqa.opensuse.org/tests/101"
    openqa.log.info = MagicMock()
    openqa.main(args)
    openqa.log.info.assert_called_with("Job 101 (foo) is passed, skipping bisection")
    assert (
        call(
            "http://openqa.opensuse.org/tests/101/investigation_ajax",
            request_type="json",
        )
        not in openqa.fetch_url.mock_calls
    )
    openqa.openqa_clone.assert_not_called()


def test_directly_chained() -> None:
    args = args_factory()
    openqa.openqa_clone = MagicMock(return_value="")
    openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)

    args.url = "http://openqa.opensuse.org/tests/123456"
    openqa.main(args)
    openqa.openqa_clone.assert_not_called()


def test_exclude_already_retried() -> None:
    args = args_factory()
    openqa.openqa_clone = MagicMock(return_value="")
    openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)

    args.url = "http://openqa.opensuse.org/tests/123458"
    openqa.main(args)
    openqa.openqa_clone.assert_not_called()


def test_exclude_group_regex() -> None:
    args = args_factory()
    openqa.openqa_clone = MagicMock(return_value="")
    openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)
    args.url = "http://openqa.opensuse.org/tests/123457"
    # should only affect test_exclude_group_regex() as it does not match other tests
    with patch.dict("os.environ", {"exclude_group_regex": "s.*parent?-group / some-.*"}):
        openqa.main(args)
    openqa.openqa_clone.assert_not_called()


def test_exclude_name_regex() -> None:
    args = args_factory()
    openqa.openqa_clone = MagicMock(return_value="")
    openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)
    args.url = "http://openqa.opensuse.org/tests/123457"
    with patch.dict("os.environ", {"exclude_name_regex": "with.*group"}):
        openqa.main(args)
    openqa.openqa_clone.assert_not_called()


def test_exclude_investigated() -> None:
    args = args_factory()
    openqa.openqa_clone = MagicMock(return_value="")
    openqa.fetch_url = MagicMock(side_effect=mocked_fetch_url)

    args.url = "http://openqa.opensuse.org/tests/123460"
    openqa.main(args)
    openqa.openqa_clone.assert_not_called()


def test_network_problems() -> None:
    args = args_factory()
    args.url = "http://doesnotexist.openqa.opensuse.org/tests/12345"
    openqa.openqa_clone = MagicMock(return_value="")
    openqa.fetch_url = MagicMock(side_effect=orig_fetch_url)
    try:
        openqa.main(args)
        raise AssertionError
    except requests.exceptions.ConnectionError:
        assert True


def test_issue_types() -> None:
    investigation = '- "OS_TEST_ISSUES": "1,2,3,4",\n+ "OS_TEST_ISSUES": "1,2,3,4,5",\n- "OTHER_TEST_ISSUES": "23",\n+ "OTHER_TEST_ISSUES": "24",\n+ "DUMMY_TEST_ISSUES": "25,26,27",'
    changes = openqa.find_changed_issues(investigation)
    exp = {
        "OS_TEST_ISSUES": {
            "-": {Incident("1"), Incident("3"), Incident("2"), Incident("4")},
            "+": {
                Incident("2"),
                Incident("5"),
                Incident("4"),
                Incident("1"),
                Incident("3"),
            },
        }
    }
    assert changes == exp

    investigation_repos = '- "OS_TEST_ISSUES": "1,2,3,4",\n- "OS_TEST_REPOS": "1,2,3,4",\n+ "OS_TEST_ISSUES": "1,2,3,4,5",\n+ "OS_TEST_REPOS": "1,2,3,4,5",\n- "OTHER_TEST_ISSUES": "23",\n+ "OTHER_TEST_ISSUES": "24",\n+ "DUMMY_TEST_ISSUES": "25,26,27",'
    changes_repos = openqa.find_changed_issues(investigation_repos)
    exp_repos = {
        "OS_TEST_REPOS": {
            "-": {Incident("1"), Incident("3"), Incident("2"), Incident("4")},
            "+": {
                Incident("2"),
                Incident("5"),
                Incident("4"),
                Incident("1"),
                Incident("3"),
            },
        }
    }
    assert changes_repos == exp_repos
