#!/usr/bin/env python3
# Copyright SUSE LLC
"""Inject benchmark request parameters into chat-completion traffic.

`openqa-llm-investigate` sends `model`, `messages` and `temperature` only, so a model whose
served chat template reads `reasoning_effort`, `top_p`, `top_k`, `min_p` or `num_ctx` runs at
its own defaults and those axes cannot be measured without changing the tool. The proxy
merges the injected parameters into the request body, forwards to the benchmark endpoint,
relays the upstream response bytes unmodified so the eval runner keeps parsing the verdict
header, and logs one line per request with the model, the token counts and the wall time.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("params_proxy")

INJECTABLE_KEYS = ("reasoning_effort", "top_p", "top_k", "min_p", "num_ctx")
INT_KEYS = ("top_k", "num_ctx")
FLOAT_KEYS = ("top_p", "min_p")
DEFAULT_LISTEN = "127.0.0.1:8089"
REQUEST_TIMEOUT = 600


def parse_params(pairs: list[str]) -> dict[str, str | int | float]:
    """Return typed values from `key=value` strings, restricted to the injectable keys."""
    params: dict[str, str | int | float] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            message = f"--params needs key=value, got {pair}"
            raise ValueError(message)
        if key not in INJECTABLE_KEYS:
            message = f"--params key {key} is not injectable"
            raise ValueError(message)
        target = int if key in INT_KEYS else float if key in FLOAT_KEYS else str
        try:
            params[key] = target(value)
        except ValueError:
            message = f"--params {key} needs a number, got {value}"
            raise ValueError(message) from None
    return params


def merge_params(body: dict[str, Any], params: dict[str, str | int | float]) -> dict[str, Any]:
    """Return `body` with the injected parameters applied; every other field is untouched."""
    return {**body, **params}


def forward(upstream: str, body: dict[str, Any], timeout: int = REQUEST_TIMEOUT) -> tuple[int, bytes]:
    """POST the merged `body` to `upstream` and return its status and raw bytes.

    An upstream error status is returned as a response, not raised, so the client sees the
    same status the benchmark server produced. The upstream URL is operator-supplied
    through `--upstream`, not untrusted input.
    """
    # The upstream URL is operator-supplied (--upstream), not untrusted input.
    request = urllib.request.Request(  # ruff: ignore[suspicious-url-open-usage]
        upstream,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # ruff: ignore[suspicious-url-open-usage]
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except urllib.error.URLError as err:
        # A refused or unreachable upstream is a response, not a dropped connection, so the
        # runner's retry and timeout accounting stay intact.
        return 502, json.dumps({"error": f"upstream unreachable: {err.reason}"}).encode()


def usage_of(payload: bytes) -> dict[str, Any]:
    """Return the usage counters of a response body; a non-JSON body yields none."""
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    usage = decoded.get("usage") if isinstance(decoded, dict) else None
    return usage if isinstance(usage, dict) else {}


def log_request(
    model: str | float | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    wall_sec: float,
    status: int,
) -> None:
    """Log one served request as model, status, token counts and wall time."""
    logger.info(
        "model=%s status=%s prompt_tokens=%s completion_tokens=%s wall_sec=%.2f",
        model,
        status,
        prompt_tokens,
        completion_tokens,
        wall_sec,
    )


class ProxyServer(ThreadingHTTPServer):
    """The proxy endpoint, carrying the upstream URL and the injected parameters."""

    def __init__(self, address: tuple[str, int], upstream: str, params: dict[str, str | int | float]) -> None:
        super().__init__(address, ProxyHandler)
        self.upstream = upstream
        self.params = params


class ProxyHandler(BaseHTTPRequestHandler):
    """Serve one chat-completion request with the injected parameters applied."""

    server: ProxyServer

    def do_POST(self) -> None:
        """Merge the injected parameters, forward, and relay the response bytes unchanged."""
        length = int(self.headers.get("Content-Length", 0))
        merged = merge_params(json.loads(self.rfile.read(length) or b"{}"), self.server.params)
        start = time.monotonic()
        status, payload = forward(self.server.upstream, merged, REQUEST_TIMEOUT)
        usage = usage_of(payload)
        log_request(
            merged.get("model"),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            time.monotonic() - start,
            status,
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # ruff: ignore[builtin-argument-shadowing, no-self-use] - the stdlib calls this hook
        """Route the built-in access log to the same stdout stream as the request log."""
        logger.info("%s", format % args)


def split_listen(value: str) -> tuple[str, int]:
    """Return host and port from a `host:port` string."""
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        message = f"--listen needs host:port, got {value}"
        raise ValueError(message)
    return host, int(port)


def main(argv: list[str] | None = None) -> int:
    """Serve the proxy until shutdown."""
    parser = argparse.ArgumentParser(
        description="Proxy chat-completion requests with injected benchmark parameters.",
    )
    parser.add_argument("--listen", default=DEFAULT_LISTEN, help="host:port to accept requests on")
    parser.add_argument("--upstream", required=True, help="chat-completions endpoint to forward to")
    parser.add_argument(
        "--params",
        action="append",
        default=[],
        help="key=value merged into every request body, repeatable",
    )
    args = parser.parse_args(argv)
    params = parse_params(args.params)
    host, port = split_listen(args.listen)
    server = ProxyServer((host, port), args.upstream, params)
    logger.info("listening on %s:%d, upstream %s, params %s", host, port, args.upstream, params)
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
