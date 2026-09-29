# Copyright SUSE LLC
"""Unit tests for openqa-investigate."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, Mock

import httpx2
import pytest
import typer

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# Load the script dynamically as a module
rootpath = pathlib.Path(__file__).parent.parent.resolve()
path = rootpath / "openqa-investigate"
spec = importlib.util.spec_from_file_location(
    "openqa_investigate",
    path,
    loader=importlib.machinery.SourceFileLoader("openqa_investigate", str(path)),
)
assert spec is not None
assert spec.loader is not None
openqa_investigate = importlib.util.module_from_spec(spec)
sys.modules["openqa_investigate"] = openqa_investigate
spec.loader.exec_module(openqa_investigate)


@pytest.mark.parametrize(
    ("verbose_count", "expected_level"),
    [
        (0, logging.WARNING),
        (1, logging.INFO),
        (2, logging.DEBUG),
        (3, logging.DEBUG),
    ],
)
def test_setup_logging(mocker: MockerFixture, verbose_count: int, expected_level: int) -> None:
    mock_basic_config = mocker.patch("openqa_investigate.logging.basicConfig")
    openqa_investigate.setup_logging(verbose_count)
    mock_basic_config.assert_called_once_with(
        level=expected_level, format="%(levelname)s: %(message)s", stream=sys.stderr, force=True
    )


@pytest.mark.parametrize(
    ("job_url_arg", "default_scheme", "default_host", "expected"),
    [
        ("https://openqa.opensuse.org/tests/1234", "http", "localhost", (1234, "https", "openqa.opensuse.org")),
        ("http://test.host/t5678", "https", "openqa.opensuse.org", (5678, "http", "test.host")),
        ("https://my.openqa.org/tests/9012/", "https", "openqa.opensuse.org", (9012, "https", "my.openqa.org")),
    ],
)
def test_parse_job_url_valid(
    job_url_arg: str, default_scheme: str, default_host: str, expected: tuple[int, str, str]
) -> None:
    assert openqa_investigate.parse_job_url(job_url_arg, default_scheme, default_host) == expected


@pytest.mark.parametrize(
    ("job_url_arg", "default_scheme", "default_host", "match_pattern"),
    [
        ("abc", "https", "openqa.opensuse.org", "Invalid job URL: abc"),
        ("https://openqa.opensuse.org/", "https", "openqa.opensuse.org", "Could not extract job ID from URL path: /"),
        ("https://openqa.opensuse.org/tests/", "https", "openqa.opensuse.org", "Could not extract numeric job ID"),
        ("https://openqa.opensuse.org/tests/abc", "https", "openqa.opensuse.org", "Could not extract numeric job ID"),
    ],
)
def test_parse_job_url_invalid(job_url_arg: str, default_scheme: str, default_host: str, match_pattern: str) -> None:
    with pytest.raises(ValueError, match=match_pattern):
        openqa_investigate.parse_job_url(job_url_arg, default_scheme, default_host)


def test_client_init() -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org/")
    assert client.host_url == "https://openqa.opensuse.org"
    assert client.retries == 3
    assert client.retry_sleep_time == 20
    assert not client.dry_run


def test_client_run_openqa_cli(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("openqa_investigate.subprocess.run")
    mock_run.return_value = Mock(stdout='{"status": "ok"}')

    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    res = client._run_openqa_cli(["jobs/123"])
    assert res == {"status": "ok"}
    mock_run.assert_called_once_with(
        [
            "openqa-cli",
            "api",
            "--header",
            f"User-Agent: {openqa_investigate.USER_AGENT}",
            "--host",
            "https://openqa.opensuse.org",
            "--retries",
            "3",
            "jobs/123",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_client_run_openqa_cli_dry_run() -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org", dry_run=True)
    res = client._run_openqa_cli(["-X", "POST", "jobs/123/comments"], mutate=True)
    assert res == {"dry_run": True}


def test_client_run_openqa_cli_variants(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    mock_run = mocker.patch("openqa_investigate.subprocess.run")

    # Debug log and mutate=False with dry_run=True (should still execute subprocess)
    client_dry = openqa_investigate.OpenQAClient("https://openqa.opensuse.org", dry_run=True)
    mock_run.return_value = Mock(stdout='[{"id": 1}]')
    with caplog.at_level(logging.DEBUG):
        res = client_dry._run_openqa_cli(["jobs/1"])
    assert res == [{"id": 1}]
    assert "Executing: openqa-cli api" in caplog.text

    # CalledProcessError branch
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    mock_run.side_effect = subprocess.CalledProcessError(1, ["cmd"], stderr="CLI failure")
    with caplog.at_level(logging.ERROR), pytest.raises(subprocess.CalledProcessError):
        client._run_openqa_cli(["jobs/2"])
    assert "Error executing openqa-cli: CLI failure" in caplog.text

    # Empty stdout
    mock_run.side_effect = None
    mock_run.return_value = Mock(stdout="   ")
    assert client._run_openqa_cli(["jobs/3"]) == {}

    # Invalid JSON
    mock_run.return_value = Mock(stdout="Not a json")
    assert client._run_openqa_cli(["jobs/4"]) == {"raw_output": "Not a json"}

    # JSON primitive (not dict or list)
    mock_run.return_value = Mock(stdout="12345")
    assert client._run_openqa_cli(["jobs/5"]) == {"raw_output": "12345"}


@pytest.mark.parametrize(
    ("method_name", "args", "expected_cli_args"),
    [
        ("get_job_status", [123], ["--json", "experimental/jobs/123/status"]),
        ("get_job", [123], ["--json", "jobs/123"]),
        ("get_job_comments", [123], ["--json", "jobs/123/comments"]),
        ("post_job_comment", [123, "hello"], ["-X", "POST", "jobs/123/comments", "text=hello"]),
        ("put_job_comment", [123, 456, "hello"], ["-X", "PUT", "jobs/123/comments/456", "text=hello"]),
        ("delete_job_comment", [123, 456], ["-X", "DELETE", "jobs/123/comments/456"]),
        ("put_job_priority", [123, 50], ["--json", "--data", '{"priority": 50}', "-X", "PUT", "jobs/123"]),
    ],
)
def test_client_api_methods(
    mocker: MockerFixture, method_name: str, args: list[Any], expected_cli_args: list[str]
) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    mock_run = mocker.patch.object(client, "_run_openqa_cli", return_value={"status": "ok"})
    method = getattr(client, method_name)

    res = method(*args)

    if method_name == "get_job_comments":
        assert isinstance(res, list)
    else:
        assert res == {"status": "ok"}

    if method_name in {"get_job_status", "get_job", "get_job_comments"}:
        mock_run.assert_called_once_with(expected_cli_args)
    else:
        mock_run.assert_called_once_with(expected_cli_args, mutate=True)


def test_client_api_methods_non_dict_fallback(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    mocker.patch.object(client, "_run_openqa_cli", return_value=["non-dict"])

    assert client.get_job_status(1) == {}
    assert client.get_job(1) == {}
    assert client.post_job_comment(1, "x") == {}
    assert client.put_job_comment(1, 2, "x") == {}
    assert client.delete_job_comment(1, 2) == {}
    assert client.put_job_priority(1, 10) == {}


def test_client_get_job_comments_variations(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Already a list
    mocker.patch.object(client, "_run_openqa_cli", return_value=[{"id": 1}])
    assert client.get_job_comments(123) == [{"id": 1}]

    # Non-list fallback
    mocker.patch.object(client, "_run_openqa_cli", return_value=123)
    assert client.get_job_comments(123) == []


def test_client_get_http(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    mock_response = MagicMock(spec=httpx2.Response)
    mock_response.json.return_value = {"status": "ok"}

    mock_client_inst = MagicMock(spec=httpx2.Client)
    mock_client_inst.__enter__.return_value = mock_client_inst
    mock_client_inst.get.return_value = mock_response

    mocker.patch("openqa_investigate.httpx2.Client", return_value=mock_client_inst)

    with caplog.at_level(logging.DEBUG):
        res = client._get_http("tests/123/dependencies_ajax")
    assert res == {"status": "ok"}
    assert "HTTP GET: https://openqa.opensuse.org/tests/123/dependencies_ajax" in caplog.text
    mock_client_inst.get.assert_called_once_with("https://openqa.opensuse.org/tests/123/dependencies_ajax")

    # Custom timeout
    res_custom = client._get_http("tests/123/dependencies_ajax", timeout=45.0)
    assert res_custom == {"status": "ok"}


def test_client_get_http_variants(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    # Non-dict JSON response returns {"data": res_json}
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    mock_resp_list = MagicMock(spec=httpx2.Response)
    mock_resp_list.json.return_value = [1, 2, 3]
    mock_client_inst = MagicMock(spec=httpx2.Client)
    mock_client_inst.__enter__.return_value = mock_client_inst
    mock_client_inst.get.return_value = mock_resp_list
    mocker.patch("openqa_investigate.httpx2.Client", return_value=mock_client_inst)
    assert client._get_http("test/path") == {"data": [1, 2, 3]}

    # Retry then succeed
    mocker.patch("time.sleep")
    client_retry = openqa_investigate.OpenQAClient("https://openqa.opensuse.org", retries=2)
    resp_ok = MagicMock(spec=httpx2.Response)
    resp_ok.json.return_value = {"ok": True}
    mock_client_inst.get.side_effect = [Exception("Temporary error"), resp_ok]
    with caplog.at_level(logging.WARNING):
        assert client_retry._get_http("test/retry") == {"ok": True}
    assert "HTTP GET failed (attempt 1/3)" in caplog.text

    # All attempts fail -> raises
    mock_client_inst.get.side_effect = Exception("Persistent error")
    with caplog.at_level(logging.ERROR), pytest.raises(Exception, match="Persistent error"):
        client_retry._get_http("test/fail")
    assert "HTTP GET failed after 3 attempts" in caplog.text
    assert "Exception: Persistent error" in caplog.text

    # retries < 0 (covers line 119 return {})
    client_empty = openqa_investigate.OpenQAClient("https://openqa.opensuse.org", retries=-1)
    assert client_empty._get_http("test/empty") == {}


def test_client_get_dependencies_ajax_fallback(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    mocker.patch.object(client, "_get_http", side_effect=httpx2.HTTPError("HTTP error"))
    mock_cli = mocker.patch.object(client, "_run_openqa_cli", return_value={"fallback": "ok"})

    with caplog.at_level(logging.DEBUG):
        res = client.get_dependencies_ajax(123)
    assert res == {"fallback": "ok"}
    assert "HTTP GET failed, falling back to openqa-cli" in caplog.text
    mock_cli.assert_called_once_with(["-X", "GET", "tests/123/dependencies_ajax"])

    # Fallback returning non-dict
    mock_cli.return_value = ["non-dict"]
    assert client.get_dependencies_ajax(123) == {}


def test_client_endpoints(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    assert client.http_timeout == pytest.approx(90.0)
    assert client.investigation_timeout == pytest.approx(120.0)
    mock_http = mocker.patch.object(client, "_get_http", return_value={"data": 1})

    assert client.get_investigation_ajax(123) == {"data": 1}
    mock_http.assert_called_with("tests/123/investigation_ajax", timeout=120.0)

    assert client.get_vars_json(123) == {"data": 1}
    mock_http.assert_called_with("tests/123/file/vars.json")


def test_clone_job_helper(mocker: MockerFixture) -> None:
    mock_run = mocker.patch("openqa_investigate.subprocess.run")
    mock_run.return_value = Mock(stdout='{"123": 456}')

    res = openqa_investigate.clone_job("https://openqa.opensuse.org", 123, ["TEST=foo"])
    assert res == {"123": 456}
    mock_run.assert_called_once_with(
        [
            "openqa-clone-job",
            "--json-output",
            "--skip-chained-deps",
            "--max-depth",
            "0",
            "--parental-inheritance",
            "--within-instance",
            "https://openqa.opensuse.org/tests/123",
            "TEST=foo",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_clone_job_helper_dry_run() -> None:
    res = openqa_investigate.clone_job("https://openqa.opensuse.org", 123, ["TEST=foo"], dry_run=True)
    assert res == {"123": 42}


def test_clone_job_variants(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    mock_run = mocker.patch("openqa_investigate.subprocess.run")

    # Debug execution
    mock_run.return_value = Mock(stdout='{"1": 2}')
    with caplog.at_level(logging.DEBUG):
        openqa_investigate.clone_job("https://openqa.opensuse.org", 1, [])
    assert "Executing: openqa-clone-job" in caplog.text

    # CalledProcessError
    mock_run.side_effect = subprocess.CalledProcessError(1, ["cmd"], stderr="Clone error")
    with caplog.at_level(logging.ERROR), pytest.raises(subprocess.CalledProcessError):
        openqa_investigate.clone_job("https://openqa.opensuse.org", 1, [])
    assert "Error executing openqa-clone-job: Clone error" in caplog.text

    # Invalid JSON
    mock_run.side_effect = None
    mock_run.return_value = Mock(stdout="not-json")
    assert openqa_investigate.clone_job("https://openqa.opensuse.org", 1, []) == {"raw_output": "not-json"}

    # Non-dict JSON
    mock_run.return_value = Mock(stdout="[1, 2]")
    assert openqa_investigate.clone_job("https://openqa.opensuse.org", 1, []) == {"raw_output": "[1, 2]"}


def test_check_clone_prerequisites() -> None:
    # No chained jobs -> succeeds
    openqa_investigate._check_clone_prerequisites({}, 123)

    # Chained children -> raises Exit(code=2)
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate._check_clone_prerequisites({"children": {"Directly chained": [1]}}, 123)
    assert exc.value.exit_code == 2

    # Chained parents -> raises Exit(code=2)
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate._check_clone_prerequisites({"parents": {"Directly chained": [1]}}, 123)
    assert exc.value.exit_code == 2


def test_prepare_refspec_settings(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Invalid TEST_GIT_URL
    mocker.patch.object(client, "get_vars_json", return_value={"TEST_GIT_URL": "ftp://bad.url"})
    settings, ext = openqa_investigate._prepare_refspec_settings(client, 1, "test", "suffix", "ref", None)
    assert settings == ["INVALID_URL"]
    assert not ext

    # TEST_GIT_URL with space
    mocker.patch.object(client, "get_vars_json", return_value={"TEST_GIT_URL": "https://good.url/repo with space"})
    settings, ext = openqa_investigate._prepare_refspec_settings(client, 1, "test", "suffix", "ref", None)
    assert settings == ["INVALID_URL"]

    # Valid TEST_GIT_URL
    mocker.patch.object(client, "get_vars_json", return_value={"TEST_GIT_URL": "https://git.repo/test.git#master"})
    settings, ext = openqa_investigate._prepare_refspec_settings(client, 1, "test", "suffix", "ref", None)
    assert settings == ["CASEDIR=https://git.repo/test.git#ref"]
    assert not ext

    # Default casedir when TEST_GIT_URL is absent and casedir is None
    mocker.patch.object(client, "get_vars_json", return_value={})
    settings, ext = openqa_investigate._prepare_refspec_settings(client, 1, "test", "suffix", "ref", None)
    assert settings == ["CASEDIR=https://github.com/os-autoinst/os-autoinst-distri-opensuse.git#ref"]

    # name_suffix with last_good_tests_and_build and WORKER_CLASS present
    mocker.patch.object(client, "get_vars_json", return_value={"WORKER_CLASS": "qemu_x86_64"})
    settings, ext = openqa_investigate._prepare_refspec_settings(
        client, 1, "mytest", "last_good_tests_and_build", "ref", "https://custom.repo.git"
    )
    assert "WORKER_CLASS:mytest=qemu_x86_64" in settings
    assert not ext

    # name_suffix with last_good_tests_and_build and WORKER_CLASS missing
    mocker.patch.object(client, "get_vars_json", return_value={})
    settings, ext = openqa_investigate._prepare_refspec_settings(
        client, 1, "mytest", "last_good_tests_and_build", "ref", "https://custom.repo.git"
    )
    assert ext == "(unidentified worker class in vars.json)"


def test_build_clone_settings(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # refspec is None and extra_settings is None
    settings_none, name_none = openqa_investigate._build_clone_settings(
        client, {"test": "test_none"}, 1, "", None, None, 0
    )
    assert name_none == "test_none:investigate"
    assert "TEST+=:investigate" in settings_none
    mocker.patch.object(openqa_investigate, "_prepare_refspec_settings", return_value=(["INVALID_URL"], ""))
    settings, name = openqa_investigate._build_clone_settings(client, {}, 1, "suf", "ref", None, 0)
    assert settings == []
    assert not name

    # Valid settings, PUBLISH_* keys, extra_settings, name_suffix
    mocker.patch.object(openqa_investigate, "_prepare_refspec_settings", return_value=(["CASEDIR=foo#ref"], "_ext"))
    job_info = {
        "test": "sample_test",
        "settings": {"PUBLISH_HDD": "1", "PUBLISH_ISO": "1", "OTHER_SETTING": "val"},
    }
    settings, name = openqa_investigate._build_clone_settings(client, job_info, 10, "suffix", "ref", ["EXTRA=1"], 42)
    assert name == "sample_test:investigate:suffix_ext"
    assert "_TRIGGER_JOB_DONE_HOOK=1" in settings
    assert "_GROUP_ID=42" in settings
    assert "BUILD=" in settings
    assert "CASEDIR=foo#ref" in settings
    assert "TEST+=:investigate:suffix" in settings
    assert "EXTRA=1" in settings
    assert "PUBLISH_HDD=none" in settings
    assert "PUBLISH_ISO=none" in settings
    assert "OPENQA_INVESTIGATE_ORIGIN=https://openqa.opensuse.org/t10" in settings


def test_clone(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Unable to query job data -> Exit(1)
    mocker.patch.object(client, "get_job", return_value={})
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.clone(client, 1, 2, "retry", None, None, 0, 100)
    assert exc.value.exit_code == 1

    # Empty settings and refspec -> returns ""
    job_data = {"job": {"test": "foo", "priority": "50"}}
    mocker.patch.object(client, "get_job", return_value=job_data)
    mocker.patch.object(openqa_investigate, "_check_clone_prerequisites")
    mocker.patch.object(openqa_investigate, "_build_clone_settings", return_value=([], ""))
    assert not openqa_investigate.clone(client, 1, 2, "retry", "ref", None, 0, 100)

    # Successful clone with priority updates
    mocker.patch.object(openqa_investigate, "_build_clone_settings", return_value=(["SET=1"], "foo:investigate:retry"))
    mocker.patch.object(openqa_investigate, "clone_job", return_value={"2": 999, "non_digit": "abc"})
    mock_prio = mocker.patch.object(client, "put_job_priority")

    res = openqa_investigate.clone(client, 1, 2, "retry", None, None, 0, 50)
    assert res == "* *foo:investigate:retry*: t#999"
    mock_prio.assert_called_once_with(999, 100)

    # Base priority is None -> does not call put_job_priority
    job_data_no_prio = {"job": {"test": "foo"}}
    mocker.patch.object(client, "get_job", return_value=job_data_no_prio)
    mock_prio.reset_mock()
    openqa_investigate.clone(client, 1, 2, "retry", None, None, 0, 50)
    mock_prio.assert_not_called()


def test_trigger_regression_jobs(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # 1. No changes in test_log, BUILD present, last_good_tests empty -> "No test regression expected"
    lines: list[str] = []
    inv1 = {"test_log": "No test changes recorded", "BUILD": "123"}
    mocker.patch.object(client, "get_vars_json", return_value={"BUILD": "100"})
    mock_clone = mocker.patch.object(openqa_investigate, "clone", return_value="clone_line_build")
    openqa_investigate._trigger_regression_jobs(client, 1, 2, inv1, None, 0, 100, lines)
    assert lines == ["clone_line_build"]

    # 2. Test changes present, BUILD not in investigation -> skips product regression, "No product regression expected"
    lines.clear()
    inv2 = {"test_log": "Changes detected"}
    mocker.patch.object(client, "get_vars_json", return_value={"TEST_GIT_HASH": "abc"})
    mock_clone.side_effect = ["clone_line_tests"]
    openqa_investigate._trigger_regression_jobs(client, 1, 2, inv2, None, 0, 100, lines)
    assert lines == ["clone_line_tests"]

    # 3. Both test and build present -> triggers all 3 regression jobs
    lines.clear()
    inv3 = {"test_log": "Changes detected", "BUILD": "999"}
    mocker.patch.object(client, "get_vars_json", return_value={"TEST_GIT_HASH": "abc", "BUILD": "888"})
    mock_clone.side_effect = ["line_tests", "", "line_both"]
    openqa_investigate._trigger_regression_jobs(client, 1, 2, inv3, None, 0, 100, lines)
    assert lines == ["line_tests", "line_both"]

    # 4. Clone returns empty string for last_good_tests and for last_good_tests_and_build
    lines.clear()
    inv4 = {"test_log": "Changes detected", "BUILD": "999"}
    mocker.patch.object(client, "get_vars_json", return_value={"TEST_GIT_HASH": "abc", "BUILD": "888"})
    mock_clone.side_effect = ["", "line_build", ""]
    openqa_investigate._trigger_regression_jobs(client, 1, 2, inv4, None, 0, 100, lines)
    assert lines == ["line_build"]


def test_trigger_jobs(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Retry clone returns empty line, last_good is missing
    mocker.patch.object(openqa_investigate, "clone", return_value="")
    mocker.patch.object(client, "get_investigation_ajax", return_value={})
    assert not openqa_investigate.trigger_jobs(client, 1, None, 0, 100)

    # last_good is "not found"
    mocker.patch.object(client, "get_investigation_ajax", return_value={"last_good": "not found"})
    assert not openqa_investigate.trigger_jobs(client, 1, None, 0, 100)

    # last_good.text is not numeric
    mocker.patch.object(client, "get_investigation_ajax", return_value={"last_good": {"text": "invalid"}})
    assert not openqa_investigate.trigger_jobs(client, 1, None, 0, 100)

    # Valid last_good -> calls _trigger_regression_jobs
    mocker.patch.object(openqa_investigate, "clone", return_value="retry_line")
    mocker.patch.object(client, "get_investigation_ajax", return_value={"last_good": {"text": "456"}})
    mock_regr = mocker.patch.object(
        openqa_investigate, "_trigger_regression_jobs", side_effect=lambda *args: args[7].append("regr_line")
    )
    out = openqa_investigate.trigger_jobs(client, 1, None, 0, 100)
    assert out == "retry_line\nregr_line"
    mock_regr.assert_called_once()


def test_should_exclude_job() -> None:
    # Matches exclude_name_regex
    assert openqa_investigate._should_exclude_job({}, "foo:investigate:bar", ":investigate:", "regex")

    # Job without group when exclude_no_group is True
    assert openqa_investigate._should_exclude_job({}, "name", "none", "regex", exclude_no_group=True)

    # Job without group when exclude_no_group is False
    assert not openqa_investigate._should_exclude_job({}, "name", "none", "regex", exclude_no_group=False)

    # Matches exclude_group_regex
    job_info_group = {"parent_group": "Development", "group": "test"}
    assert openqa_investigate._should_exclude_job(
        job_info_group, "name", "none", r"Development.*", exclude_no_group=True
    )

    # Case where NO_INVESTIGATION is 1
    job_info_no_inv = {"group": "grp", "settings": {"NO_INVESTIGATION": "1"}}
    assert openqa_investigate._should_exclude_job(job_info_no_inv, "name", "none", "none", exclude_no_group=False)

    # Case where NO_INVESTIGATION is 0
    job_info_inv_ok = {"group": "grp", "settings": {"NO_INVESTIGATION": "0"}}
    assert not openqa_investigate._should_exclude_job(job_info_inv_ok, "name", "none", "none", exclude_no_group=False)


def test_query_dependency_data_or_postpone(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Empty dep data
    mocker.patch.object(client, "get_dependencies_ajax", return_value={})
    assert openqa_investigate.query_dependency_data_or_postpone(client, 1) == {}

    # Pending nodes in cluster -> Exit(142)
    dep_data_pending = {
        "cluster": [[1, 2], [3, 4]],
        "nodes": [{"id": 2, "state": "running"}, {"id": 1, "state": "done"}, {"id": 3, "state": "running"}],
    }
    mocker.patch.object(client, "get_dependencies_ajax", return_value=dep_data_pending)
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.query_dependency_data_or_postpone(client, 1)
    assert exc.value.exit_code == 142

    # All cluster nodes done / cancelled
    dep_data_done = {
        "cluster": [[1, 2]],
        "nodes": [{"id": 1, "state": "done"}, {"id": 2, "state": "cancelled"}],
    }
    mocker.patch.object(client, "get_dependencies_ajax", return_value=dep_data_done)
    assert openqa_investigate.query_dependency_data_or_postpone(client, 1) == dep_data_done

    # Missing "cluster" key
    dep_no_cluster = {"nodes": [{"id": 1, "state": "done"}]}
    mocker.patch.object(client, "get_dependencies_ajax", return_value=dep_no_cluster)
    assert openqa_investigate.query_dependency_data_or_postpone(client, 1) == dep_no_cluster

    # Missing "nodes" key
    dep_no_nodes = {"cluster": [[1, 2]]}
    mocker.patch.object(client, "get_dependencies_ajax", return_value=dep_no_nodes)
    assert openqa_investigate.query_dependency_data_or_postpone(client, 1) == dep_no_nodes


@pytest.mark.parametrize(
    ("dep_data", "job_id", "expected"),
    [
        ({"cluster": {"cluster_1": [1, 2]}}, 1, {1, 2}),
        ({"cluster": [[1, 2], [3, 4]]}, 1, {1, 2}),
        ({"cluster": [[3, 4]]}, 1, {1}),
        ({"cluster": None}, 1, {1}),
        ({}, 1, {1}),
    ],
)
def test_get_cluster_jobs(dep_data: dict[str, Any], job_id: int, expected: set[int]) -> None:
    assert openqa_investigate.get_cluster_jobs(dep_data, job_id) == expected


def test_query_dependency_data_handles_dictionary_type_cluster(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    dep_dict_cluster = {
        "cluster": {"cluster_1": [1, 2]},
        "nodes": [{"id": 1, "state": "done"}, {"id": 2, "state": "done"}],
    }
    mocker.patch.object(client, "get_dependencies_ajax", return_value=dep_dict_cluster)
    assert openqa_investigate.query_dependency_data_or_postpone(client, 1) == dep_dict_cluster


def test_sync_via_investigation_comment(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Dry run -> None
    client.dry_run = True
    assert openqa_investigate.sync_via_investigation_comment(client, 1, 1) is None
    client.dry_run = False

    # post_job_comment returns no ID
    mocker.patch.object(client, "post_job_comment", return_value={})
    assert openqa_investigate.sync_via_investigation_comment(client, 1, 1) is None

    # No investigation comments in get_job_comments
    mocker.patch.object(client, "post_job_comment", return_value={"id": 100})
    mocker.patch.object(client, "get_job_comments", return_value=[{"text": "hello", "id": 50}])
    assert openqa_investigate.sync_via_investigation_comment(client, 1, 1) == "100"

    # Comment ID matches first comment ID
    mocker.patch.object(
        client, "get_job_comments", return_value=[{"text": "investigation", "id": 100}, {"text": "investigation"}]
    )
    assert openqa_investigate.sync_via_investigation_comment(client, 1, 1) == "100"

    # Comment ID does not match first comment ID -> deletes comment and raises Exit(0)
    mocker.patch.object(
        client,
        "get_job_comments",
        return_value=[{"text": "investigation", "id": 50}, {"text": "investigation", "id": 100}],
    )
    mock_del = mocker.patch.object(client, "delete_job_comment")
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.sync_via_investigation_comment(client, 1, 1)
    assert exc.value.exit_code == 0
    mock_del.assert_called_with(1, 100)

    # Comment ID does not match first comment ID, but force=True -> skips deletion, returns comment ID
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.sync_via_investigation_comment(client, 1, 1, force=False)
    assert exc.value.exit_code == 0

    assert openqa_investigate.sync_via_investigation_comment(client, 1, 1, force=True) == "100"


def test_finalize_investigation_comment(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    mock_del = mocker.patch.object(client, "delete_job_comment")
    mock_put = mocker.patch.object(client, "put_job_comment")
    mock_post = mocker.patch.object(client, "post_job_comment")

    # Test deletion
    openqa_investigate.finalize_investigation_comment(client, 1, 1, "123", "")
    mock_del.assert_called_once_with(1, "123")
    mock_put.assert_not_called()
    mock_post.assert_not_called()

    mock_del.reset_mock()

    # Test finalize on same job
    openqa_investigate.finalize_investigation_comment(client, 1, 1, "123", "out text")
    mock_del.assert_not_called()
    mock_put.assert_called_once()
    mock_post.assert_not_called()
    assert "Automatic investigation jobs for job 1:\n\nout text" in mock_put.call_args[0][2]

    mock_put.reset_mock()

    # Test finalize on different job
    openqa_investigate.finalize_investigation_comment(client, 2, 1, "123", "out text")
    mock_del.assert_not_called()
    mock_put.assert_called_once()
    mock_post.assert_called_once()


def test_find_investigation_comment() -> None:
    comments = [
        {"text": "Unrelated"},
        {"text": "Automatic investigation jobs for job 1 without retry"},
        {"text": "Automatic investigation jobs for job 1 :investigate:retry*:", "id": "100"},
        {"text": "Automatic investigation jobs for job 1 :investigate:retry*:", "id": "50"},
        {"text": "Automatic investigation jobs for job 1 :investigate:retry*:", "id": "200"},
        {"text": "Automatic investigation jobs for job 1 :investigate:retry*:"},  # missing id
    ]
    target = openqa_investigate._find_investigation_comment(comments)
    assert target is not None
    assert target.get("id") == "50"

    assert openqa_investigate._find_investigation_comment([]) is None


def test_fetch_investigation_results(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # dry_run -> []
    client.dry_run = True
    assert openqa_investigate.fetch_investigation_results(client, 1) == []
    client.dry_run = False

    # No target comment -> []
    mocker.patch.object(openqa_investigate, "_find_investigation_comment", return_value=None)
    mocker.patch.object(client, "get_job_comments", return_value=[])
    assert openqa_investigate.fetch_investigation_results(client, 1) == []

    # Comment with sub-job not finished -> Exit(142)
    comment = {
        "text": (
            "* *test:investigate:last_good_tests*: t#101\n* *test:investigate:retry*: t#102\nOther non-matching line"
        ),
    }
    mocker.patch.object(openqa_investigate, "_find_investigation_comment", return_value=comment)
    mocker.patch.object(client, "get_job_status", side_effect=[{"state": "scheduled"}])
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.fetch_investigation_results(client, 1)
    assert exc.value.exit_code == 142

    # Comment with finished sub-jobs
    mocker.patch.object(
        client,
        "get_job_status",
        side_effect=[
            {"state": "done", "result": "passed"},
            {"state": "cancelled", "result": "user_cancelled"},
        ],
    )
    results = openqa_investigate.fetch_investigation_results(client, 1)
    assert results == [("last_good_tests", 101, "passed"), ("retry", 102, "user_cancelled")]


def test_identify_issue_type() -> None:
    # Cancelled result in set
    cancelled_results = [("last_good_tests", 101, "skipped")]
    assert openqa_investigate.identify_issue_type(cancelled_results) == (False, False, False, True)

    # Product issue: pass_lgtb not False, pass_lgt not True, pass_lgb is True
    prod_results = [
        ("last_good_tests_and_build", 100, "passed"),
        ("last_good_tests", 101, "failed"),
        ("last_good_build", 102, "passed"),
    ]
    assert openqa_investigate.identify_issue_type(prod_results) == (True, False, False, False)

    # Test issue: pass_lgtb not False, pass_lgt is True, pass_lgb not True
    test_results = [
        ("last_good_tests_and_build", 100, "softfailed"),
        ("last_good_tests", 101, "passed"),
        ("last_good_build", 102, "failed"),
    ]
    assert openqa_investigate.identify_issue_type(test_results) == (False, True, False, False)

    # Infra issue: pass_lgtb is False, pass_lgt is False, pass_lgb is False
    infra_results = [
        ("last_good_tests_and_build", 100, "failed"),
        ("last_good_tests", 101, "failed"),
        ("last_good_build", 102, "failed"),
    ]
    assert openqa_investigate.identify_issue_type(infra_results) == (False, False, True, False)

    # Inconclusive (neither prod, test, nor infra)
    other_results = [
        ("last_good_tests_and_build", 100, "failed"),
        ("last_good_tests", 101, "passed"),
        ("last_good_build", 102, "passed"),
        ("other_type", 103, "passed"),
    ]
    assert openqa_investigate.identify_issue_type(other_results) == (False, False, False, False)


def test_verify_retry_job(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Empty origin_url
    assert openqa_investigate._verify_retry_job(client, "retry", 1, "") == (0, False)

    # Non-matching origin_url
    assert openqa_investigate._verify_retry_job(client, "retry", 1, "https://openqa.opensuse.org/invalid") == (0, False)

    # Origin job not found (HTTP 404)
    mocker.patch.object(client, "get_job", return_value={"error_status": 404})
    assert openqa_investigate._verify_retry_job(client, "retry", 1, "https://openqa.opensuse.org/t50") == (0, False)

    # Retry name does not match expected pattern
    mocker.patch.object(client, "get_job", return_value={"job": {"test": "origin_test"}})
    assert openqa_investigate._verify_retry_job(client, "wrong_name", 1, "https://openqa.opensuse.org/t50") == (
        0,
        False,
    )

    # Successful match
    assert openqa_investigate._verify_retry_job(
        client, "origin_test:investigate:retry", 1, "https://openqa.opensuse.org/t50"
    ) == (50, True)


def test_build_investigation_comment(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Passed with force status
    c1 = openqa_investigate._build_investigation_comment(
        client, 1, "test:investigate:retry", "passed", {"_FORCE_STATUS_ON_RETRY": "1"}, 50
    )
    assert "Likely a sporadic failure" in c1
    assert "label:force_result:passed:retry_job_passed" in c1

    # Passed without force status
    c2 = openqa_investigate._build_investigation_comment(
        client, 1, "test:investigate:retry", "softfailed", {"_FORCE_STATUS_ON_RETRY": "0"}, 50
    )
    assert "Likely a sporadic failure" in c2
    assert "label:force_result" not in c2

    # Failed and cancelled
    mocker.patch.object(openqa_investigate, "fetch_investigation_results", return_value=[])
    mocker.patch.object(openqa_investigate, "identify_issue_type", return_value=(False, False, False, True))
    assert "cancelled" in openqa_investigate._build_investigation_comment(
        client, 1, "test:investigate:retry", "failed", {}, 50
    )

    # Product issue
    mocker.patch.object(openqa_investigate, "identify_issue_type", return_value=(True, False, False, False))
    assert "likely a product issue" in openqa_investigate._build_investigation_comment(
        client, 1, "test:investigate:retry", "failed", {}, 50
    )

    # Test issue
    mocker.patch.object(openqa_investigate, "identify_issue_type", return_value=(False, True, False, False))
    assert "likely a test issue" in openqa_investigate._build_investigation_comment(
        client, 1, "test:investigate:retry", "failed", {}, 50
    )

    # Infra issue
    mocker.patch.object(openqa_investigate, "identify_issue_type", return_value=(False, False, True, False))
    assert "infrastructure" in openqa_investigate._build_investigation_comment(
        client, 1, "test:investigate:retry", "failed", {}, 50
    )

    # Inconclusive
    mocker.patch.object(openqa_investigate, "identify_issue_type", return_value=(False, False, False, False))
    assert "Likely not a sporadic failure" in openqa_investigate._build_investigation_comment(
        client, 1, "test:investigate:retry", "failed", {}, 50
    )


def test_post_investigate(mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Name does not end with investigate:retry
    with caplog.at_level(logging.INFO):
        openqa_investigate.post_investigate(client, 1, "not_retry", {})
    assert "already, skipping investigation" in caplog.text

    # _verify_retry_job returns False
    mocker.patch.object(openqa_investigate, "_verify_retry_job", return_value=(0, False))
    openqa_investigate.post_investigate(client, 1, "test:investigate:retry", {})

    # Success posting comment (and 404 response handled gracefully)
    mocker.patch.object(openqa_investigate, "_verify_retry_job", return_value=(50, True))
    mocker.patch.object(openqa_investigate, "_build_investigation_comment", return_value="comment text")
    mocker.patch.object(client, "post_job_comment", return_value={"error_status": 404})
    openqa_investigate.post_investigate(client, 1, "test:investigate:retry", {})

    # Normal successful comment posting (does not raise)
    mocker.patch.object(client, "post_job_comment", return_value={"id": 123})
    openqa_investigate.post_investigate(client, 1, "test:investigate:retry", {})

    # Unexpected error response -> Exit(2)
    mocker.patch.object(client, "post_job_comment", return_value={"error": "Something exploded"})
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.post_investigate(client, 1, "test:investigate:retry", {})
    assert exc.value.exit_code == 2


def test_run_investigation_logic(mocker: MockerFixture) -> None:
    client = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")

    # Unable to query job data -> Exit(1)
    mocker.patch.object(client, "get_job", return_value={})
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.run_investigation_logic(client, 1)
    assert exc.value.exit_code == 1

    # Job is an investigation job -> calls post_investigate
    job_inv = {"job": {"test": "foo:investigate:retry"}}
    mocker.patch.object(client, "get_job", return_value=job_inv)
    mock_post_inv = mocker.patch.object(openqa_investigate, "post_investigate")
    openqa_investigate.run_investigation_logic(client, 1)
    mock_post_inv.assert_called_once()

    # Job already has clone and not force -> returns
    job_cloned = {"job": {"test": "foo", "clone_id": 999}}
    mocker.patch.object(client, "get_job", return_value=job_cloned)
    openqa_investigate.run_investigation_logic(client, 1, force=False)

    # Job excluded by filter -> returns
    mocker.patch.object(openqa_investigate, "query_dependency_data_or_postpone", return_value={})
    mocker.patch.object(openqa_investigate, "_should_exclude_job", return_value=True)
    openqa_investigate.run_investigation_logic(client, 1, force=True)

    # Success path with cluster, comment sync, trigger_jobs, finalize
    mocker.patch.object(client, "get_job", return_value={"job": {"test": "foo"}})
    mocker.patch.object(openqa_investigate, "_should_exclude_job", return_value=False)
    mocker.patch.object(
        openqa_investigate, "query_dependency_data_or_postpone", return_value={"cluster": [[1, 2], [5]]}
    )
    mocker.patch.object(openqa_investigate, "sync_via_investigation_comment", return_value="123")
    mocker.patch.object(openqa_investigate, "trigger_jobs", return_value="out text")
    mock_fin = mocker.patch.object(openqa_investigate, "finalize_investigation_comment")
    openqa_investigate.run_investigation_logic(client, 2)
    mock_fin.assert_called_once_with(client, 2, 1, "123", "out text")

    # trigger_jobs raises typer.Exit
    mocker.patch.object(openqa_investigate, "trigger_jobs", side_effect=typer.Exit(code=5))
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.run_investigation_logic(client, 2)
    assert exc.value.exit_code == 5

    # trigger_jobs raises known ignorable error
    mock_put = mocker.patch.object(client, "put_job_comment")
    known_err = Exception("Job will fail because repositories are unavailable")
    mocker.patch.object(openqa_investigate, "trigger_jobs", side_effect=known_err)
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.run_investigation_logic(client, 2)
    assert exc.value.exit_code == 0
    mock_put.assert_called_once()

    # trigger_jobs raises unknown error without comment_id_str
    mocker.patch.object(openqa_investigate, "sync_via_investigation_comment", return_value=None)
    mocker.patch.object(openqa_investigate, "trigger_jobs", side_effect=Exception("Unknown error"))
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.run_investigation_logic(client, 2)
    assert exc.value.exit_code == 1

    # Success path with comment_id_str=None
    client_quiet = openqa_investigate.OpenQAClient("https://openqa.opensuse.org")
    mocker.patch.object(client_quiet, "get_job", return_value={"job": {"test": "foo"}})
    mocker.patch.object(openqa_investigate, "sync_via_investigation_comment", return_value=None)
    mocker.patch.object(openqa_investigate, "trigger_jobs", return_value="out text")
    mock_fin.reset_mock()
    openqa_investigate.run_investigation_logic(client_quiet, 2)
    mock_fin.assert_not_called()


def test_main(mocker: MockerFixture) -> None:
    # Invalid job_id / URL -> Exit(1)
    with pytest.raises(typer.Exit) as exc:
        openqa_investigate.main("invalid_job_id")
    assert exc.value.exit_code == 1

    # Valid invocation
    mock_run = mocker.patch.object(openqa_investigate, "run_investigation_logic")
    openqa_investigate.main(
        "1234",
        extra_settings=["FOO=BAR"],
        host="test.openqa",
        scheme="http",
        investigation_gid=5,
        dry_run=True,
        verbose=1,
        prio_add=20,
        exclude_name_regex="excl",
        exclude_no_group=False,
        exclude_group_regex="group_excl",
        force=True,
        retries=5,
        retry_sleep_time=10,
    )
    mock_run.assert_called_once()
    client_arg = mock_run.call_args[0][0]
    assert client_arg.host_url == "http://test.openqa"
    assert client_arg.retries == 5
    assert client_arg.retry_sleep_time == 10
    assert client_arg.dry_run is True
    assert client_arg.http_timeout == pytest.approx(90.0)
    assert client_arg.investigation_timeout == pytest.approx(120.0)
    assert mock_run.call_args[0][1] == 1234
    kwargs = mock_run.call_args[1]
    assert kwargs["extra_settings"] == ["FOO=BAR"]
    assert kwargs["investigation_gid"] == 5
    assert kwargs["prio_add"] == 20
    assert kwargs["exclude_name_regex"] == "excl"
    assert kwargs["exclude_no_group"] is False
    assert kwargs["exclude_group_regex"] == "group_excl"
    assert kwargs["force"] is True

    # Custom timeouts passed to main
    mock_run.reset_mock()
    openqa_investigate.main("1234", http_timeout=45.0, investigation_timeout=60.0)
    client_custom = mock_run.call_args[0][0]
    assert client_custom.http_timeout == pytest.approx(45.0)
    assert client_custom.investigation_timeout == pytest.approx(60.0)


def test_main_entrypoint(mocker: MockerFixture) -> None:
    mocker.patch.object(openqa_investigate.typer.Typer, "__call__")
    main_spec = importlib.util.spec_from_file_location(
        "__main__",
        path,
        loader=importlib.machinery.SourceFileLoader("__main__", str(path)),
    )
    assert main_spec
    assert main_spec.loader
    main_mod = importlib.util.module_from_spec(main_spec)
    main_spec.loader.exec_module(main_mod)
