# Copyright SUSE LLC
# ruff: noqa: S404, TRY002, TRY003, EM101, FBT001, ARG005, RUF059
"""Unit tests for openqa-update-cert-from-obs."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
import typer

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# Load the script dynamically as a module
rootpath = pathlib.Path(__file__).parent.parent.resolve()
path = rootpath / "openqa-update-cert-from-obs"
spec = importlib.util.spec_from_file_location(
    "update_cert",
    path,
    loader=importlib.machinery.SourceFileLoader("update_cert", str(path)),
)
assert spec is not None
assert spec.loader is not None
update_cert = importlib.util.module_from_spec(spec)
sys.modules["update_cert"] = update_cert
spec.loader.exec_module(update_cert)


def test_parse_config_success(tmp_path: pathlib.Path) -> None:
    config_file = tmp_path / "test.conf"
    config_file.write_text(
        "# This is a comment\n"
        "\n"
        "OBS_PROJECT=openSUSE:Factory:Staging\n"
        "CRT_NAME = 'openSUSE-Factory-Staging.crt'\n"
        'OBS_API_URL = "https://api.opensuse.org"\n'
        "INVALID_LINE\n"
    )
    res = update_cert.parse_config(config_file)
    assert res == {
        "OBS_PROJECT": "openSUSE:Factory:Staging",
        "CRT_NAME": "openSUSE-Factory-Staging.crt",
        "OBS_API_URL": "https://api.opensuse.org",
    }


def test_parse_config_error(tmp_path: pathlib.Path) -> None:
    missing_file = tmp_path / "does_not_exist.conf"
    with pytest.raises(typer.Exit) as exc:
        update_cert.parse_config(missing_file)
    assert exc.value.exit_code == 2


def unlink_and_raise_called_process_error(tmp_path: pathlib.Path) -> None:
    tmp_file = tmp_path / "var_lib" / "openqa" / "share" / "factory" / "other" / "fixed" / "cert.crt.tmp"
    tmp_file.unlink()
    raise subprocess.CalledProcessError(5, "osc", stderr="error")


def unlink_and_raise_exception(tmp_path: pathlib.Path) -> None:
    tmp_file = tmp_path / "var_lib" / "openqa" / "share" / "factory" / "other" / "fixed" / "cert.crt.tmp"
    tmp_file.unlink()
    raise Exception("generic error")


@pytest.mark.parametrize(
    ("config_content", "mkdir_raises", "sub_raises", "replace_raises", "expected_exit"),
    [
        (
            {"OBS_PROJECT": "proj"},
            False,
            None,
            False,
            1,  # CRT_NAME missing
        ),
        (
            {"CRT_NAME": "cert.crt"},
            False,
            None,
            False,
            1,  # OBS_PROJECT missing
        ),
        (
            {"OBS_PROJECT": "proj", "CRT_NAME": "cert.crt"},
            True,  # mkdir raises OSError
            None,
            False,
            1,
        ),
        (
            {"OBS_PROJECT": "proj", "CRT_NAME": "cert.crt"},
            False,
            subprocess.CalledProcessError(5, "osc", stderr="some error"),
            False,
            5,  # CalledProcessError exit code
        ),
        (
            {"OBS_PROJECT": "proj", "CRT_NAME": "cert.crt"},
            False,
            subprocess.CalledProcessError(5, "osc", stderr=None),
            False,
            5,  # CalledProcessError with stderr=None
        ),
        (
            {"OBS_PROJECT": "proj", "CRT_NAME": "cert.crt"},
            False,
            unlink_and_raise_called_process_error,
            False,
            5,  # CalledProcessError with file already unlinked
        ),
        (
            {"OBS_PROJECT": "proj", "CRT_NAME": "cert.crt"},
            False,
            Exception("generic subprocess exception"),
            False,
            1,  # Generic exception
        ),
        (
            {"OBS_PROJECT": "proj", "CRT_NAME": "cert.crt"},
            False,
            unlink_and_raise_exception,
            False,
            1,  # Generic exception with file already unlinked
        ),
        (
            {"OBS_PROJECT": "proj", "CRT_NAME": "cert.crt"},
            False,
            None,
            True,  # replace raises OSError
            1,
        ),
        (
            {"OBS_PROJECT": "proj", "CRT_NAME": "cert.crt", "OBS_API_URL": "https://api.custom.org"},
            False,
            None,
            False,
            0,  # success path
        ),
    ],
)
def test_main_flow(
    mocker: MockerFixture,
    tmp_path: pathlib.Path,
    config_content: dict[str, str],
    mkdir_raises: bool,
    sub_raises: Any,
    replace_raises: bool,
    expected_exit: int,
) -> None:
    config_file = tmp_path / "test.conf"
    config_lines = [f"{k}={v}" for k, v in config_content.items()]
    config_file.write_text("\n".join(config_lines))

    # Mock parse_config to return config_content
    mocker.patch("update_cert.parse_config", return_value=config_content)

    if mkdir_raises:
        mock_mkdir = mocker.patch("pathlib.Path.mkdir")
        mock_mkdir.side_effect = OSError("Permission denied")

    if replace_raises:
        mock_replace = mocker.patch("pathlib.Path.replace")
        mock_replace.side_effect = OSError("Replace failed")

    mock_run = mocker.patch("subprocess.run")
    if sub_raises:
        if callable(sub_raises):
            mock_run.side_effect = lambda *args, **kwargs: sub_raises(tmp_path)
        else:
            mock_run.side_effect = sub_raises

    openqa_basedir = tmp_path / "var_lib"

    if expected_exit == 0:
        update_cert.main(config_file, openqa_basedir=openqa_basedir)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd == [
            "osc",
            "--apiurl",
            config_content.get("OBS_API_URL", "https://api.opensuse.org"),
            "signkey",
            "--sslcert",
            config_content["OBS_PROJECT"],
        ]
    else:
        with pytest.raises(typer.Exit) as exc:
            update_cert.main(config_file, openqa_basedir=openqa_basedir)
        assert exc.value.exit_code == expected_exit
