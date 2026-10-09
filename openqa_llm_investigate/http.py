# Copyright SUSE LLC
"""Fetching job data from an openQA server."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import Any

import httpx

from .text import redact

log = logging.getLogger(__name__)

JsonDict = dict[str, Any]

# ponytail: head+tail byte cap per job file; parse incrementally if signals hide in the middle
JOB_FILE_MAX_BYTES = 8 * 1024 * 1024


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
        self._cache: dict[str, str] = {}

    def text(self, path: str) -> str:
        """Return the body of `path`, fetched once per `OpenQA`; raise `AnalyzerError` on failure."""
        if path not in self._cache:
            url = f"{self.server}{path}"
            try:
                response = self.client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as e:
                msg = f"Error fetching {url}: {e}"
                raise AnalyzerError(msg) from e
            self._cache[path] = response.text
        return self._cache[path]

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
        except AnalyzerError as e:
            self._log_fetch_failure(path, e.__cause__ or e)
            return ""

    def _log_fetch_failure(self, path: str, error: BaseException) -> None:
        level = (
            logging.DEBUG
            if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == HTTPStatus.NOT_FOUND
            else logging.WARNING
        )
        log.log(level, "Optional fetch failed: %s", redact(f"{self.server}{path}: {error}"))

    def _capped(self, path: str, max_bytes: int) -> str:
        """Stream `path`, keeping only the first and last `max_bytes // 2` bytes of larger bodies."""
        half = max_bytes // 2
        head, tail, total = bytearray(), bytearray(), 0
        with self.client.stream("GET", f"{self.server}{path}") as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                total += len(chunk)
                room = half - len(head)
                head += chunk[:room]
                tail += chunk[room:]
                del tail[:-half]
        if total <= max_bytes:
            return (head + tail).decode(errors="replace")
        omitted = total - len(head) - len(tail)
        head_lines = head.decode(errors="replace").rsplit("\n", 1)[0]
        tail_lines = tail.decode(errors="replace").split("\n", 1)[-1]
        return f"{head_lines}\n[... {omitted} bytes omitted from the middle of this file ...]\n{tail_lines}"

    def job_file(self, job_id: str, name: str, max_bytes: int = JOB_FILE_MAX_BYTES) -> str:
        """Return a job's result/uploaded file, fetched once and capped to `max_bytes` ("" when unavailable)."""
        path = f"/tests/{job_id}/file/{name}"
        if path not in self._cache:
            try:
                self._cache[path] = self._capped(path, max_bytes)
            except httpx.HTTPError as e:
                self._log_fetch_failure(path, e)
                self._cache[path] = ""
        return self._cache[path]
