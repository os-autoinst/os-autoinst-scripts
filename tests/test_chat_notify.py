# Copyright SUSE LLC
from typing import NoReturn
from unittest.mock import MagicMock, patch

import httpx
from typer.testing import CliRunner

from os_autoinst_scripts.chat_notify import app

runner = CliRunner()


@patch("httpx.post")
def test_send_message_success(mock_post: MagicMock) -> None:
    # Arrange
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"event_id": "$12345"},
        raise_for_status=lambda: None,
    )
    server_url = "example.com"
    message_body = "Hello from test!"
    access_token = "test_token"  # noqa: S105
    room_id = "test_room"

    # Act
    result = runner.invoke(app, [server_url, message_body, access_token, room_id])

    # Assert
    assert result.exit_code == 0
    assert "[+] Message sent!" in result.stdout
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0] == (
        f"https://{server_url}/_matrix/client/r0/rooms/{room_id}/send/m.room.message"
    )
    assert mock_post.call_args.kwargs["headers"] == {"Authorization": f"Bearer {access_token}"}
    assert mock_post.call_args.kwargs["json"] == {
        "msgtype": "m.text",
        "body": message_body,
        "formatted_body": message_body,
        "format": "org.matrix.custom.html",
    }


@patch("httpx.post")
def test_send_message_matrix_error(mock_post: MagicMock) -> None:
    # Arrange
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"errcode": "M_UNKNOWN", "error": "Unknown error"},
        raise_for_status=lambda: None,
    )
    server_url = "example.com"
    message_body = "Hello from test!"
    access_token = "test_token"  # noqa: S105
    room_id = "test_room"

    # Act
    result = runner.invoke(app, [server_url, message_body, access_token, room_id])

    # Assert
    assert result.exit_code == 1
    assert "[!] Something went wrong sending the message" in result.stderr
    assert "Unknown error" in result.stderr
    mock_post.assert_called_once()


@patch("httpx.post")
def test_send_message_http_error(mock_post: MagicMock) -> None:
    # Arrange
    def raise_http_error() -> NoReturn:
        response = httpx.Response(404, request=httpx.Request("POST", "http://example.com"))
        response.text = "Not Found"
        msg = "Not Found"
        raise httpx.HTTPStatusError(msg, request=response.request, response=response)

    mock_post.side_effect = raise_http_error
    server_url = "example.com"
    message_body = "Hello from test!"
    access_token = "test_token"  # noqa: S105
    room_id = "test_room"

    # Act
    result = runner.invoke(app, [server_url, message_body, access_token, room_id])

    # Assert
    assert result.exit_code == 1
    mock_post.assert_called_once()


@patch("httpx.post")
def test_send_message_network_error(mock_post: MagicMock) -> None:
    # Arrange
    mock_post.side_effect = httpx.RequestError(
        "Network unreachable", request=httpx.Request("POST", "http://example.com")
    )
    server_url = "example.com"
    message_body = "Hello from test!"
    access_token = "test_token"  # noqa: S105
    room_id = "test_room"

    # Act
    result = runner.invoke(app, [server_url, message_body, access_token, room_id])

    # Assert
    assert result.exit_code == 1
    assert "[!] Network error sending message:" in result.stderr
    assert "Network unreachable" in result.stderr
    mock_post.assert_called_once()
