# Copyright SUSE LLC
"""Fetching job data from an openQA server."""

from __future__ import annotations

import json
from typing import Any

import httpx

JsonDict = dict[str, Any]


class AnalyzerError(Exception):
    """Fetching or parsing a job's data failed; `partial_output` holds the text produced so far."""

    def __init__(self, message: str, partial_output: str = "") -> None:
        """Store the message and any output produced before the failure."""
        super().__init__(message)
        self.partial_output = partial_output


class NoFailedModulesError(AnalyzerError):
    """The job has no failed (or, when requested, softfailed) modules."""


class OpenQA:
    """Fetches job data from one openQA server through an `httpx.Client`."""

    def __init__(self, client: httpx.Client, server: str) -> None:
        """Bind the client to a server base URL."""
        self.client = client
        self.server = server.rstrip("/")

    def text(self, path: str) -> str:
        """Return the body of `path`; raise `AnalyzerError` on failure."""
        url = f"{self.server}{path}"
        try:
            response = self.client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            msg = f"Error fetching {url}: {e}"
            raise AnalyzerError(msg) from e
        return response.text

    def json(self, path: str) -> Any:  # ruff: ignore[any-type]
        """Return the parsed JSON of `path`; raise `AnalyzerError` on failure."""
        try:
            return json.loads(self.text(path))
        except ValueError as e:
            msg = f"Error parsing JSON from {self.server}{path}: {e}"
            raise AnalyzerError(msg) from e

    def text_or_empty(self, path: str) -> str:
        """Return the body of `path`, or "" when it cannot be fetched (e.g. a 404 page)."""
        try:
            return self.text(path)
        except AnalyzerError:
            return ""
