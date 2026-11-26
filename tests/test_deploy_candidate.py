# Copyright SUSE LLC
"""Unit tests for deploy_candidate."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/benchmarking/deploy_candidate.sh"
BASH = shutil.which("bash") or "/bin/bash"
DEFAULT_IMAGE = "ghcr.io/ggml-org/llama.cpp:server"
STUB_SSH = """#!/bin/sh
{ printf 'ARG:%s\\n' "$@"; echo "---STDIN---"; cat; } > "$DEPLOY_CAPTURE"
"""
DEFAULT_PODMAN = (
    'su - llama -c "podman run --name llama-bench-8082 -d   '
    "-v /opt/llm/models:/root/.cache/huggingface:z   "
    "-p 8082:8080   ghcr.io/ggml-org/llama.cpp:server   "
    "-hf 'unsloth/gemma-4-26B-A4B-it-GGUF' --hf-file 'UD-Q4_K_M'   "
    "--threads 64   --ctx-size 65536   --cache-ram 65536   "
    "-ctk q8_0   -ctv q8_0   -fa on   --no-mmproj   "
    '--temp 1.0   --top-p 0.95   --top-k 64"'
)


class DeployCandidateTestCase(unittest.TestCase):
    """Runs the script against a stub ssh that records its arguments."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "ssh"
        stub.write_text(STUB_SSH, encoding="utf-8")
        stub.chmod(0o755)
        self.capture = self.root / "capture.txt"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DEPLOY_CAPTURE": str(self.capture),
        }

    def run_script(self, *args: str) -> tuple[str, str]:
        result = subprocess.run(
            [BASH, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        captured = self.capture.read_text(encoding="utf-8")
        arguments, _, remote = captured.partition("---STDIN---\n")
        return arguments, remote


class TestDefaultInvocation(DeployCandidateTestCase):
    """The start action without options deploys the production shape."""

    def test_default_start_is_unchanged(self) -> None:
        arguments, remote = self.run_script("start", "unsloth/gemma-4-26B-A4B-it-GGUF@UD-Q4_K_M", "8082", "64", "65536")
        assert "ARG:w26\n" in arguments
        assert f"{DEFAULT_PODMAN}\n" in remote
        assert "--spec-" not in remote


class TestWorkerOption(DeployCandidateTestCase):
    """The worker override reaches start, stop and status."""

    def test_worker_override_reaches_every_action(self) -> None:
        cases = [
            {
                "desc": "start runs on the requested worker",
                "args": (
                    "start",
                    "--worker",
                    "w27",
                    "unsloth/gemma-4-26B-A4B-it-GGUF@UD-Q4_K_M",
                    "8082",
                ),
                "expected": ["ARG:w27\n"],
            },
            {
                "desc": "stop removes the container on the requested worker",
                "args": ("stop", "--worker", "w27", "8083"),
                "expected": ["ARG:w27\n", "podman rm -f llama-bench-8083"],
            },
            {
                "desc": "status inspects the container on the requested worker",
                "args": ("status", "--worker", "w27", "8083"),
                "expected": ["ARG:w27\n", "llama-bench-8083"],
            },
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                arguments, remote = self.run_script(*case["args"])
                for needle in case["expected"]:
                    assert needle in arguments + remote
                assert "ARG:w26\n" not in arguments

    def test_unknown_option_is_rejected(self) -> None:
        result = subprocess.run(
            [BASH, str(SCRIPT), "start", "--socket", "/run/podman.sock"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
            check=False,
        )
        assert result.returncode == 1
        assert "Unknown option: --socket" in result.stderr


class TestImageOption(DeployCandidateTestCase):
    """The image override replaces the runner image."""

    def test_image_override_replaces_the_runner(self) -> None:
        _, remote = self.run_script(
            "start",
            "--image",
            "localhost/llama-fork:24ea7bb",
            "unsloth/gemma-4-26B-A4B-it-GGUF@UD-Q4_K_M",
            "8082",
        )
        assert "localhost/llama-fork:24ea7bb   -hf" in remote
        assert DEFAULT_IMAGE not in remote


class TestDraftOptions(DeployCandidateTestCase):
    """The speculative decoding flags reach the server command line."""

    def test_draft_flags_reach_the_server(self) -> None:
        cases = [
            {
                "desc": "repo and file become the draft repo and draft model",
                "args": (
                    "--draft",
                    "unsloth/gemma-4-26B-A4B-it-qat-GGUF@mtp-gemma-4-26B-A4B-it.gguf",
                ),
                "expected": (
                    "--spec-draft-hf 'unsloth/gemma-4-26B-A4B-it-qat-GGUF' "
                    "--spec-draft-model 'mtp-gemma-4-26B-A4B-it.gguf'"
                ),
            },
            {
                "desc": "a bare path becomes the draft model only",
                "args": ("--draft", "/opt/llm/models/draft.gguf"),
                "expected": "--spec-draft-model '/opt/llm/models/draft.gguf'",
            },
        ]
        for case in cases:
            with self.subTest(case["desc"]):
                _, remote = self.run_script(
                    "start",
                    *case["args"],
                    "unsloth/gemma-4-26B-A4B-it-GGUF@UD-Q4_K_M",
                    "8083",
                )
                assert case["expected"] in remote
                assert "--name llama-bench-8083" in remote

    def test_spec_type_and_n_max_reach_the_server(self) -> None:
        _, remote = self.run_script(
            "start",
            "--spec-type",
            "draft-mtp",
            "--spec-draft-n-max",
            "6",
            "unsloth/gemma-4-26B-A4B-it-GGUF@UD-Q4_K_M",
            "8083",
        )
        assert "--spec-type 'draft-mtp'" in remote
        assert "--spec-draft-n-max '6'" in remote
        assert "--hf-file 'UD-Q4_K_M' --spec-type 'draft-mtp'" in remote


if __name__ == "__main__":
    unittest.main()
