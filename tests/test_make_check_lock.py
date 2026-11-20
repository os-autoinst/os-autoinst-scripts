# Copyright SUSE LLC
"""Tests for the check-lock Makefile target that keeps uv.lock in sync."""

import shutil
import subprocess
from pathlib import Path

MAKE = shutil.which("make") or "make"
REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_CONFIG_FILES = ("pyproject.toml", "uv.lock", "Makefile", ".setup.mk")


def run_check_lock(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([MAKE, "-C", str(directory), "check-lock"], capture_output=True, text=True, check=False)


def test_check_lock_accepts_the_committed_lock() -> None:
    result = run_check_lock(REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_check_lock_rejects_a_lock_that_does_not_match_pyproject(tmp_path: Path) -> None:
    for name in TOOL_CONFIG_FILES:
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    pyproject = tmp_path / "pyproject.toml"
    content = pyproject.read_text()
    drifted = content.replace('"vulture",', '"vulture==2.16",')
    assert drifted != content
    pyproject.write_text(drifted)
    result = run_check_lock(tmp_path)
    assert result.returncode != 0
