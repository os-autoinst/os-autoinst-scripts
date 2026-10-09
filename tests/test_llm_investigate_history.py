# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.history."""

from __future__ import annotations

import json
from typing import Any

from openqa_llm_investigate import history

_JOB = {"job": {"group_id": 7, "settings": {"BUILD": "1.2"}}}
_ONE_FAILED = {"modules": [{"name": "a", "result": "failed", "details": []}]}
_FAIL_CMD = {
    "modules": [
        {
            "name": "a",
            "result": "failed",
            "details": [
                {
                    "num": 1,
                    "result": "fail",
                    "display_title": "wait_serial",
                    "text_data": "# Command: SUSEConnect -r INTERNAL-USE-ONLY-fba30486a8951760\n# Exit code: 1",
                }
            ],
        }
    ]
}


def _hist(job_id: int, result: str, mods: tuple[str, ...] = (), build: str = "1.2") -> dict[str, Any]:
    return {"id": job_id, "result": result, "state": "done", "build": build, "failedmodules": list(mods)}


def _sib(job_id: int, result: str, test: str = "t", mods: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"id": job_id, "result": result, "test": test, "modules": [{"name": m, "result": "failed"} for m in mods]}


def _lines(
    make_openqa: Any,
    ajax: dict[str, Any] | None = None,
    jobs: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> list[str]:
    routes = {"/api/v1/jobs/42": json.dumps(job or _JOB), "/tests/*/details_ajax": json.dumps(details or _ONE_FAILED)}
    if ajax is not None:
        routes["/tests/42/ajax"] = json.dumps(ajax)
    if jobs is not None:
        routes["/api/v1/jobs"] = json.dumps(jobs)
    oqa = make_openqa(routes)
    return history.run_history(oqa, "42", history.fetch_job(oqa, "42"))


def _text(*args: Any, **kwargs: Any) -> str:
    return "\n".join(_lines(*args, **kwargs))


def test_header_shows_job_metadata_and_counts() -> None:
    job = {
        "test": "patch_job",
        "result": "failed",
        "state": "done",
        "reason": "boom INTERNAL-USE-ONLY-cafe-beef",
        "settings": {
            "JOB_DESCRIPTION": "A desc",
            "DISTRI": "sle",
            "VERSION": "15-SP7",
            "ARCH": "aarch64",
            "QEMURAM": "2048",
        },
    }
    details = {"modules": [{"result": "failed"}, {"result": "skipped"}, {"result": "softfailed"}]}
    assert history.job_header(job, details) == [
        "Test: patch_job",
        "Result: failed (state done)",
        "Reason: boom INTERNAL-USE-ONLY-****-****",
        "Description: A desc",
        "Settings: DISTRI=sle VERSION=15-SP7 ARCH=aarch64 QEMURAM=2048",
        "Modules: 1 failed / 1 softfailed / 1 skipped",
        "",
    ]


def test_header_without_job_data() -> None:
    assert history.job_header({}, _ONE_FAILED) == ["Modules: 1 failed / 0 softfailed / 0 skipped", ""]
    assert not history.job_header({}, {})


def test_fetch_job_survives_api_failure(make_openqa: Any) -> None:
    assert history.fetch_job(make_openqa({}), "42") == {}


def test_everything_else_succeeded(make_openqa: Any) -> None:
    ajax = {"data": [_hist(42, "failed", ("a",)), _hist(41, "passed"), _hist(40, "passed"), _hist(43, "passed")]}
    jobs = {"jobs": [_sib(42, "failed"), _sib(50, "passed"), _sib(51, "softfailed")]}
    assert _lines(make_openqa, ajax, jobs) == [
        "Previous/next runs: previous 2 run(s): 2 passed; next 1 run(s): 1 passed.",
        "Same group+build: everything else succeeded, all 2 other jobs in group 7 build 1.2 passed or softfailed.",
        "",
    ]


def test_failed_runs_with_commands_and_masking(make_openqa: Any) -> None:
    ajax = {
        "data": [
            _hist(42, "failed", ("a",)),
            _hist(30, "failed", ("a",), build="1.1"),
            _hist(31, "failed", ("a",), build="1.1"),
            _hist(32, "passed"),
            _hist(33, "incomplete"),
        ]
    }
    jobs = {
        "jobs": [_sib(60, "failed", "x", ("a",)), _sib(61, "failed", "x", ("a",)), _sib(62, "passed"), _sib(63, "none")]
    }
    out = _text(make_openqa, ajax, jobs, details=_FAIL_CMD)
    assert "2x failed in a (`SUSEConnect -r ****`) [job 31, build 1.1]" in out
    assert "incomplete [job 33, build 1.2]" in out
    assert "2 of 4 other jobs in group 7 build 1.2 failed: 2x x failed in a (" in out
    assert "1 passed/softfailed, 1 not finished." in out
    assert "fba30486a8951760" not in out


def test_no_failed_siblings_but_unfinished(make_openqa: Any) -> None:
    out = _text(make_openqa, None, {"jobs": [_sib(50, "passed"), _sib(51, "none")]})
    assert "no other job in group 7 build 1.2 failed (1 passed/softfailed, 1 not finished)" in out
    assert "Previous/next" not in out


def test_fetch_budget_and_group_cap(make_openqa: Any) -> None:
    jobs = {"jobs": [_sib(100 + i, "failed", f"t{i}", (f"m{i}",)) for i in range(12)]}
    out = _text(make_openqa, None, jobs, details=_FAIL_CMD)
    assert "+6 more" in out
    assert out.count("`SUSEConnect") == 6


def test_same_flavor_failures_first(make_openqa: Any) -> None:
    def sib(i: int, flavor: str, test: str) -> dict[str, Any]:
        return {**_sib(i, "failed", test, ("m",)), "settings": {"FLAVOR": flavor}}

    others = [sib(100 + i, "other", f"o{i}") for i in range(8)] + [sib(120, "other", "o0")]
    mine = [sib(200 + i, "mine", f"s{i}") for i in range(2)]
    job = {"job": {"group_id": 7, "settings": {"BUILD": "1", "FLAVOR": "mine"}}}
    out = _text(make_openqa, None, {"jobs": others + mine}, job=job, details=_FAIL_CMD)
    assert "(2 in flavor mine)" in out
    line = next(ln for ln in out.splitlines() if ln.startswith("Same group"))
    assert line.index("s0 failed") < line.index("2x o0") < line.index("o1")
    assert line.index("s1 failed") < line.index("2x o0")
    assert "s0 failed in m (`SUSEConnect" in line


def test_no_flavor_keeps_count_order(make_openqa: Any) -> None:
    jobs = [_sib(1, "failed", "a", ("m",)), _sib(2, "failed", "b", ("m",)), _sib(3, "failed", "b", ("m",))]
    out = _text(make_openqa, None, {"jobs": jobs})
    assert "in flavor" not in out
    assert out.index("2x b") < out.index("a failed")


def test_omitted_when_unavailable(make_openqa: Any) -> None:
    assert not _lines(make_openqa, None, None, job={"job": {"settings": {}}})


def test_only_current_job_in_group_omitted(make_openqa: Any) -> None:
    assert not _lines(make_openqa, {"data": [_hist(42, "failed")]}, {"jobs": [_sib(42, "failed")]})


def test_details_fetch_error_keeps_modules(make_openqa: Any) -> None:
    oqa = make_openqa({
        "/api/v1/jobs/42": json.dumps(_JOB),
        "/tests/42/ajax": json.dumps({"data": [_hist(41, "failed", ("a",))]}),
    })
    assert "failed in a [job 41, build 1.2]" in "\n".join(history.run_history(oqa, "42", history.fetch_job(oqa, "42")))


def test_fetch_budget_shared_across_sections(make_openqa: Any) -> None:
    ajax = {"data": [_hist(42, "failed"), *(_hist(30 + i, "failed", (f"p{i}",)) for i in range(5))]}
    jobs = {"jobs": [_sib(100 + i, "failed", f"t{i}", (f"m{i}",)) for i in range(6)]}
    assert _text(make_openqa, ajax, jobs, details=_FAIL_CMD).count("`SUSEConnect") == 8


def test_siblings_filtered_by_version(make_openqa: Any) -> None:
    seen: list[str] = []

    def jobs(request: Any) -> str:
        seen.append(str(request.url.query, "ascii"))
        return json.dumps({"jobs": [_sib(50, "passed")]})

    job = {"job": {"group_id": 7, "settings": {"BUILD": "1.2", "VERSION": "16.0"}}}
    oqa = make_openqa({"/api/v1/jobs/42": json.dumps(job), "/api/v1/jobs": jobs})
    history.run_history(oqa, "42", history.fetch_job(oqa, "42"))
    assert seen == ["groupid=7&build=1.2&version=16.0&latest=1&limit=500"]
