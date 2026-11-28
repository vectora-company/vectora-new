"""Contrato das rotas do GitHub com o ``gh`` REAL, só leitura, neste repositório.

Fica fora da suíte padrão pelo marker ``live`` (como ``test_tools_github_live``).
Roda quando o ``gh`` está instalado e autenticado e o repositório do checkout é
``vectora-company/vectora``; fora disso o teste é pulado, nunca falha por falta
de ambiente.

Restrições, aplicadas por um guarda em ``_gh_run`` (não por convenção):

* só subcomandos de leitura (``run list|view``, ``workflow list``, ``pr list|view``,
  ``issue list|view``); qualquer outra coisa reprova o teste na hora;
* sempre dentro deste repositório (``cwd`` é a raiz do checkout e o remoto é
  conferido antes);
* poucas chamadas por teste, para não consumir a cota da API.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api.handlers import github

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_REPO = "vectora-company/vectora"
READ_ONLY = {
    ("run", "list"),
    ("run", "view"),
    ("workflow", "list"),
    ("pr", "list"),
    ("pr", "view"),
    ("issue", "list"),
    ("issue", "view"),
}


def _request():
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id="live")))


@pytest.fixture(scope="module")
def own_repo() -> str:
    """Pula o módulo inteiro se o ambiente não é este repositório com gh logado."""
    gh_path = shutil.which("gh")
    if gh_path is None:
        pytest.skip("gh não está instalado")
    try:
        # Comando fixo e só leitura, com o binário já resolvido por `which`.
        result = subprocess.run(  # noqa: S603
            [
                gh_path,
                "repo",
                "view",
                "--json",
                "nameWithOwner",
                "--jq",
                ".nameWithOwner",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("não foi possível consultar o gh")
    name = result.stdout.strip()
    if result.returncode != 0 or not name:
        pytest.skip("gh não autenticado ou sem repositório remoto neste checkout")
    if name != EXPECTED_REPO:
        pytest.skip(f"este teste só roda em {EXPECTED_REPO} (checkout é {name})")
    return name


@pytest.fixture
def real_gh(own_repo: str, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Usa o gh de verdade, mas reprova qualquer chamada que não seja de leitura."""
    calls: list[list[str]] = []
    real = github._gh_run

    async def guarded(args, cwd=None, input_data=None, user_id=None):
        assert tuple(args[:2]) in READ_ONLY, f"gh fora da leitura: {args}"
        assert input_data is None, "leitura não envia corpo"
        assert Path(cwd or "").resolve() == ROOT, f"fora do repositório: {cwd}"
        calls.append(list(args))
        return await real(args, cwd=cwd, input_data=input_data, user_id=None)

    monkeypatch.setattr(github, "_gh_run", guarded)
    monkeypatch.setattr(github, "_workspace_cwd", lambda _w: str(ROOT))
    return calls


async def _first_completed_run() -> github.RunSummary:
    response = await github.list_runs(_request(), "completed", None, None, None)
    if response.status != "ok" or not response.runs:
        pytest.skip("o repositório não tem runs concluídos para inspecionar")
    return response.runs[0]


async def test_run_list_returns_real_runs(real_gh: list[list[str]]) -> None:
    response = await github.list_runs(_request(), None, None, None, "ws")

    assert response.status == "ok"
    assert response.runs, "o repositório deveria ter runs"
    for run in response.runs:
        assert run.id > 0 and run.title and run.workflow
        assert run.status in {
            "queued",
            "in_progress",
            "completed",
            "waiting",
            "requested",
            "pending",
        }
        if run.status != "completed":
            assert run.conclusion == ""
    assert real_gh[0][:2] == ["run", "list"]


async def test_run_list_filters_reach_gh_and_narrow_the_result(
    real_gh: list[list[str]],
) -> None:
    response = await github.list_runs(_request(), "completed", "master", None, None)

    assert response.status == "ok"
    assert all(run.status == "completed" for run in response.runs)
    assert all(run.branch == "master" for run in response.runs)
    args = real_gh[0]
    assert args[args.index("--status") + 1] == "completed"
    assert args[args.index("--branch") + 1] == "master"


async def test_run_detail_matches_the_listed_run_and_has_real_jobs(
    real_gh: list[list[str]],
) -> None:
    run = await _first_completed_run()

    detail = await github.get_run(_request(), run.id, None)

    assert detail.status == "ok" and detail.run is not None
    assert detail.run.id == run.id and detail.run.title == run.title
    assert detail.run.jobs, "um run concluído tem jobs"
    for job in detail.run.jobs:
        assert job.id > 0 and job.name and job.url.startswith("https://github.com/")
        if job.steps:
            assert [s.number for s in job.steps] == sorted(s.number for s in job.steps)


async def test_job_log_of_a_finished_job_is_real_text(real_gh: list[list[str]]) -> None:
    run = await _first_completed_run()
    detail = await github.get_run(_request(), run.id, None)
    assert detail.run is not None
    job = next(
        (j for j in detail.run.jobs if j.steps and j.conclusion != "skipped"), None
    )
    if job is None:
        pytest.skip("nenhum job com passos no run escolhido")

    response = await github.get_job_log(_request(), run.id, job.id, None)

    assert response.status == "ok"
    assert response.log.strip()
    assert len(response.log) <= github._MAX_LOG_CHARS
    assert real_gh[-1][:4] == ["run", "view", str(run.id), "--job"]


async def test_workflows_are_real(real_gh: list[list[str]]) -> None:
    response = await github.list_workflows(_request(), None)

    assert response.status == "ok"
    assert response.workflows and all(w.name for w in response.workflows)


async def test_pull_and_issue_detail_match_the_lists(real_gh: list[list[str]]) -> None:
    pulls = await github.list_pulls(_request(), "all", None)
    issues = await github.list_issues(_request(), "all", None)
    assert pulls.status == "ok" and pulls.pulls
    assert issues.status == "ok" and issues.issues

    pull = await github.get_pull(_request(), pulls.pulls[0].number, None)
    issue = await github.get_issue(_request(), issues.issues[0].number, None)

    assert pull.pull is not None and pull.pull.title == pulls.pulls[0].title
    assert issue.issue is not None and issue.issue.title == issues.issues[0].title


async def test_everything_the_tests_asked_of_gh_was_read_only(
    real_gh: list[list[str]],
) -> None:
    await github.list_runs(_request(), None, None, None, None)
    await github.list_workflows(_request(), None)
    await github.list_pulls(_request(), "open", None)

    assert real_gh
    assert all(tuple(call[:2]) in READ_ONLY for call in real_gh)


# ----- negativos reais -------------------------------------------------------


async def test_unknown_run_id_is_an_error_not_an_empty_run(
    real_gh: list[list[str]],
) -> None:
    response = await github.get_run(_request(), 1, None)

    assert response.status in {"error", "unavailable"}
    assert response.run is None


async def test_invalid_status_is_rejected_before_calling_gh(
    real_gh: list[list[str]],
) -> None:
    with pytest.raises(HTTPException) as error:
        await github.list_runs(_request(), "--repo=outro/repo", None, None, None)

    assert error.value.status_code == 422
    assert real_gh == []


async def test_without_gh_on_the_path_the_routes_say_unavailable(
    own_repo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "_workspace_cwd", lambda _w: str(ROOT))
    monkeypatch.setenv("PATH", str(tmp_path))

    runs = await github.list_runs(_request(), None, None, None, None)
    detail = await github.get_run(_request(), 1, None)

    assert runs.status == "unavailable" and runs.runs == []
    assert detail.status == "unavailable" and detail.run is None
