"""Handlers do GitHub contra saídas REAIS do ``gh`` (somente leitura, deste repositório).

As fixtures em ``tests/fixtures/github/gh_*`` foram capturadas do ``gh`` real
(``gh run list``, ``gh run view``, ``gh pr view`` etc.) neste mesmo repositório,
com e-mails removidos. Elas são a base das simulações: o que os testes e o
Playwright usam não é um formato imaginado, é o que o ``gh`` devolve de verdade.

Duas garantias:

* o backend entende o formato real (inclusive casos incômodos: run em andamento
  sem conclusão, jobs ``skipped`` sem passos, log com BOM e prefixo por linha);
* as respostas em ``frontend/e2e/fixtures/github`` (usadas pelo frontend e pelo
  e2e) são exatamente o que o backend produz a partir das fixtures reais, então
  as simulações do frontend não envelhecem em silêncio.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.api.handlers import github
from tests.fake_requests import fake_request

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "tests" / "fixtures" / "github"
API = ROOT / "frontend" / "e2e" / "fixtures" / "github"

# Só leitura: nenhum teste deste arquivo pode pedir outra coisa ao gh.
READ_ONLY = {
    ("run", "list"),
    ("run", "view"),
    ("workflow", "list"),
    ("pr", "list"),
    ("pr", "view"),
    ("issue", "list"),
    ("issue", "view"),
    ("api", "--include"),
}


def _raw(name: str) -> str:
    return (RAW / name).read_text(encoding="utf-8")


def _api(name: str) -> dict:
    return json.loads((API / name).read_text(encoding="utf-8"))


def _request() -> MagicMock:
    return fake_request(state=SimpleNamespace(user=SimpleNamespace(id="alice")))


@pytest.fixture
def gh_output(monkeypatch: pytest.MonkeyPatch):
    """Devolve a saída real escolhida e registra cada chamada ao gh."""
    calls: list[list[str]] = []
    state = {"output": ""}

    async def fake(
        args: list[str],
        cwd: str | None = None,
        input_data=None,
        user_id: str | None = None,
    ) -> dict[str, str]:
        calls.append(list(args))
        assert tuple(args[:2]) in READ_ONLY, f"operação fora da leitura: {args}"
        if args[:2] == ["api", "--include"]:
            endpoint = args[2].rsplit("/", 1)[-1]
            fixture = "gh_pr_list.json" if endpoint == "pulls" else "gh_issue_list.json"
            raw_items = json.loads(_raw(fixture))
            output_items = []
            for item in raw_items:
                if endpoint == "pulls":
                    output_items.append(
                        {
                            "number": item["number"],
                            "title": item.get("title", ""),
                            "state": str(item.get("state", "")).lower(),
                            "draft": item.get("isDraft", False),
                            "user": item.get("author", {}),
                            "head": {"ref": item.get("headRefName", "")},
                            "base": {"ref": item.get("baseRefName", "")},
                            "updated_at": item.get("updatedAt", ""),
                            "html_url": item.get("url", ""),
                            "merged_at": item.get("mergedAt"),
                        }
                    )
                elif "pullRequest" not in item:
                    output_items.append(
                        {
                            "number": item["number"],
                            "title": item.get("title", ""),
                            "state": str(item.get("state", "")).lower(),
                            "user": item.get("author", {}),
                            "labels": item.get("labels", []),
                            "updated_at": item.get("updatedAt", ""),
                            "html_url": item.get("url", ""),
                            "comments": len(item.get("comments", [])),
                        }
                    )
            return {"status": "ok", "output": json.dumps(output_items)}
        return {"status": "ok", "output": state["output"]}

    monkeypatch.setattr(github, "_gh_run", fake)
    monkeypatch.setattr(github, "_workspace_cwd", lambda _w: str(ROOT))

    def use(name: str) -> list[list[str]]:
        state["output"] = _raw(name)
        return calls

    return use


# ---------------------------------------------------------------------------
# O backend entende o formato real
# ---------------------------------------------------------------------------


async def test_run_list_real_has_a_finished_and_an_in_progress_run(gh_output) -> None:
    gh_output("gh_run_list.json")

    response = await github.list_runs(_request(), None, None, None, None)

    assert response.status == "ok"
    by_status = {r.status: r for r in response.runs}
    assert by_status["completed"].conclusion == "success"
    # Run em andamento: o gh devolve conclusão vazia, nunca nula.
    assert by_status["in_progress"].conclusion == ""
    assert all(r.id > 0 and r.branch and r.sha for r in response.runs)


async def test_run_detail_real_maps_jobs_and_steps(gh_output) -> None:
    gh_output("gh_run_view.json")
    run_id = json.loads(_raw("gh_run_view.json"))["databaseId"]

    response = await github.get_run(_request(), run_id, None)

    assert response.status == "ok" and response.run is not None
    run = response.run
    assert (run.status, run.conclusion, run.attempt) == ("completed", "success", 1)
    assert len(run.jobs) == 5
    with_steps = [job for job in run.jobs if job.steps]
    assert with_steps
    for job in with_steps:
        numbers = [step.number for step in job.steps]
        assert numbers == sorted(numbers) and numbers[0] == 1
        assert all(step.name for step in job.steps)
        assert all(job.url.startswith("https://github.com/") for job in [job])


async def test_run_detail_real_skipped_jobs_have_no_steps_and_do_not_break(
    gh_output,
) -> None:
    gh_output("gh_run_view.json")
    run_id = json.loads(_raw("gh_run_view.json"))["databaseId"]

    response = await github.get_run(_request(), run_id, None)

    assert response.run is not None
    skipped = [job for job in response.run.jobs if job.conclusion == "skipped"]
    assert skipped and all(job.steps == [] for job in skipped)


async def test_workflows_real_are_listed(gh_output) -> None:
    gh_output("gh_workflow_list.json")

    response = await github.list_workflows(_request(), None)

    assert response.status == "ok"
    assert response.workflows and all(w.id > 0 and w.name for w in response.workflows)


async def test_job_log_real_keeps_the_text_as_gh_prints_it(gh_output) -> None:
    gh_output("gh_run_view_job_log.txt")

    response = await github.get_job_log(_request(), 1, 2, None)

    assert response.status == "ok" and response.truncated is False
    assert response.log == _raw("gh_run_view_job_log.txt")
    first = response.log.splitlines()[0]
    # O log real vem com BOM e prefixado por "job<TAB>passo<TAB>hora texto".
    assert first.startswith("﻿") or first.count("\t") >= 2
    assert all(line.count("\t") >= 2 for line in response.log.splitlines() if line)


async def test_pull_list_real_without_checks_payload(gh_output) -> None:
    gh_output("gh_pr_list.json")

    response = await github.list_pulls(_request(), "all", None)

    assert response.state == "loaded" and response.items
    pull = response.items[0]
    assert pull.author and pull.head_ref and pull.base_ref
    # A lista não pede o rollup de checks (payload pesado); só o detalhe pede.
    assert (
        pull.checks.passed == 0 and pull.checks.failed == 0 and pull.checks.pending == 0
    )


async def test_pull_detail_real_maps_commits_files_comments_and_milestone(
    gh_output,
) -> None:
    gh_output("gh_pr_view.json")
    number = json.loads(_raw("gh_pr_view.json"))["number"]

    response = await github.get_pull(_request(), number, None)

    assert response.status == "ok" and response.pull is not None
    pull = response.pull
    assert pull.number == number
    assert pull.commits and pull.commits[0].sha and pull.commits[0].author
    assert pull.files and pull.files[0].path
    assert pull.comments and pull.comments[0].author
    assert pull.milestone
    assert pull.mergeable in {"MERGEABLE", "CONFLICTING", "UNKNOWN"}
    assert pull.checks.passed > 0


async def test_issue_list_and_detail_real(gh_output) -> None:
    gh_output("gh_issue_list.json")
    listed = await github.list_issues(_request(), "all", None)
    assert listed.state == "loaded" and listed.items
    assert listed.items[0].author

    gh_output("gh_issue_view.json")
    number = json.loads(_raw("gh_issue_view.json"))["number"]
    detail = await github.get_issue(_request(), number, None)

    assert detail.status == "ok" and detail.issue is not None
    assert detail.issue.body and detail.issue.labels
    assert detail.issue.milestone


# ---------------------------------------------------------------------------
# As simulações do frontend e do e2e não envelhecem
# ---------------------------------------------------------------------------


async def _compare(gh_output, raw_name: str, api_name: str, call) -> None:
    gh_output(raw_name)
    response = await call()
    assert response.model_dump(mode="json") == _api(api_name), (
        f"{api_name} desatualizado em relação a {raw_name}: gere de novo a "
        "partir do handler"
    )


async def test_frontend_fixtures_match_what_the_backend_produces(gh_output) -> None:
    run_view = json.loads(_raw("gh_run_view.json"))
    job_id = run_view["jobs"][0]["databaseId"]
    pull_number = json.loads(_raw("gh_pr_view.json"))["number"]
    issue_number = json.loads(_raw("gh_issue_view.json"))["number"]

    await _compare(
        gh_output,
        "gh_run_list.json",
        "runs.json",
        lambda: github.list_runs(_request(), None, None, None, None),
    )
    await _compare(
        gh_output,
        "gh_run_view.json",
        "run-detail.json",
        lambda: github.get_run(_request(), run_view["databaseId"], None),
    )
    await _compare(
        gh_output,
        "gh_workflow_list.json",
        "workflows.json",
        lambda: github.list_workflows(_request(), None),
    )
    await _compare(
        gh_output,
        "gh_run_view_job_log.txt",
        "job-log.json",
        lambda: github.get_job_log(_request(), run_view["databaseId"], job_id, None),
    )
    await _compare(
        gh_output,
        "gh_pr_list.json",
        "pulls.json",
        lambda: github.list_pulls(_request(), "all", None),
    )
    await _compare(
        gh_output,
        "gh_pr_view.json",
        "pull-detail.json",
        lambda: github.get_pull(_request(), pull_number, None),
    )
    await _compare(
        gh_output,
        "gh_issue_list.json",
        "issues.json",
        lambda: github.list_issues(_request(), "all", None),
    )
    await _compare(
        gh_output,
        "gh_issue_view.json",
        "issue-detail.json",
        lambda: github.get_issue(_request(), issue_number, None),
    )


def test_every_raw_fixture_has_an_api_counterpart() -> None:
    assert sorted(p.name for p in API.glob("*.json")) == [
        "issue-detail.json",
        "issues.json",
        "job-log.json",
        "pull-detail.json",
        "pulls.json",
        "run-detail.json",
        "runs.json",
        "workflows.json",
    ]


# ---------------------------------------------------------------------------
# Privacidade das fixtures
# ---------------------------------------------------------------------------


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@pytest.mark.parametrize(
    "path",
    sorted(RAW.glob("gh_*")) + sorted(API.glob("*.json")),
    ids=lambda p: p.name,
)
def test_fixtures_never_contain_an_email_address(path: Path) -> None:
    assert _EMAIL.findall(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", sorted(RAW.glob("gh_*.json")), ids=lambda p: p.name)
def test_raw_fixtures_are_valid_json(path: Path) -> None:
    assert json.loads(path.read_text(encoding="utf-8")) is not None


# ---------------------------------------------------------------------------
# Negativos com o formato real
# ---------------------------------------------------------------------------


async def test_truncated_real_output_is_an_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = _raw("gh_run_view.json")[:200]

    async def fake(
        args: list[str],
        cwd: str | None = None,
        input_data=None,
        user_id: str | None = None,
    ) -> dict[str, str]:
        return {"status": "ok", "output": broken}

    monkeypatch.setattr(github, "_gh_run", fake)
    monkeypatch.setattr(github, "_workspace_cwd", lambda _w: str(ROOT))

    response = await github.get_run(_request(), 1, None)

    assert response.status == "error" and response.run is None


async def test_a_run_list_payload_is_not_accepted_as_a_run_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(
        args: list[str],
        cwd: str | None = None,
        input_data=None,
        user_id: str | None = None,
    ) -> dict[str, str]:
        return {"status": "ok", "output": _raw("gh_run_list.json")}

    monkeypatch.setattr(github, "_gh_run", fake)
    monkeypatch.setattr(github, "_workspace_cwd", lambda _w: str(ROOT))

    response = await github.get_run(_request(), 1, None)

    assert response.status == "error"
