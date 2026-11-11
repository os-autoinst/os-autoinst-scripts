# Copyright SUSE LLC
"""Ask a decision model for the failure class and write it into the Classification line of the verdict."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

STATE_CHARS = 30000
ENV_THRESHOLD = 0.3
# the job done hook of openQA waits for this request, and a hook that runs out of time posts nothing
MAX_SECONDS = 300
CLASS_NAMES = {"product": "Product bug", "test": "Test issue", "infra": "Infrastructure problem"}
DIFFERS = "  (the analysis model says {})"
NO_CLASS = "  (no class from the analysis model)"
# without it a summary with the class of the LLM alone reads as if both models agreed
NO_ANSWER = "  (no answer from the decision model)"
# openSUSE-specific: the wording and its examples; their effect was measured on jobs of openqa.opensuse.org
_INSTRUCTIONS = (
    "What caused this openQA test failure: the tested openSUSE software, the openQA test itself,"
    " or the test environment?"
)
_CRITERIA = {
    "product": {
        "definition": "The tested openSUSE software misbehaves: a package, service, kernel, installer, desktop or"
        " tool in the product build crashes, regressed, reports an error, or produces wrong output, so a bug must be"
        " filed against the product.",
        "not_for": "A screen or output that changed on purpose in the product and that the test does not know yet"
        " (that is a test issue). A worker, network, mirror or openQA backend failure (that is infrastructure).",
        "examples": [
            (
                "first_boot: no candidate needle with tag(s) 'displaymanager, emergency-shell, emergency-mode' and the"
                " serial log shows the system dropped to emergency mode"
            ),
            "zypper -n in libvirt vhostmd failed: nothing provides a dependency of a package in the build",
            "journal_check finds new errors from a product service; orphaned_packages_check finds unexpected orphans",
            (
                "check_selinux_fails: '! grep denied' fails because product policy denies a product service"
                " (e.g. plymouthd denied access to /var)"
            ),
        ],
    },
    "test": {
        "definition": "The openQA test is wrong or outdated: its code, needles, test data, schedule or expectations"
        " no longer match an intended product change, or the test is fragile (timing, wrong prompt, wrong VT,"
        " missing preinstalled tool).",
        "not_for": "A product regression that makes the expected screen or output never appear, such as a crash, an"
        " emergency shell or an error dialog from the product (that is a product bug). The worker or network failing"
        " (that is infrastructure).",
        "examples": [
            (
                "opensuse_welcome: no candidate needle with tag(s) 'opensuse-welcome-close' after the welcome dialog"
                " was redesigned"
            ),
            "libqt5_qtbase: the test expects yast to be preinstalled, which it no longer is",
            "login_test: needle 'users-settings' does not match the new GNOME settings layout",
            "kubevirt_smoketest: the test applies an outdated operator manifest that fails validation",
        ],
    },
    "infra": {
        "definition": "The test environment failed, not the product or the test: the worker, its virtualization"
        " backend or firmware, the network between machines, mirrors, external servers or registries, asset"
        " downloads, or openQA itself.",
        "not_for": "The product's own networking or services failing on a healthy network (that is a product bug)."
        " A test pointing at a wrong or removed repository (that is a test issue).",
        "examples": [
            "bootloader_uefi: firmware crash (Synchronous Exception) before the bootloader appears",
            "multi machine tests cannot reach each other over the worker network",
            "a download from an external server or registry times out or is unreachable from the worker",
            "the backend died, the qemu process ended, or the job hit the worker's timeout without output",
        ],
    },
}
_ENV_QUESTION = (
    "Did the test environment fail (the worker, its virtualization backend or firmware, the network between"
    " machines, a mirror, an external server, an asset download or openQA itself), rather than the tested software"
    " or the test?"
)
# also with a space before the colon, or with a full-width colon or a spaced dash in its place
_CLASS_RE = re.compile(
    r"(?:[\s*_`>#-]|\d+\.)*Classification[*_`]*(?:\s*[:\uff1a]|\s+[-\u2013\u2014]\s)"
    r"[\s*_`]*(.*[^\s*_`.])?[\s*_`.]*$",
    re.IGNORECASE,
)
# a decision server that is down, slow or answers something else must not cost the summary
_NO_ANSWER_ERRORS = (httpx.HTTPError, httpx.InvalidURL, KeyError, TypeError, ValueError, OverflowError, RecursionError)

log = logging.getLogger(__name__)


def state_of(prompt: str) -> str:
    """Return the job header and the failure analysis of the first LLM prompt, without the questions to the LLM."""
    first = prompt.split("You will get a few short questions", 1)[0]
    header = first.split("\n\n", 1)[0]
    analysis = first.split("\nFailure analysis (", 1)[-1]
    lines = f"{header}\n\nFailure analysis ({analysis}".splitlines()
    # the answers were measured without the reviews of earlier runs
    return "\n".join(ln for ln in lines if not ln.startswith("Earlier reviews")).strip()[:STATE_CHARS]


def questions() -> dict[str, dict[str, Any]]:
    """Return the questions of one decision request: the class, and whether the environment failed."""
    return {
        "guide": {"type": "choice", "instructions": _INSTRUCTIONS, "criteria": _CRITERIA},
        "env": {"type": "noul", "instructions": _ENV_QUESTION},
    }


def decide(answers: dict[str, dict[str, Any]]) -> str:
    """Return `product`, `test` or `infra` from the answers to `questions()`.

    Only the yes/no question decides on `infra`; the class question then picks between the other two.
    """
    if float(answers["env"]["noul"]) >= ENV_THRESHOLD:
        return "infra"
    probabilities = answers["guide"]["probabilities"]
    return max(("product", "test"), key=lambda c: float(probabilities[c]))


def _last_class(lines: list[str]) -> tuple[int, str] | None:
    """Return the index of the last Classification line and what it names."""
    found = [(i, m.group(1) or "") for i, ln in enumerate(lines) if (m := _CLASS_RE.match(ln))]
    return found[-1] if found else None


def _with_class(verdict: str, name: str) -> str:
    """Set the last Classification line of the verdict to `name`, on top without such a line.

    Unless the LLM named the same class, a line below quotes what it named, or says that it named none.
    """
    lines = verdict.splitlines()
    at, said = 0, ""
    if last := _last_class(lines):
        at, said = last
        del lines[at]
    theirs = [] if said.casefold() == name.casefold() else [DIFFERS.format(said) if said else NO_CLASS]
    lines[at:at] = [f"Classification: {name}", *theirs]
    return "\n".join(lines)


def _without_answer(verdict: str) -> str:
    """Add `NO_ANSWER` below the last Classification line of the verdict, on top without such a line."""
    lines = verdict.splitlines()
    last = _last_class(lines)
    at = last[0] + 1 if last else 0
    lines[at:at] = [NO_ANSWER]
    return "\n".join(lines)


def decided_verdict(url: str, prompt: str, verdict: str, *, headers: dict[str, str], timeout: float) -> str:
    """Return the verdict with the class of the decision model at `url`, unchanged without a URL.

    `url` is the `/v1/systemone` endpoint of a llama.cpp server with a decision model, `prompt` the first LLM prompt.
    Without a usable answer the verdict keeps the class of the LLM and gets the line `NO_ANSWER`.
    """
    if not url:
        return verdict
    body = {"state": state_of(prompt), "questions": questions()}
    try:
        # a single try: a retry would delay the summary further
        response = httpx.post(url, json=body, headers=headers, timeout=min(timeout, MAX_SECONDS))
        response.raise_for_status()
        decided = decide(response.json()["answers"])
    except _NO_ANSWER_ERRORS as e:
        log.warning("No class from the decision model at %s, keeping the one of the LLM: %r", url, e)
        return _without_answer(verdict)
    return _with_class(verdict, CLASS_NAMES[decided])
