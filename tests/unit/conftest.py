"""Fixtures mínimas compartilhadas — KISS."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def _no_nats_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutraliza o sidecar NATS: ``get_kv``/``get_mq`` caem no fallback de memória.

    Sem isso, todo teste que assume ``MemoryKV``/``MemoryMQ`` passa a subir o
    ``nats-server`` real assim que o binário existe em ``resources/`` (após
    ``scons install``) — deixando a suíte dependente da presença do binário e
    compartilhando o store JetStream em ``~/.vectora/nats`` entre os testes.

    Testes que querem o ``NatsKV``/``NatsMQ`` de verdade ou re-mockam
    ``ensure_nats_sidecar`` no próprio corpo (o último ``setattr`` vence) ou
    vivem em ``test_nats_sidecar.py`` / ``test_nats_mq_kv_real.py`` (que não
    dependem deste fixture).
    """
    from backend.scheduling import nats_sidecar

    monkeypatch.setattr(
        nats_sidecar, "ensure_nats_sidecar", AsyncMock(return_value=None)
    )


@pytest.fixture
def _no_thread_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutraliza ``threads._increment_message_count``.

    ``stream_engine_events()`` (native_stream.py) dispara essa chamada
    fire-and-forget no 1º ``TokenEvent`` de qualquer stream (ver
    ``_mark_thread_has_content``) —
    sem mock, abre uma conexão ``aiosqlite`` REAL a ``~/.vectora/checkpoints.db``.
    Testes que só exercitam semântica de streaming (não bookkeeping de sessão)
    vazavam essa conexão entre si — a task nunca é esperada, então o singleton
    ``_db_conn`` do módulo ``threads`` ficava preso a um event loop de um teste
    já encerrado, travando ``TestClient.__enter__`` de testes de lifespan bem
    mais adiante na suíte.
    """
    from backend.api.handlers import threads

    monkeypatch.setattr(
        threads, "_increment_message_count", AsyncMock(return_value=None)
    )


@pytest.fixture
def sem_bwrap_automatico(monkeypatch: pytest.MonkeyPatch) -> None:
    """Faz `parse_policy` ignorar o `bwrap` do host: os testes que esperam o
    sandbox desligado sem `vectora.toml` não podem depender de a máquina de
    quem roda ter ou não o binário instalado."""
    import backend.sandbox.policy as policy_module

    monkeypatch.setattr(policy_module, "_native_sandbox_available_sync", lambda: False)


@pytest.fixture(autouse=True)
def _limpa_cache_de_leituras_do_gh() -> Iterator[None]:
    """O handler do GitHub guarda leituras por alguns segundos; sem limpar, um
    teste veria a resposta simulada do anterior."""
    from backend.api.handlers import github

    github._gh_cache.clear()
    github._gh_inflight.clear()
    github._etag_cache.clear()
    github._rate_gate_until.clear()
    yield
    github._gh_cache.clear()
    github._gh_inflight.clear()
    github._etag_cache.clear()
    github._rate_gate_until.clear()


@pytest.fixture(autouse=True)
def _encerra_workers_do_jail() -> Iterator[None]:
    """Nenhum worker jailado sobrevive ao teste que o criou: o `jail_manager` é
    um singleton do processo, e um worker esquecido seguiria apontando para a
    pasta temporária de um teste anterior."""
    yield
    import contextlib

    from backend.sandbox.workspace_jail import jail_manager

    for worker in list(jail_manager._workers.values()):
        with contextlib.suppress(ProcessLookupError, OSError):
            worker.proc.kill()
    jail_manager._workers.clear()


@pytest.fixture(autouse=True)
def _git_com_ramo_padrao_master(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repositórios criados pelos testes nascem em `master`, qualquer que seja
    o `init.defaultBranch` da máquina de quem roda (os testes de git citam o
    ramo pelo nome)."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "master")


@pytest.fixture(autouse=True)
def _politica_do_sandbox_isolada(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A política do usuário (fora do workspace) nunca vem do `~/.vectora` de
    quem roda os testes: cada teste enxerga uma pasta vazia."""
    monkeypatch.setenv(
        "VECTORA_SANDBOX_POLICY_DIR", str(tmp_path_factory.mktemp("sandbox-policies"))
    )
    # A auditoria do sandbox também nunca escreve no `~/.vectora` de quem roda.
    monkeypatch.setenv(
        "VECTORA_SANDBOX_AUDIT_FILE",
        str(tmp_path_factory.mktemp("sandbox-audit") / "audit.jsonl"),
    )
