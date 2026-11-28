"""Rotas de PRs e issues da aba GitHub (backend/api/handlers/github.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.handlers import github
from backend.vtypes.contracts import JsonValue


def _request(user_id: str | None = "alice"):
    user = SimpleNamespace(id=user_id) if user_id else None
    return SimpleNamespace(state=SimpleNamespace(user=user))


def _fake_gh(monkeypatch: pytest.MonkeyPatch, result: dict):
    calls: list[tuple[list[str], str | None]] = []

    async def fake(args, cwd=None, input_data=None, user_id=None):
        calls.append((args, user_id))
        return result

    monkeypatch.setattr(github, "_gh_run", fake)
    monkeypatch.setattr(github, "_workspace_cwd", lambda _w: "/tmp/ws")
    monkeypatch.setattr(github, "_github_repository", lambda _cwd: "o/r")
    monkeypatch.setattr(github, "github_auth_epoch", lambda _user_id: _epoch())
    return calls


async def _epoch(_user_id: str | None = None) -> str:
    return "epoch-1"


def test_checks_summary_counts_each_outcome() -> None:
    summary = github._checks_summary(
        [
            {"conclusion": "SUCCESS"},
            {"conclusion": "SKIPPED"},
            {"conclusion": "FAILURE"},
            {"status": "IN_PROGRESS", "conclusion": ""},
            {"state": "PENDING"},
            "lixo",
        ]
    )

    assert (summary.passed, summary.failed, summary.pending) == (2, 1, 2)
    assert github._checks_summary(None).passed == 0


async def test_pulls_are_listed_with_review_and_without_checks_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "number": 7,
            "title": "feat: algo",
            "state": "open",
            "draft": True,
            "user": {"login": "ana"},
            "head": {"ref": "feat/x"},
            "base": {"ref": "master"},
            "updated_at": "2026-11-19T10:00:00Z",
            "html_url": "https://github.com/o/r/pull/7",
        },
        {"sem": "numero"},
    ]
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": json.dumps(payload)})

    response = await github.list_pulls(_request(), state="all", workspace_id=None)

    assert response.state == "loaded"
    assert len(response.items) == 1
    assert response.status == "ok"
    assert response.pulls == response.items
    pull = response.items[0]
    assert (pull.number, pull.author, pull.is_draft) == (7, "ana", True)
    assert pull.review_decision == ""
    assert pull.checks.passed == 0
    # A lista não pede o rollup de checks (payload pesado); só o detalhe pede.
    assert "statusCheckRollup" not in " ".join(calls[0][0])
    assert calls[0][0][:3] == ["api", "--include", "repos/o/r/pulls"]
    assert calls[0][1] == "alice"


async def test_issues_are_listed_with_labels_and_comment_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "number": 3,
            "title": "bug",
            "state": "open",
            "user": {"login": "bia"},
            "labels": [{"name": "bug"}, {"name": "frontend"}],
            "comments": 2,
            "html_url": "u",
        }
    ]
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": json.dumps(payload)})

    response = await github.list_issues(_request(), state="open", workspace_id=None)

    assert response.state == "loaded"
    assert response.status == "ok"
    assert response.issues == response.items
    assert response.items[0].labels == ["bug", "frontend"]
    assert response.items[0].comments == 2
    assert calls[0][0][2] == "search/issues"
    assert "q=repo:o/r is:issue state:open" in calls[0][0]


async def test_empty_repository_list_is_distinct_from_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(monkeypatch, {"status": "ok", "output": "[]"})

    response = await github.list_pulls(_request(), state="open", workspace_id=None)

    assert response.state == "empty"
    assert response.items == []
    assert response.status == "ok"
    assert response.pulls == []


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("gh not found in PATH", "unavailable"),
        ("To get started with GitHub CLI, please run: gh auth login", "unavailable"),
        ("GraphQL: Could not resolve to a Repository", "error"),
    ],
)
async def test_gh_failures_are_classified(
    monkeypatch: pytest.MonkeyPatch, message: str, expected: str
) -> None:
    _fake_gh(monkeypatch, {"status": "error", "message": message})

    response = await github.list_pulls(_request(), state="open", workspace_id=None)

    assert response.state == "unknown"
    assert response.error == message
    assert response.items == []
    assert response.status == expected
    assert response.message == message


async def test_invalid_gh_output_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(monkeypatch, {"status": "ok", "output": "not json"})

    response = await github.list_issues(_request(), state="open", workspace_id=None)

    assert response.state == "unknown"
    assert response.code == "invalid_response"


async def test_requires_an_authenticated_user() -> None:
    with pytest.raises(HTTPException) as error:
        await github.list_pulls(_request(None), state="open", workspace_id=None)

    assert error.value.status_code == 401


async def test_rate_gate_stops_repeated_github_cli_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def rate_limited(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "status": "error",
            "code": "rate_limited",
            "message": "rate limit exceeded",
            "reset_at": int(github.time.time()) + 120,
        }

    monkeypatch.setattr(github, "_gh_run", rate_limited)
    monkeypatch.setattr(github, "_workspace_cwd", lambda _w: "/tmp/rate-limit")

    first = await github._gh_read(["pr", "list"], "/tmp/rate-limit", "rate-user")
    second = await github._gh_read(["issue", "list"], "/tmp/rate-limit", "rate-user")

    assert calls == 1
    assert first["code"] == second["code"] == "rate_limited"
    assert second["reset_at"] == first["reset_at"]


PULL_VIEW = {
    "number": 7,
    "title": "feat: algo",
    "state": "OPEN",
    "isDraft": False,
    "author": {"login": "ana"},
    "headRefName": "feat/x",
    "baseRefName": "master",
    "createdAt": "2026-11-19T10:00:00Z",
    "updatedAt": "2026-11-19T11:00:00Z",
    "url": "https://github.com/o/r/pull/7",
    "body": "Closes #3",
    "labels": [{"name": "bug"}, {"name": "frontend"}],
    "assignees": [{"login": "ana"}],
    "reviewRequests": [{"login": "bia"}, {"name": "time-ui"}],
    "milestone": {"title": "0.2"},
    "additions": 56,
    "deletions": 32,
    "changedFiles": 2,
    "mergeable": "MERGEABLE",
    "reviewDecision": "APPROVED",
    "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}],
    "comments": [
        {
            "author": {"login": "bia"},
            "body": "Boa!",
            "createdAt": "2026-11-19T12:00:00Z",
            "url": "https://github.com/o/r/pull/7#c1",
        }
    ],
    "commits": [
        {
            "oid": "abc1234",
            "messageHeadline": "feat: algo",
            "authors": [{"login": "ana", "name": "Ana"}],
            "committedDate": "2026-11-19T10:30:00Z",
        }
    ],
    "files": [{"path": "a.ts", "additions": 30, "deletions": 0}],
}


async def test_pull_detail_returns_body_comments_commits_and_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": json.dumps(PULL_VIEW)})

    response = await github.get_pull(_request(), 7, "ws1")

    assert calls[0][0][:3] == ["pr", "view", "7"]
    pull = response.pull
    assert response.status == "ok" and pull is not None
    assert (pull.title, pull.author, pull.head_ref, pull.base_ref) == (
        "feat: algo",
        "ana",
        "feat/x",
        "master",
    )
    assert pull.labels == ["bug", "frontend"]
    assert pull.reviewers == ["bia", "time-ui"]
    assert pull.milestone == "0.2"
    assert (pull.additions, pull.deletions, pull.changed_files) == (56, 32, 2)
    assert (pull.checks.passed, pull.checks.failed) == (1, 1)
    assert [c.author for c in pull.comments] == ["bia"]
    assert pull.commits[0].sha == "abc1234"
    assert pull.commits[0].author == "ana"
    assert [(f.path, f.additions) for f in pull.files] == [("a.ts", 30)]


async def test_pull_detail_survives_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(
        monkeypatch,
        {
            "status": "ok",
            "output": json.dumps({"number": 9, "title": "x", "state": "OPEN"}),
        },
    )

    response = await github.get_pull(_request(), 9, None)

    assert response.pull is not None
    assert response.pull.body == ""
    assert response.pull.labels == []
    assert response.pull.comments == []


async def test_pull_detail_reports_gh_unavailable_not_an_empty_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(monkeypatch, {"status": "error", "message": "gh not found in PATH"})

    response = await github.get_pull(_request(), 7, None)

    assert response.status == "unavailable"
    assert response.pull is None


@pytest.mark.parametrize("output", ["", "nope", "[]", '{"title": "sem numero"}'])
async def test_pull_detail_rejects_invalid_payloads(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    _fake_gh(monkeypatch, {"status": "ok", "output": output})

    response = await github.get_pull(_request(), 7, None)

    assert response.status == "error"
    assert response.pull is None


async def test_pull_detail_requires_an_authenticated_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gh(monkeypatch, {"status": "ok", "output": "{}"})

    with pytest.raises(HTTPException) as error:
        await github.get_pull(_request(None), 7, None)

    assert error.value.status_code == 401


async def test_issue_detail_returns_body_labels_and_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "number": 3,
        "title": "bug no chat",
        "state": "OPEN",
        "author": {"login": "ana"},
        "createdAt": "2026-11-19T10:00:00Z",
        "updatedAt": "2026-11-19T11:00:00Z",
        "url": "https://github.com/o/r/issues/3",
        "body": "## Objetivo",
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "bia"}],
        "milestone": None,
        "comments": [{"author": {"login": "bia"}, "body": "ok", "createdAt": "x"}],
    }
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": json.dumps(payload)})

    response = await github.get_issue(_request(), 3, "ws1")

    assert calls[0][0][:3] == ["issue", "view", "3"]
    issue = response.issue
    assert response.status == "ok" and issue is not None
    assert (issue.title, issue.author, issue.labels, issue.assignees) == (
        "bug no chat",
        "ana",
        ["bug"],
        ["bia"],
    )
    assert issue.milestone == ""
    assert [c.body for c in issue.comments] == ["ok"]


async def test_issue_detail_reports_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(monkeypatch, {"status": "error", "message": "could not resolve"})

    response = await github.get_issue(_request(), 3, None)

    assert response.status == "error"
    assert response.issue is None


def test_comment_list_is_capped() -> None:
    many: list[JsonValue] = [
        {"author": {"login": "a"}, "body": str(i)} for i in range(500)
    ]

    assert len(github._comments(many)) == 100


# --- cache curto das leituras do gh -----------------------------------------


@pytest.mark.asyncio
async def test_leitura_repetida_usa_o_cache_e_chama_o_gh_uma_vez(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": "[]"})

    first = await github._gh_read(["pr", "list"], cwd="/w", user_id="u1")
    second = await github._gh_read(["pr", "list"], cwd="/w", user_id="u1")

    assert first == second
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_nao_mistura_usuarios_nem_argumentos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": "[]"})

    await github._gh_read(["pr", "list"], cwd="/w", user_id="u1")
    await github._gh_read(["pr", "list"], cwd="/w", user_id="u2")
    await github._gh_read(["issue", "list"], cwd="/w", user_id="u1")

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_erro_nao_fica_em_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_gh(monkeypatch, {"status": "error", "message": "boom"})

    await github._gh_read(["pr", "list"], cwd="/w", user_id="u1")
    await github._gh_read(["pr", "list"], cwd="/w", user_id="u1")

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_chamadas_simultaneas_iguais_compartilham_uma_execucao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    started = 0

    async def slow(args, cwd=None, input_data=None, user_id=None):
        nonlocal started
        started += 1
        await asyncio.sleep(0.01)
        return {"status": "ok", "output": "[]"}

    monkeypatch.setattr(github, "_gh_run", slow)

    await asyncio.gather(
        *(github._gh_read(["pr", "list"], cwd="/w", user_id="u1") for _ in range(5))
    )

    assert started == 1


@pytest.mark.asyncio
async def test_cache_expira_depois_do_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_gh(monkeypatch, {"status": "ok", "output": "[]"})
    clock = [100.0]
    monkeypatch.setattr(github.time, "monotonic", lambda: clock[0])

    await github._gh_read(["pr", "list"], cwd="/w", user_id="u1")
    clock[0] += github._GH_CACHE_TTL_S + 1
    await github._gh_read(["pr", "list"], cwd="/w", user_id="u1")

    assert len(calls) == 2


def test_failure_uses_structured_gh_unavailable_code() -> None:
    assert github._failure({"code": "gh_unavailable", "message": "gh missing"}) == (
        "unavailable",
        "gh missing",
    )


async def test_github_list_sends_etag_and_reuses_body_on_not_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    responses = [
        {
            "status": "ok",
            "http_status": 200,
            "etag": '"v1"',
            "output": 'HTTP/2.0 200 OK\r\nETag: "v1"\r\n\r\n[{"number": 4}]',
        },
        {
            "status": "error",
            "code": "gh_command_failed",
            "http_status": 304,
            "output": "HTTP/2.0 304 Not Modified",
        },
    ]

    async def fake(args, cwd=None, input_data=None, user_id=None):
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(github, "_gh_run", fake)
    monkeypatch.setattr(github, "_github_repository", lambda _cwd: "o/r")
    monkeypatch.setattr(github, "github_auth_epoch", _epoch)

    first, first_error = await github._github_api_list("pulls", "open", "/ws", "alice")
    second, second_error = await github._github_api_list(
        "pulls", "open", "/ws", "alice"
    )

    assert first_error is second_error is None
    assert first == second == [{"number": 4}]
    assert "--header" not in calls[0]
    assert calls[1][-2:] == ["--header", 'If-None-Match: "v1"']


async def test_github_etag_cache_is_isolated_by_credential_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epochs = iter(["first", "second"])
    calls: list[list[str]] = []

    async def changing_epoch(_user_id):
        return next(epochs)

    async def fake(args, cwd=None, input_data=None, user_id=None):
        calls.append(args)
        return {
            "status": "ok",
            "etag": '"v1"',
            "output": '[{"number": 4}]',
        }

    monkeypatch.setattr(github, "_gh_run", fake)
    monkeypatch.setattr(github, "_github_repository", lambda _cwd: "o/r")
    monkeypatch.setattr(github, "github_auth_epoch", changing_epoch)

    await github._github_api_list("pulls", "open", "/ws", "alice")
    await github._github_api_list("pulls", "open", "/ws", "alice")

    assert all("--header" not in args for args in calls)


async def test_github_api_rate_gate_uses_reset_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def limited(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "status": "error",
            "code": "rate_limited",
            "message": "API rate limit exceeded",
            "reset_at": int(github.time.time()) + 180,
        }

    monkeypatch.setattr(github, "_gh_run", limited)
    monkeypatch.setattr(github, "_github_repository", lambda _cwd: "o/r")
    monkeypatch.setattr(github, "github_auth_epoch", _epoch)

    _items, first_error = await github._github_api_list("pulls", "open", "/ws", "alice")
    _items, second_error = await github._github_api_list(
        "issues", "open", "/ws", "alice"
    )

    assert calls == 1
    assert first_error is not None and second_error is not None
    assert first_error["reset_at"] == second_error["reset_at"]
