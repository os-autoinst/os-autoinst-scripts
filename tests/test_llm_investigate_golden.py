# Copyright SUSE LLC
"""Replay recorded o3 jobs through analyze_job; expected.txt was checked against openqa-ai for the same job."""

from __future__ import annotations

import json
import lzma
import pathlib

import httpx
import pytest

from openqa_llm_investigate.analysis import analyze_job
from openqa_llm_investigate.http import OpenQA

GOLDEN = pathlib.Path(__file__).parent / "data" / "analysis" / "golden"
OSD_JOBS = {"24776159"}


@pytest.mark.parametrize("job_id", sorted(p.name for p in GOLDEN.iterdir()))
def test_golden_analysis(job_id: str) -> None:
    routes = json.loads(lzma.decompress((GOLDEN / job_id / "routes.json.xz").read_bytes()))

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.raw_path.decode())
        return httpx.Response(404) if body is None else httpx.Response(200, text=body)

    server = "https://openqa.suse.de" if job_id in OSD_JOBS else "https://openqa.opensuse.org"
    oqa = OpenQA(httpx.Client(transport=httpx.MockTransport(handler)), server)
    assert analyze_job(oqa, job_id) == (GOLDEN / job_id / "expected.txt").read_bytes().decode()
