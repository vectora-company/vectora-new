"""Testes de `backend/tools/github.py` — saída de API do modelo de
referência PR review via webhook.

Cobre happy path + erro no mesmo teste: token ausente, resposta HTTP
não-2xx, e sucesso — para as duas tools (buscar diff, postar comentário).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.tools.github import github_fetch_pr_diff, github_post_pr_comment


def _mock_httpx(response: MagicMock) -> AsyncMock:
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_ctx.get = AsyncMock(return_value=response)
    mock_ctx.post = AsyncMock(return_value=response)
    return mock_ctx


class TestGithubFetchPrDiff:
    @pytest.mark.asyncio
    async def test_repositorio_publico_funciona_sem_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        mock_response = MagicMock(status_code=200, text="diff público")
        client = _mock_httpx(mock_response)
        with patch("httpx.AsyncClient", return_value=client):
            result = json.loads(
                await github_fetch_pr_diff(owner="vectora", repo="vectora", pr_number=1)
            )
        assert result == {"status": "ok", "diff": "diff público"}
        assert "Authorization" not in client.get.call_args.kwargs["headers"]

    @pytest.mark.asyncio
    async def test_token_do_usuario_tem_precedencia(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "token-global")
        mock_response = MagicMock(status_code=200, text="diff autenticado")
        client = _mock_httpx(mock_response)
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch(
                "backend.rbac.auth.get_env_overrides",
                new=AsyncMock(return_value={"GITHUB_TOKEN": "token-do-usuario"}),
            ),
        ):
            result = json.loads(
                await github_fetch_pr_diff(
                    owner="vectora",
                    repo="vectora",
                    pr_number=1,
                    ctx=MagicMock(user_id="user-1"),
                )
            )
        assert result["status"] == "ok"
        assert (
            client.get.call_args.kwargs["headers"]["Authorization"]
            == "Bearer token-do-usuario"
        )

    @pytest.mark.asyncio
    async def test_sucesso_devolve_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "diff --git a/foo.py b/foo.py\n+print('oi')\n"

        with patch("httpx.AsyncClient", return_value=_mock_httpx(mock_response)):
            result = json.loads(
                await github_fetch_pr_diff(
                    owner="vectora", repo="vectora", pr_number=42
                )
            )

        assert result["status"] == "ok"
        assert "print('oi')" in result["diff"]

    @pytest.mark.asyncio
    async def test_pr_inexistente_retorna_erro_com_status_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        with patch("httpx.AsyncClient", return_value=_mock_httpx(mock_response)):
            result = json.loads(
                await github_fetch_pr_diff(
                    owner="vectora", repo="vectora", pr_number=999
                )
            )

        assert result["status"] == "error"
        assert "404" in result["error"]

    @pytest.mark.asyncio
    async def test_rate_limit_publico_e_identificado_sem_confundir_permissao(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        rate_limited = MagicMock(
            status_code=403,
            text='{"message":"API rate limit exceeded"}',
            headers={"x-ratelimit-remaining": "0"},
        )
        forbidden = MagicMock(
            status_code=403,
            text='{"message":"Resource not accessible by integration"}',
            headers={"x-ratelimit-remaining": "4999"},
        )

        with patch(
            "httpx.AsyncClient",
            side_effect=[_mock_httpx(rate_limited), _mock_httpx(forbidden)],
        ):
            rate_limit_result = json.loads(
                await github_fetch_pr_diff(owner="vectora", repo="vectora", pr_number=1)
            )
            permission_result = json.loads(
                await github_fetch_pr_diff(owner="vectora", repo="vectora", pr_number=1)
            )

        assert rate_limit_result["status"] == "error"
        assert rate_limit_result["rate_limited"] is True
        assert permission_result["status"] == "error"
        assert "rate_limited" not in permission_result

    @pytest.mark.asyncio
    async def test_diff_grande_demais_e_montado_pela_lista_de_arquivos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        too_large = MagicMock(
            status_code=406, text='{"errors":[{"code":"too_large"}]}', headers={}
        )
        first = MagicMock(status_code=200)
        first.json.return_value = [
            {"filename": f"src/{n}.py", "patch": f"@@ -0,0 +1 @@\n+{n}"}
            for n in range(100)
        ]
        second = MagicMock(status_code=200)
        second.json.return_value = [
            {"filename": "novo.png", "previous_filename": "antigo.png"}
        ]
        client = _mock_httpx(too_large)
        client.get = AsyncMock(side_effect=[too_large, first, second])

        with patch("httpx.AsyncClient", return_value=client):
            result = json.loads(
                await github_fetch_pr_diff(owner="vectora", repo="vectora", pr_number=7)
            )

        assert result["status"] == "ok"
        assert result["truncated"] is False
        assert "diff --git a/src/0.py b/src/0.py\n@@ -0,0 +1 @@\n+0" in result["diff"]
        assert "diff --git a/antigo.png b/novo.png" in result["diff"]
        assert client.get.call_args.kwargs["params"] == {"per_page": 100, "page": 2}

    @pytest.mark.asyncio
    async def test_lista_de_arquivos_e_limitada_e_erro_e_informado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        too_large = MagicMock(
            status_code=406, text='{"errors":[{"code":"too_large"}]}', headers={}
        )
        full = MagicMock(status_code=200)
        full.json.return_value = [{"filename": "a.py", "patch": "+a"}] * 100
        failed = MagicMock(status_code=500, text="falha", headers={})
        client = _mock_httpx(too_large)
        client.get = AsyncMock(side_effect=[too_large, full, full, full])
        with patch("httpx.AsyncClient", return_value=client):
            limited = json.loads(
                await github_fetch_pr_diff(owner="vectora", repo="vectora", pr_number=7)
            )
        client.get = AsyncMock(side_effect=[too_large, failed])
        with patch("httpx.AsyncClient", return_value=client):
            error = json.loads(
                await github_fetch_pr_diff(owner="vectora", repo="vectora", pr_number=7)
            )

        assert limited["truncated"] is True
        assert error["status"] == "error"
        assert "500" in error["error"]

    @pytest.mark.asyncio
    async def test_erro_de_rede_nunca_propaga(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(side_effect=ConnectionError("dns falhou"))

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = json.loads(
                await github_fetch_pr_diff(owner="vectora", repo="vectora", pr_number=1)
            )

        assert result["status"] == "error"
        assert "dns falhou" in result["error"]


class TestGithubPostPrComment:
    @pytest.mark.asyncio
    async def test_sem_token_retorna_erro(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        result = json.loads(
            await github_post_pr_comment(
                owner="vectora", repo="vectora", pr_number=1, body="LGTM"
            )
        )
        assert result["status"] == "error"
        assert "GITHUB_TOKEN" in result["error"]

    @pytest.mark.asyncio
    async def test_sucesso_devolve_url_do_comentario(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "html_url": "https://github.com/vectora/vectora/pull/42#issuecomment-1"
        }

        with patch("httpx.AsyncClient", return_value=_mock_httpx(mock_response)):
            result = json.loads(
                await github_post_pr_comment(
                    owner="vectora",
                    repo="vectora",
                    pr_number=42,
                    body="Revisado — sem observações.",
                )
            )

        assert result["status"] == "ok"
        assert "issuecomment-1" in result["comment_url"]

    @pytest.mark.asyncio
    async def test_token_sem_escopo_retorna_erro_com_status_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_sem_escopo")
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Resource not accessible by integration"

        with patch("httpx.AsyncClient", return_value=_mock_httpx(mock_response)):
            result = json.loads(
                await github_post_pr_comment(
                    owner="vectora", repo="vectora", pr_number=1, body="x"
                )
            )

        assert result["status"] == "error"
        assert "403" in result["error"]
