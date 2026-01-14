# Copyright SUSE LLC
# ruff: noqa: ERA001
from unittest.mock import MagicMock

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from os_autoinst_scripts.reboot_stability_check import app

runner = CliRunner()


def test_check_one_host_success(mocker: MockerFixture) -> None:
    mock_ping = mocker.patch("os_autoinst_scripts.reboot_stability_check.ping")
    mock_nc = mocker.patch("os_autoinst_scripts.reboot_stability_check.nc")
    mock_ssh = mocker.patch("os_autoinst_scripts.reboot_stability_check.ssh")
    mock_sleep = mocker.patch("time.sleep")
    mock_ping.return_value = MagicMock()
    mock_nc.return_value = MagicMock()
    mock_ssh.return_value = MagicMock()

    result = runner.invoke(app, ["somehost", "--runs", "1"])

    assert result.exit_code == 0
    assert "run: 1, somehost: ping .. ok, ssh .. ok, uptime/reboot: " in result.stdout
    assert mock_ping.call_count > 0
    assert mock_nc.call_count > 0
    assert mock_ssh.call_count > 0
    assert mock_sleep.call_count > 0


# def test_check_one_host_ping_fails(mocker: MockerFixture) -> None:
#    mocker.patch("os_autoinst_scripts.reboot_stability_check.ping", side_effect=Exception("ping failed"))
#    mocker.patch("os_autoinst_scripts.reboot_stability_check.nc")
#    mocker.patch("os_autoinst_scripts.reboot_stability_check.ssh")
#    mocker.patch("time.sleep")
#    # wip stuck
#    # Act
#    result = runner.invoke(app, ["somehost", "--runs", "1"])
#
#    # Assert
#    assert result.exit_code == 1
#    assert "Error during ping: " in result.stdout
#    assert "Host somehost failed on run 1. Exiting." in result.stdout


# @patch("os_autoinst_scripts.reboot_stability_check.ping")
# @patch("os_autoinst_scripts.reboot_stability_check.nc", side_effect=Exception("nc failed"))
# @patch("os_autoinst_scripts.reboot_stability_check.ssh")
# def test_check_one_host_ssh_nc_fails(mock_nc: MagicMock, mock_ping: MagicMock) -> None:
#     # Act
#     result = runner.invoke(app, ["somehost", "--runs", "1"])
#
#     # Assert
#     assert result.exit_code == 1
#     assert "Error during nc: " in result.stdout
#     assert "Host somehost failed on run 1. Exiting." in result.stdout
#
#
# @patch("os_autoinst_scripts.reboot_stability_check.ping")
# @patch("os_autoinst_scripts.reboot_stability_check.nc")
# @patch("os_autoinst_scripts.reboot_stability_check.ssh", side_effect=Exception("ssh failed"))
# def test_check_one_host_ssh_reboot_fails(
#     mock_ssh: MagicMock, mock_nc: MagicMock, mock_ping: MagicMock
# ) -> None:
#     # Act
#     result = runner.invoke(app, ["somehost", "--runs", "1"])
#
#     # Assert
#     assert result.exit_code == 1
#     assert "Error during ssh: " in result.stdout
#     assert "Host somehost failed on run 1. Exiting." in result.stdout
