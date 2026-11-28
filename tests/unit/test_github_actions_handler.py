"""Rotas somente leitura de GitHub Actions (backend/api/handlers/github.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.api.handlers import github
from tests.fake_requests import fake_request


def _request(user_id: str | None = "alice") -> MagicMock:
    user = SimpleNamespace(id=user_id) if user_id else None
    return fake_request(state=SimpleNamespace(user=user))


def _fake_gh(monkeypatch: pytest.MonkeyPatch, result: dict):
    calls: list[list[str]] = []

    async def fake(args, cwd=None, input_data=None, user_id=None):
        calls.append(args)
        return result

    monkeypatch.setattr(github, "_gh_run", fake)
    monkeypatch.setattr(github, "_workspace_cwd", lambda _w: "/tmp/ws")
    return calls


RUN = {
    "databaseId": 987654321,
    "number": 590,
    "displayTitle": "feat: algo",
    "workflowName": "PR Gate",
    "status": "completed",
    "conclusion": "failure",
    "event": "pull_request",
    "headBranch": "feat/x",
    "headSha": "abc1234",
    "createdAt": "2026-11-19T10:00:00Z",
    "updatedAt": "2026-11-19T10:05:00Z",
    "startedAt": "2026-11-19T10:00:05Z",
    "url": "https://github.com/o/r/actions/runs/987654321",
    "attempt": 2,
}

JOB = {
    "databaseId": 111,
    "name": "Título e labels",
    "status": "completed",
    "conclusion": "failure",
    "startedAt": "2026-11-19T10:00:10Z",
    "completedAt": "2026-11-19T10:00:42Z",
    "url": "https://github.com/o/r/actions/runs/987654321/job/111",
    "steps": [
        {
            "number": 1,
            "name": "Set up job",
            "status": "completed",
            "conclusion": "success",
            "startedAt": "2026-11-19T10:00:10Z",
            "completedAt": "2026-11-19T10:00:13Z",
        },
        {
            "number": 2,
            "name": "Run lint",
            "status": "completed",
            "conclusion": "failure",
        },
        "lixo",
    ],
}


async def test_run_list_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": json.dumps([RUN])})

    response = await github.list_runs(_request(), None, None, None, "ws1")

    assert response.status == "ok"
    [run] = response.runs
    assert (run.id, run.number, run.title, run.workflow) == (
        987654321,
        590,
        "feat: algo",
        "PR Gate",
    )
    assert (run.status, run.conclusion, run.event) == (
        "completed",
        "failure",
        "pull_request",
    )
    assert (run.branch, run.sha, run.attempt) == ("feat/x", "abc1234", 2)
    assert calls[0][:2] == ["run", "list"]
    assert "--branch" not in calls[0]


async def test_run_list_passes_filters_as_gh_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": "[]"})

    await github.list_runs(_request(), "failure", "feat/x", "PR Gate", None)

    args = calls[0]
    assert args[args.index("--status") + 1] == "failure"
    assert args[args.index("--branch") + 1] == "feat/x"
    assert args[args.index("--workflow") + 1] == "PR Gate"


async def test_run_list_rejects_an_unknown_status_before_calling_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": "[]"})

    with pytest.raises(HTTPException) as error:
        await github.list_runs(_request(), "--repo=evil", None, None, None)

    assert error.value.status_code == 422
    assert calls == []


@pytest.mark.parametrize(
    "value", ["--repo=x", "-f", "", " leading", "a\nb", "x" * 201, ";rm -rf"]
)
def test_branch_and_workflow_pattern_blocks_option_injection(value: str) -> None:
    import re

    assert re.match(github._REF_PATTERN, value) is None


@pytest.mark.parametrize(
    "value", ["feat/x", "release/0.2", "main", "PR Gate", "ci.yml", "fix_1-2"]
)
def test_branch_and_workflow_pattern_accepts_normal_names(value: str) -> None:
    import re

    assert re.match(github._REF_PATTERN, value) is not None


async def test_run_list_skips_items_without_an_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(
        monkeypatch,
        {"status": "ok", "output": json.dumps([RUN, {"displayTitle": "sem id"}, 3])},
    )

    response = await github.list_runs(_request(), None, None, None, None)

    assert [r.id for r in response.runs] == [987654321]


async def test_run_list_reports_gh_unavailable_not_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(monkeypatch, {"status": "error", "message": "gh not found in PATH"})

    response = await github.list_runs(_request(), None, None, None, None)

    assert response.status == "unavailable"
    assert response.runs == []


@pytest.mark.parametrize("output", ["", "nope", "{}", '"texto"'])
async def test_run_list_rejects_invalid_payloads(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    _fake_gh(monkeypatch, {"status": "ok", "output": output})

    response = await github.list_runs(_request(), None, None, None, None)

    assert response.status == "error"


async def test_run_list_requires_an_authenticated_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(monkeypatch, {"status": "ok", "output": "[]"})

    with pytest.raises(HTTPException) as error:
        await github.list_runs(_request(None), None, None, None, None)

    assert error.value.status_code == 401


async def test_run_detail_returns_jobs_and_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_gh(
        monkeypatch,
        {"status": "ok", "output": json.dumps({**RUN, "jobs": [JOB, {"name": "x"}]})},
    )

    response = await github.get_run(_request(), 987654321, "ws1")

    assert calls[0][:3] == ["run", "view", "987654321"]
    run = response.run
    assert response.status == "ok" and run is not None
    assert run.title == "feat: algo" and run.attempt == 2
    [job] = run.jobs
    assert (job.id, job.name, job.conclusion) == (111, "Título e labels", "failure")
    assert [s.name for s in job.steps] == ["Set up job", "Run lint"]
    assert job.steps[0].completed_at == "2026-11-19T10:00:13Z"
    assert job.steps[1].started_at == ""
    assert job.url.endswith("/job/111")


async def test_run_detail_while_running_has_empty_conclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = {**RUN, "status": "in_progress", "conclusion": None, "jobs": []}
    _fake_gh(monkeypatch, {"status": "ok", "output": json.dumps(running)})

    response = await github.get_run(_request(), 987654321, None)

    assert response.run is not None
    assert (response.run.status, response.run.conclusion) == ("in_progress", "")


@pytest.mark.parametrize("output", ["", "nope", "[]", '{"displayTitle": "sem id"}'])
async def test_run_detail_rejects_invalid_payloads(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    _fake_gh(monkeypatch, {"status": "ok", "output": output})

    response = await github.get_run(_request(), 1, None)

    assert response.status == "error"
    assert response.run is None


async def test_run_detail_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(monkeypatch, {"status": "error", "message": "could not find run"})

    response = await github.get_run(_request(), 1, None)

    assert response.status == "error"
    assert "could not find run" in response.message


async def test_job_log_returns_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": "linha 1\nlinha 2"})

    response = await github.get_job_log(_request(), 10, 20, "ws1")

    assert calls[0] == ["run", "view", "10", "--job", "20", "--log"]
    assert (response.status, response.log, response.truncated) == (
        "ok",
        "linha 1\nlinha 2",
        False,
    )


async def test_job_log_keeps_the_end_when_truncating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = "inicio\n" + "x" * github._MAX_LOG_CHARS + "\nfim com a falha"
    _fake_gh(monkeypatch, {"status": "ok", "output": big})

    response = await github.get_job_log(_request(), 10, 20, None)

    assert response.truncated is True
    assert len(response.log) == github._MAX_LOG_CHARS
    assert response.log.endswith("fim com a falha")
    assert "inicio" not in response.log


async def test_job_log_of_a_running_job_is_an_error_not_an_empty_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(monkeypatch, {"status": "error", "message": "job is still in progress"})

    response = await github.get_job_log(_request(), 10, 20, None)

    assert response.status == "error"
    assert response.log == ""


async def test_workflows_are_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_gh(
        monkeypatch,
        {
            "status": "ok",
            "output": json.dumps(
                [
                    {"id": 1, "name": "PR Gate", "state": "active"},
                    {"name": "sem id"},
                    "lixo",
                ]
            ),
        },
    )

    response = await github.list_workflows(_request(), "ws1")

    assert calls[0][:2] == ["workflow", "list"]
    assert [(w.id, w.name, w.state) for w in response.workflows] == [
        (1, "PR Gate", "active")
    ]


async def test_workflows_report_gh_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(monkeypatch, {"status": "error", "message": "gh not found in PATH"})

    response = await github.list_workflows(_request(), None)

    assert response.status == "unavailable"


def test_the_actions_routes_only_read() -> None:
    methods: dict[str, set[str]] = {}
    for route in github.router.routes:
        path = str(getattr(route, "path", ""))
        if "/runs" in path or "/workflows" in path:
            methods[path] = set(getattr(route, "methods", None) or set())

    assert methods
    assert all(m == {"GET"} for m in methods.values())
