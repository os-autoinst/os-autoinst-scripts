# Copyright SUSE LLC
import httpx
import typer

app = typer.Typer()


def send_message(server_url: str, message_body: str, access_token: str, room_id: str) -> None:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://{server_url}/_matrix/client/r0/rooms/{room_id}/send/m.room.message"
    json = {
        "msgtype": "m.text",
        "body": message_body,
        "formatted_body": message_body,
        "format": "org.matrix.custom.html",
    }
    try:
        response = httpx.post(url, headers=headers, json=json)
        response.raise_for_status()
        response_json = response.json()
        if "errcode" in response_json:
            typer.secho(
                f"[!] Something went wrong sending the message: {response_json.get('error', 'Unknown error')}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        typer.secho("[+] Message sent!", fg=typer.colors.GREEN)
    except httpx.HTTPStatusError as e:
        typer.secho(f"[!] HTTP error sending message: {e}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from e
    except httpx.RequestError as e:
        typer.secho(f"[!] Network error sending message: {e}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from e


@app.command()
def main(
    server_url: str,
    message_body: str,
    access_token: str,
    room_id: str,
) -> None:
    send_message(server_url, message_body, access_token, room_id)


if __name__ == "__main__":
    app()
