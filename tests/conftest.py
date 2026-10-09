# Copyright SUSE LLC
"""Shared fixtures for the openqa_llm_investigate tests."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

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
