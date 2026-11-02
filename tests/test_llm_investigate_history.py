# Copyright SUSE LLC
"""Unit tests for openqa_llm_investigate.history."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from openqa_llm_investigate import history

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

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
    return {
        "id": job_id,
        "result": result,
        "state": "done",
        "build": build,
        "failedmodules": list(mods),
        "comment_data": 0,
    }


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
            "MACHINE": "64bit",
            "QEMURAM": "2048",
        },
    }
    details = {"modules": [{"result": "failed"}, {"result": "skipped"}, {"result": "softfailed"}]}
    assert history.job_header(job, details) == [
        "Test: patch_job",
        "Result: failed (state done)",
        "Reason: boom INTERNAL-USE-ONLY-****-****",
        "Description: A desc",
        "Settings: MACHINE=64bit QEMURAM=2048",
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


def _commented(row: dict[str, Any], bugs: tuple[str, ...] = (), label: str | None = None) -> dict[str, Any]:
    """Return a history row with openQA's comment data for these bugrefs and label."""
    found = [
        {"content": b, "css_class": "label_bug fa-solid fa-bug", "title": f"Bug referenced: {b}\nx", "url": "u"}
        for b in bugs
    ]
    comment_data = {"bugs": found, "comment_icon": "", "comments": 1, "label": label, "label_url": None, "reviewed": 1}
    return {**row, "comment_data": comment_data}


def _reviews(make_openqa: Any, rows: list[dict[str, Any]]) -> list[str]:
    lines = _lines(make_openqa, {"data": rows})
    assert lines[0].startswith("Previous/next runs:")
    return [ln for ln in lines if ln.startswith("Earlier reviews")]


def test_earlier_reviews_of_the_same_module(make_openqa: Any) -> None:
    rows = [
        _commented(_hist(43, "failed", ("a", "b")), label="later_run"),
        _commented(_hist(42, "failed", ("a", "b"))),
        _commented(_hist(41, "softfailed", ("a", "b")), label="force_result:softfailed"),
        _commented(_hist(40, "failed", ("c",)), bugs=("poo#1",)),
        _commented(_hist(39, "failed", ("b", "a", "c"), build="1.1"), bugs=("boo#2",)),
        _commented(_hist(37, "failed", ("a", "b"))),
        _hist(36, "failed", ("a", "b")),
    ]
    expected = (
        "Earlier reviews of runs failing in the same module: force_result:softfailed (label, job 41, build 1.2);"
        " boo#2 (job 39, build 1.1)."
    )
    assert _reviews(make_openqa, rows) == [expected]
    assert "label:" not in expected


def test_earlier_reviews_name_each_mark_once(make_openqa: Any) -> None:
    rows = [
        _hist(42, "failed", ("a",)),
        _commented(_hist(41, "failed", ("a",)), bugs=("boo#2", "poo#3")),
        _commented(_hist(40, "failed", ("a",), build="1.1"), bugs=("boo#2", "poo#4")),
        _commented(_hist(39, "failed", ("a",)), label="force_result:softfailed"),
        _commented(_hist(38, "failed", ("a",)), label="force_result:softfailed"),
    ]
    expected = (
        "Earlier reviews of runs failing in the same module: boo#2 (job 41, build 1.2); poo#3 (job 41, build 1.2);"
        " poo#4 (job 40, build 1.1); force_result:softfailed (label, job 39, build 1.2)."
    )
    assert _reviews(make_openqa, rows) == [expected]


def test_earlier_reviews_capped(make_openqa: Any) -> None:
    rows = [
        _hist(42, "failed", ("a",)),
        *(_commented(_hist(30 + i, "failed", ("a",)), bugs=(f"poo#{i}",)) for i in range(7)),
    ]
    (line,) = _reviews(make_openqa, rows)
    assert line.count("(job ") == 5
    assert "poo#6 (job 36, build 1.2);" in line
    assert "poo#1" not in line


@pytest.mark.parametrize(("newer", "shown"), [(19, 1), (20, 0)])
def test_earlier_reviews_look_back(make_openqa: Any, newer: int, shown: int) -> None:
    runs = [_hist(41 - i, "passed") for i in range(newer)]
    rows = [_hist(42, "failed", ("a",)), *runs, _commented(_hist(41 - newer, "failed", ("a",)), bugs=("poo#7",))]
    assert len(_reviews(make_openqa, rows)) == shown


@pytest.mark.parametrize("label", ["linked", "linked:poo#209598", "unknown_failure"])
def test_earlier_reviews_skip_bot_labels(make_openqa: Any, label: str) -> None:
    rows = [_hist(42, "failed", ("a",)), _commented(_hist(41, "failed", ("a",)), bugs=("poo#1",), label=label)]
    assert _reviews(make_openqa, rows) == [
        "Earlier reviews of runs failing in the same module: poo#1 (job 41, build 1.2)."
    ]


_SNAPSHOT_MODULES = ("boot_into_snapshot", "rollback_from_ro_snapshot")


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [_hist(42, "failed"), _commented(_hist(41, "failed", ("a",)), label="x")], id="job-without-module"
        ),
        pytest.param(
            [_hist(42, "failed", ("a",)), _commented(_hist(41, "failed", ("b",)), label="x")], id="other-module"
        ),
        pytest.param(
            [
                _hist(42, "failed", ("desktop_mainmenu", *_SNAPSHOT_MODULES)),
                _commented(_hist(41, "softfailed", _SNAPSHOT_MODULES), bugs=("poo#195551",)),
            ],
            id="not-every-module",
        ),
        pytest.param([_hist(42, "failed", ("a",)), _commented(_hist(41, "failed", ("a",)))], id="no-bugref-or-label"),
        pytest.param(
            [_hist(42, "failed", ("a",)), _commented(_hist(41, "failed", ("a",)), label="linked")], id="linked"
        ),
        pytest.param(
            [
                _hist(42, "failed", ("a",)),
                {**_commented(_hist(41, "failed", ("a",)), label="x"), "state": "running"},
                _hist(40, "passed"),
            ],
            id="not-done",
        ),
        pytest.param(
            [_hist(42, "failed", ("a",)), {**_hist(41, "failed", ("a",)), "comment_data": {"bugs": [{"url": "u"}]}}],
            id="malformed-comment-data",
        ),
    ],
)
def test_no_earlier_reviews(make_openqa: Any, rows: list[dict[str, Any]]) -> None:
    assert not _reviews(make_openqa, rows)


@pytest.mark.parametrize("served", [pytest.param(["/tests/42/ajax"], id="served"), pytest.param([], id="404")])
def test_earlier_reviews_reuse_the_history_response(make_openqa: Any, mocker: MockerFixture, served: list[str]) -> None:
    rows = [_hist(42, "failed", ("a",)), _commented(_hist(41, "failed", ("a",)), label="x")]
    oqa = make_openqa(dict.fromkeys(served, json.dumps({"data": rows})))
    get = mocker.spy(oqa.client, "get")
    lines = history.run_history(oqa, "42", {})
    expected = "Earlier reviews of runs failing in the same module: x (label, job 41, build 1.2)."
    assert (expected in lines) == bool(served)
    assert [c.args[0] for c in get.call_args_list].count("https://srv/tests/42/ajax") == 1
