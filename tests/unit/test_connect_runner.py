"""Testes para backend/services/connect/runner.py.

`run_agent_for_thread` é o ponto de entrada único que os 4 adapters
(Telegram/Discord/Slack/Email) usam pra rodar uma mensagem externa através
do motor nativo (`backend/engine/conversation_loop.py`) — mesmo caminho que
o chat web usa, sob o usuário "local" e `permission_mode="auto"` (mensageria
externa não tem UI de aprovar/rejeitar tool call).
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.engine.hitl import ApprovalGate
from backend.engine.subagents import SubagentSpec
from backend.persistence.native.session_store import SessionStore
from backend.services import agent_factory
from backend.services.agent_factory import NativeAgent
from backend.services.connect import runner
from backend.storage.sqlite.pool import AsyncConnectionPool
from backend.tools.context import ToolContext
from backend.tools.registry import ToolRegistry
from backend.vtypes.contracts import JsonObject
from backend.vtypes.message import ToolCallChunk, VMessageChunk
from tests.chat_fakes import (
    ScriptedChatClient,
    require_spec,
    texto_chunk,
    tool_call_chunk,
)
from tests.native_engine import patch_native_engine


@pytest.fixture
async def native_session_store(tmp_path):
    pool = AsyncConnectionPool(
        str(tmp_path / "connect-runner-sessions.db"), min_size=1, max_size=2
    )
    await pool.open()
    store = SessionStore(pool)
    await store.setup()
    try:
        yield store
    finally:
        await pool.close()


class TestRunAgentForThread:
    async def test_roda_o_turno_e_devolve_o_texto_final(
        self, native_session_store, monkeypatch
    ) -> None:
        client = ScriptedChatClient([[texto_chunk("Olá! Como posso ajudar?")]])
        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            chat_client=client,
        )

        reply = await runner.run_agent_for_thread("connect-telegram-abc", "oi")

        assert reply == "Olá! Como posso ajudar?"
        assert client.chamadas == 1
        runs = await native_session_store.list_agent_runs("connect-telegram-abc")
        assert len(runs) == 1
        assert runs[0].user_id == "local"
        assert runs[0].untrusted_content is True

        # A mensagem do usuário e a resposta do agente ficaram persistidas
        # em SessionStore, sob o mesmo thread_id do mapeamento da plataforma.
        historico = await native_session_store.get_history("connect-telegram-abc")
        assert [m.role.value for m in historico] == ["system", "user", "assistant"]
        assert historico[1].text() == "oi"
        assert historico[-1].text() == "Olá! Como posso ajudar?"

        # Erro/borda: resposta maior que MAX_REPLY_CHARS é truncada (Telegram/
        # Discord rejeitam mensagem inteira acima do limite da plataforma).
        texto_longo = "x" * (runner.MAX_REPLY_CHARS + 500)
        client2 = ScriptedChatClient([[texto_chunk(texto_longo)]])
        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            chat_client=client2,
        )
        reply2 = await runner.run_agent_for_thread("connect-telegram-def", "oi de novo")
        assert len(reply2) == runner.MAX_REPLY_CHARS + 1  # +1 do "…" de corte
        assert reply2.endswith("…")

    async def test_segunda_mensagem_na_mesma_thread_reusa_o_historico(
        self, native_session_store, monkeypatch
    ) -> None:
        """Cada chamada relê o histórico persistido — a 2ª mensagem do mesmo
        `thread_id` não recria o system prompt nem perde o turno anterior."""
        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            chat_client=ScriptedChatClient([[texto_chunk("primeira resposta")]]),
        )
        await runner.run_agent_for_thread("connect-discord-xyz", "primeira mensagem")

        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            chat_client=ScriptedChatClient([[texto_chunk("segunda resposta")]]),
        )
        await runner.run_agent_for_thread("connect-discord-xyz", "segunda mensagem")

        historico = await native_session_store.get_history("connect-discord-xyz")
        # Só 1 system prompt (não duplicado) + 2 turnos completos.
        assert [m.role.value for m in historico] == [
            "system",
            "user",
            "assistant",
            "user",
            "assistant",
        ]

    async def test_tool_destrutiva_nunca_pausa_em_hitl_mensageria_externa(
        self, native_session_store, monkeypatch
    ) -> None:
        """`permission_mode` fixo em "auto" — o interlocutor externo não tem
        UI de aprovar/rejeitar, então uma tool destrutiva roda direto em vez
        de deixar o turno preso esperando aprovação que nunca chega."""
        from backend.tools.registry import TOOL_REGISTRY, ToolExtras, vtool

        if TOOL_REGISTRY.get("escrever_arquivo_connect") is None:

            @vtool(extras=ToolExtras(destructive=True))
            async def escrever_arquivo_connect(ctx: ToolContext) -> str:
                """escreve um arquivo (fake)."""
                return "escrito"

        registry = ToolRegistry()
        registry.register(require_spec("escrever_arquivo_connect"))

        client = ScriptedChatClient(
            [
                [
                    tool_call_chunk(
                        index=0,
                        id="call_1",
                        name="escrever_arquivo_connect",
                        args="{}",
                    )
                ],
                [texto_chunk("arquivo escrito com sucesso")],
            ]
        )
        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            tool_registry=registry,
            chat_client=client,
        )

        reply = await runner.run_agent_for_thread(
            "connect-slack-c1", "escreva um arquivo"
        )

        assert reply == "arquivo escrito com sucesso"
        assert client.chamadas == 2  # nunca pausou — o loop seguiu direto
        historico = await native_session_store.get_history("connect-slack-c1")
        assert [m.role.value for m in historico[-3:]] == [
            "assistant",
            "tool",
            "assistant",
        ]

    async def test_conector_nao_executa_tool_quando_modelo_exige_aprovacao(
        self, native_session_store, monkeypatch
    ) -> None:
        from backend.engine import conversation_loop
        from backend.engine.hitl import _risk_assessment_key
        from backend.tools.registry import TOOL_REGISTRY, ToolExtras, vtool

        executions = 0
        if TOOL_REGISTRY.get("escrever_arquivo_com_aprovacao_connect") is None:

            @vtool(extras=ToolExtras(destructive=True))
            async def escrever_arquivo_com_aprovacao_connect(
                ctx: ToolContext,
            ) -> str:
                """Escreve apenas quando a política libera a execução."""
                nonlocal executions
                executions += 1
                return "executado"

        registry = ToolRegistry()
        registry.register(require_spec("escrever_arquivo_com_aprovacao_connect"))
        client = ScriptedChatClient(
            [
                [
                    tool_call_chunk(
                        index=0,
                        id="call_requires_approval",
                        name="escrever_arquivo_com_aprovacao_connect",
                        args="{}",
                    )
                ]
            ]
        )
        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            tool_registry=registry,
            chat_client=client,
        )

        original_ctx_from_config = runner.ctx_from_config

        def _ctx_with_connector_decisions(config):
            ctx = original_ctx_from_config(config)
            ctx._extra["decision_model_preferences"] = {
                "enabled": True,
                "provider": "clef-local",
                "surfaces": {"connectors": True, "hitl": True},
            }
            return ctx

        async def _require_human(_harness, *, tool_name, ctx, args, **_kwargs) -> bool:
            key = _risk_assessment_key(tool_name, args)
            ctx._extra.setdefault("systemone_decisions", {})[key] = {
                "decision": "requires_human",
                "reason": "test_requires_human",
                "provider": "clef-local",
                "model": "test-model",
                "confidence": 0.99,
            }
            ctx._extra.setdefault("systemone_risk_elevated_calls", set()).add(key)
            ctx._extra["systemone_risk_requires_approval"] = True
            return True

        monkeypatch.setattr(runner, "ctx_from_config", _ctx_with_connector_decisions)
        monkeypatch.setattr(
            conversation_loop, "_build_selected_decision_harness", lambda _ctx: object()
        )
        monkeypatch.setattr(
            "backend.engine.hitl.precompute_tool_risk_assessment", _require_human
        )

        reply = await runner.run_agent_for_thread(
            "connect-slack-approval", "escreva um arquivo"
        )

        assert reply == "A ação requer aprovação no Vectora e foi pausada sem execução."
        assert executions == 0
        assert client.chamadas == 1
        run = (await native_session_store.list_agent_runs("connect-slack-approval"))[0]
        assert run.status == "waiting_approval"
        assert run.approvals[0]["tool_name"] == "escrever_arquivo_com_aprovacao_connect"

    async def test_delegacao_a_uma_soul_roda_sem_pausar_em_hitl(
        self, native_session_store, monkeypatch
    ) -> None:
        """Gap real (revisão de 2026-08-30): nenhum teste deste arquivo
        exercitava `native_agent.subagent_catalog` não-vazio — em produção
        `run_agent_for_thread` só injeta `SubagentDeps` (com
        `should_require_approval`) quando o catálogo não está vazio
        (runner.py:76-85), e é isso que faz uma mensagem do
        Telegram/Discord/Slack/Email conseguir de fato delegar a uma SOUL.
        Sem este teste, um bug nesse `if` (ex.: catálogo nunca chegando
        populado a partir de `get_native_agent`) passaria despercebido."""

        client = ScriptedChatClient(
            [
                [
                    tool_call_chunk(
                        index=0,
                        id="call_soul",
                        name="delegate_to_subagent",
                        args='{"subagent_type": "coder", "prompt": "implemente algo"}',
                    )
                ],
                [texto_chunk("feito pela SOUL")],  # turno interno da SOUL
                [texto_chunk("SOUL concluiu: feito pela SOUL")],  # loop externo
            ]
        )
        registry = ToolRegistry()
        registry.register(require_spec("delegate_to_subagent"))
        soul = SubagentSpec(
            name="coder",
            description="agente de teste",
            system_prompt="você é um agente de teste",
            tools=[],
        )
        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            tool_registry=registry,
            subagent_catalog={"coder": soul},
            chat_client=client,
        )

        reply = await runner.run_agent_for_thread(
            "connect-telegram-soul", "peça pro coder implementar algo"
        )

        assert reply == "SOUL concluiu: feito pela SOUL"
        assert client.chamadas == 3  # nunca pausou esperando aprovação
        historico = await native_session_store.get_history("connect-telegram-soul")
        assert [m.role.value for m in historico[-3:]] == [
            "assistant",
            "tool",
            "assistant",
        ]

    async def test_falha_no_chat_client_propaga_pro_handle_incoming_message(
        self, native_session_store, monkeypatch
    ) -> None:
        """Erro/borda: run_agent_for_thread NÃO trata a exceção — é
        `handle_incoming_message` (backend/services/gateway/messaging.py)
        quem converte falha em resposta amigável, então o interlocutor
        externo nunca fica sem resposta nenhuma."""
        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            chat_client=ScriptedChatClient(exc=RuntimeError("provider fora do ar")),
        )

        with pytest.raises(RuntimeError, match="provider fora do ar"):
            await runner.run_agent_for_thread("connect-email-1", "mensagem qualquer")


class TestProcessIncoming:
    async def test_fluxo_completo_resolve_thread_roda_agente_e_devolve_resposta(
        self, native_session_store, monkeypatch, tmp_path
    ) -> None:
        """Fluxo ponta a ponta de uma mensagem de plataforma externa: resolve
        (cria) o mapeamento thread, roda o agente via `run_agent_for_thread`
        e devolve a resposta pronta pra plataforma reenviar."""
        from backend.services.gateway.messaging import IncomingMessage
        from backend.settings import settings

        monkeypatch.setattr(settings, "db_dsn", str(tmp_path / "connect-process.db"))

        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            chat_client=ScriptedChatClient([[texto_chunk("resposta do agente")]]),
        )

        incoming = IncomingMessage(
            platform="telegram", platform_user_id="999", text="oi vectora"
        )
        outgoing = await runner.process_incoming(incoming)

        assert outgoing.platform == "telegram"
        assert outgoing.platform_user_id == "999"
        assert outgoing.text == "resposta do agente"

        # Erro/borda: falha ao rodar o agente (ex.: provider fora do ar)
        # nunca deixa o interlocutor sem resposta — process_incoming devolve
        # uma mensagem amigável em vez de propagar a exceção.
        patch_native_engine(
            monkeypatch,
            module=runner,
            session_store=native_session_store,
            chat_client=ScriptedChatClient(exc=RuntimeError("boom")),
        )
        incoming2 = IncomingMessage(
            platform="telegram", platform_user_id="999", text="de novo"
        )
        outgoing2 = await runner.process_incoming(incoming2)
        assert "não consegui processar" in outgoing2.text.lower()
        # Reusa a MESMA thread da primeira mensagem (mesmo platform_user_id).
        assert outgoing2.platform_user_id == "999"
