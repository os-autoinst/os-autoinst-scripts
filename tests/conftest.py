# Copyright SUSE LLC
"""Shared fixtures for the openqa_llm_investigate tests."""

from __future__ import annotations

import fnmatch
import json
import pathlib
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from openqa_llm_investigate.http import OpenQA

Route = str | Callable[[httpx.Request], str]

DATA_DIR = pathlib.Path(__file__).parent / "data" / "analysis"


def _load(name: str) -> Any:
    path = DATA_DIR / name
    return json.loads(path.read_text()) if name.endswith(".json") else path.read_text()


@pytest.fixture
def sample_details_failed() -> Any:
    return _load("details_ajax_failed.json")


@pytest.fixture
def sample_details_softfailed() -> Any:
    return _load("details_ajax_softfailed.json")


@pytest.fixture
def sample_details_no_modules() -> Any:
    return _load("details_ajax_no_modules.json")


@pytest.fixture
def sample_serial_log() -> str:
    return _load("serial_terminal.txt")


@pytest.fixture
def sample_serial0_panic() -> str:
    return _load("serial0_kernel_panic.txt")


@pytest.fixture
def sample_autoinst_log() -> str:
    return _load("autoinst_log_sample.txt")


@pytest.fixture
def make_openqa() -> Callable[[Mapping[str, Route]], OpenQA]:
    """Build an `OpenQA` serving `routes` by URL path glob (a callable gets the request); other paths are 404."""

    def make(routes: Mapping[str, Route]) -> OpenQA:
        def handler(request: httpx.Request) -> httpx.Response:
            route = next((r for pattern, r in routes.items() if fnmatch.fnmatch(request.url.path, pattern)), None)
            if route is None:
                return httpx.Response(404, text="<html>not found</html>")
            return httpx.Response(200, text=route if isinstance(route, str) else route(request))

        return OpenQA(httpx.Client(transport=httpx.MockTransport(handler)), "https://srv")

    return make
