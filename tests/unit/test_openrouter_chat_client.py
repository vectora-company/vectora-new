"""``OpenRouterChatClient`` — chat nativo do OpenRouter (formato OpenAI-
compatível), Protocol ``ChatClient``. Espelha os demais
`test_*_chat_client.py`: `reasoning` em canal próprio (nunca concatenado ao
texto), tool_calls fragmentado no streaming por `index`, `arguments`
inválido não derruba a resposta.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.llm.openrouter.chat_client import OpenRouterChatClient
from backend.llm.openrouter.client import OpenRouterClient, OpenRouterResponseError
from backend.tools.registry import ToolExtras, vtool
from backend.vtypes.message import (
    ContentBlock,
    MessageRole,
    ToolCall,
    VMessage,
    text_message,
)


def _client(handler, **kwargs) -> OpenRouterChatClient:
    http_client = OpenRouterClient(
        api_key="sk-or-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenRouterChatClient(model="openrouter/auto", client=http_client, **kwargs)


def _ok_response(text: str = "Olá", **extra) -> dict:
    return {
        "id": "gen_1",
        "model": "openrouter/auto",
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        **extra,
    }


class TestAgenerate:
    async def test_prompt_simples_devolve_conteudo(self) -> None:
        def handler(_req):
            return httpx.Response(200, json=_ok_response("Olá do OpenRouter"))

        resultado = await _client(handler).agenerate(
            [text_message(MessageRole.USER, "oi")]
        )

        assert resultado.text() == "Olá do OpenRouter"
        assert resultado.role == MessageRole.ASSISTANT

    async def test_resposta_sem_choices_vira_erro_tipado(self) -> None:
        def handler(_req):
            return httpx.Response(200, json={"id": "gen_1"})

        with pytest.raises(OpenRouterResponseError, match="choices"):
            await _client(handler).agenerate([text_message(MessageRole.USER, "oi")])


class TestConversaoDeMensagens:
    async def test_projecao_de_prompt_para_modelo_pequeno_preserva_nome(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            capturado.update(json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        client = _client(handler, compact_system_prompt=True)
        await client.agenerate(
            [
                text_message(
                    MessageRole.SYSTEM,
                    "## Identity — Vectora\n" + ("documentação longa " * 200),
                ),
                text_message(MessageRole.USER, "Quem é você?"),
            ]
        )

        system = capturado["messages"][0]["content"]
        assert "Nome: Vectora" in system
        assert len(system) < 1_000

    async def test_modelo_pequeno_aplica_piso_de_temperatura(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            capturado.update(json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        client = _client(handler, compact_system_prompt=True)
        await client.agenerate(
            [text_message(MessageRole.USER, "apresente-se")], temperature=0
        )

        assert capturado["temperature"] == 0.2

    async def test_roles_traduzidos_e_tool_message_carrega_tool_call_id(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            capturado.update(json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        await _client(handler).agenerate(
            [
                text_message(MessageRole.SYSTEM, "sistema"),
                text_message(MessageRole.USER, "usuário"),
                text_message(MessageRole.ASSISTANT, "assistente"),
                VMessage(
                    role=MessageRole.TOOL,
                    content=[ContentBlock(kind="text", text="resultado")],
                    tool_call_id="call_1",
                ),
            ]
        )

        mensagens = capturado["messages"]
        assert [m["role"] for m in mensagens] == ["system", "user", "assistant", "tool"]
        assert mensagens[-1]["tool_call_id"] == "call_1"

    async def test_assistant_com_tool_calls_serializa_arguments_como_json(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            capturado.update(json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        mensagem = VMessage(
            role=MessageRole.ASSISTANT,
            content=[ContentBlock(kind="text", text="vou somar")],
            tool_calls=[ToolCall(id="call_1", name="somar", args={"a": 1})],
        )

        await _client(handler).agenerate(
            [text_message(MessageRole.USER, "x"), mensagem]
        )

        enviado = capturado["messages"][-1]
        assert enviado["tool_calls"][0]["function"]["name"] == "somar"
        assert json.loads(enviado["tool_calls"][0]["function"]["arguments"]) == {"a": 1}

    async def test_mensagem_de_role_desconhecido_falha(self) -> None:
        def handler(_req):
            return httpx.Response(200, json=_ok_response())

        estranha = text_message(MessageRole.USER, "?")
        estranha.role = "extraterrestre"  # ty: ignore[invalid-assignment]

        with pytest.raises(ValueError, match="extraterrestre"):
            await _client(handler).agenerate([estranha])

    async def test_bloco_de_imagem_nao_e_descartado(self) -> None:
        """Regressão: `_to_openrouter_message` usava `msg.text()`, que só
        concatena blocos `text` — um anexo de imagem desaparecia do payload
        sem erro nem log."""
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            capturado.update(json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        mensagem = VMessage(
            role=MessageRole.USER,
            content=[
                ContentBlock(kind="text", text="olha essa imagem"),
                ContentBlock(kind="image_url", image_url="data:image/png;base64,abc"),
            ],
        )

        await _client(handler).agenerate([mensagem])

        partes = capturado["messages"][0]["content"]
        assert {"type": "text", "text": "olha essa imagem"} in partes
        assert {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        } in partes


class TestToolCalling:
    async def test_tool_spec_vira_schema_openai_no_payload(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            capturado.update(json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        @vtool(extras=ToolExtras())
        async def buscar(query: str) -> str:
            """busca algo.
            Args:
                query: termo
            """
            return ""

        from backend.tools.registry import TOOL_REGISTRY

        spec = TOOL_REGISTRY.get("buscar")
        assert spec is not None

        await _client(handler).agenerate(
            [text_message(MessageRole.USER, "busca x")], tools=[spec]
        )

        assert capturado["tools"][0]["function"]["name"] == "buscar"

    async def test_tool_call_nao_streaming_volta_montada(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "id": "gen_1",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "somar",
                                            "arguments": '{"a": 1, "b": 2}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            )

        resultado = await _client(handler).agenerate(
            [text_message(MessageRole.USER, "some 1 e 2")]
        )
        assert resultado.tool_calls == [
            ToolCall(id="call_1", name="somar", args={"a": 1, "b": 2})
        ]
        assert resultado.finish_reason == "tool_calls"

    async def test_arguments_invalido_nao_derruba_a_resposta(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "id": "gen_1",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "somar",
                                            "arguments": "{isso nao e json",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            )

        resultado = await _client(handler).agenerate(
            [text_message(MessageRole.USER, "x")]
        )
        assert resultado.tool_calls[0].name == "somar"
        assert "_parse_error" in resultado.tool_calls[0].args


class TestStreaming:
    async def test_chunks_de_texto_saem_na_ordem(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"Oi"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":" mundo"}}]}\n\n'
            b"data: [DONE]\n\n"
        )

        def handler(_req):
            return httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )

        chunks = [
            c
            async for c in _client(handler).astream(
                [text_message(MessageRole.USER, "oi")]
            )
        ]
        text = "".join(c.delta_text for c in chunks)

        assert text == "Oi mundo"

    async def test_reasoning_nao_entra_no_delta_text(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"reasoning":"pensando..."}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"resposta"}}]}\n\n'
            b"data: [DONE]\n\n"
        )

        def handler(_req):
            return httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )

        chunks = [
            c
            async for c in _client(handler).astream(
                [text_message(MessageRole.USER, "oi")]
            )
        ]
        text = "".join(c.delta_text for c in chunks)
        raciocinio = "".join(c.delta_reasoning or "" for c in chunks)

        assert text == "resposta"
        assert raciocinio == "pensando..."

    async def test_stream_sem_nenhum_chunk_util_nao_trava(self) -> None:
        def handler(_req):
            return httpx.Response(
                200,
                content=b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )

        chunks = [
            c
            async for c in _client(handler).astream(
                [text_message(MessageRole.USER, "oi")]
            )
        ]
        assert chunks == []


class TestStreamingToolCallsEUsage:
    async def test_tool_call_fragmentada_por_index_sai_completa(self) -> None:
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "file_read", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"path":'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"a.py"}'},
                                }
                            ]
                        }
                    }
                ]
            },
        ]
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
        body += b"data: [DONE]\n\n"

        def handler(_req):
            return httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )

        chunks = [
            c
            async for c in _client(handler).astream(
                [text_message(MessageRole.USER, "oi")]
            )
        ]
        fragments = "".join(
            tc.args_fragment for c in chunks for tc in c.tool_call_chunks
        )
        names = {tc.name for c in chunks for tc in c.tool_call_chunks if tc.name}

        assert fragments == '{"path":"a.py"}'
        assert names == {"file_read"}

    async def test_usage_do_ultimo_evento_vira_usage_metadata(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"oi"}}]}\n\n'
            b'data: {"choices":[],"usage":'
            b'{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}\n\n'
            b"data: [DONE]\n\n"
        )

        def handler(_req):
            return httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )

        chunks = [
            c
            async for c in _client(handler).astream(
                [text_message(MessageRole.USER, "oi")]
            )
        ]
        com_usage = [c for c in chunks if c.usage]

        assert len(com_usage) == 1
        assert com_usage[0].usage == {
            "input_tokens": 10,
            "output_tokens": 3,
            "total_tokens": 13,
        }

    async def test_compact_system_prompt_preserva_instrucoes_e_idioma(self) -> None:
        payload_captured = {}

        def handler(req):
            nonlocal payload_captured
            payload_captured = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, json=_ok_response("Ok"))

        client = _client(handler, compact_system_prompt=True)
        system_text = (
            "Você é o Vectora Coder Agent com acesso ao sistema.\n"
            "<guidelines>\n"
            "Antes de alterar arquivos, solicite aprovação do usuário.\n"
            "# Diretriz prioritária de segurança para produção:\n"
            "Esta regra possui mais de trezentos caracteres para garantir que diretrizes longas, completas e detalhadas fornecidas pelos usuários ou pelo workspace nunca sejam truncadas ou descartadas por critérios arbitrários de tamanho de linha, mantendo a integridade total de políticas corporativas complexas.\n"
            "</guidelines>\n"
            "Idioma: Responda em Espanhol.\n"
            "Regra: Nunca exclua o arquivo de banco de dados.\n"
            "Nunca use write_terminal sem aprovação.\n"
            "### Tools\n"
            "#### 🗂️ Filesystem (main priority)\n"
            "- `file_read`, `file_edit`, `file_write` — read, edit, and create files\n"
            "Muitas descrições longas sobre a interface e botões que devem ser ignoradas..."
        )
        await client.agenerate(
            [
                text_message(MessageRole.SYSTEM, system_text),
                text_message(MessageRole.USER, "ajuda"),
            ]
        )

        sys_msg = payload_captured["messages"][0]["content"]
        assert "Antes de alterar arquivos, solicite aprovação do usuário." in sys_msg
        assert "Esta regra possui mais de trezentos caracteres" in sys_msg
        assert "Nunca use write_terminal sem aprovação." in sys_msg
        assert "Idioma: Responda em Espanhol." in sys_msg
        assert "Regra: Nunca exclua o arquivo de banco de dados." in sys_msg
        assert "Muitas descrições longas" not in sys_msg
        assert "- `file_read`, `file_edit`" not in sys_msg

    async def test_compact_system_prompt_preserva_secao_apos_tools_e_regras_sem_keywords(
        self,
    ) -> None:
        payload_captured = {}

        def handler(req):
            nonlocal payload_captured
            payload_captured = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, json=_ok_response("Ok"))

        client = _client(handler, compact_system_prompt=True)
        system_text = (
            "Você é o Vectora assistente.\n"
            "### Tools\n"
            "#### 🗂️ Filesystem\n"
            "- read and write files\n"
            "### Segurança\n"
            "Execute testes antes de concluir.\n"
        )
        await client.agenerate(
            [
                text_message(MessageRole.SYSTEM, system_text),
                text_message(MessageRole.USER, "olá"),
            ]
        )

        sys_msg = payload_captured["messages"][0]["content"]
        assert "- read and write files" not in sys_msg
        assert "### Segurança" in sys_msg
        assert "Execute testes antes de concluir." in sys_msg

    async def test_compact_system_prompt_hierarquia_markdown_e_mencao_em_instrucao(
        self,
    ) -> None:
        payload_captured = {}

        def handler(req):
            nonlocal payload_captured
            payload_captured = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, json=_ok_response("Ok"))

        client = _client(handler, compact_system_prompt=True)
        system_text = (
            "Você é o assistente.\n"
            "Não altere a seção ### Tools sem autorização prévia.\n"
            "### Tools\n"
            "#### Terminal\n"
            "Documentação extensa do terminal que deve ser descartada.\n"
            "##### Subseção profunda do terminal\n"
            "Mais detalhes técnicos irrelevantes.\n"
            "### Políticas do Usuário\n"
            "Siga rigorosamente as orientações da equipe.\n"
        )
        await client.agenerate(
            [
                text_message(MessageRole.SYSTEM, system_text),
                text_message(MessageRole.USER, "olá"),
            ]
        )

        sys_msg = payload_captured["messages"][0]["content"]
        # A menção inline a "### Tools" NÃO deve ter sido tratada como cabeçalho descartável
        assert "Não altere a seção ### Tools sem autorização prévia." in sys_msg
        # Subseções (#### e #####) dentro de ### Tools DEVEM ser descartadas
        assert "Documentação extensa do terminal" not in sys_msg
        assert "Mais detalhes técnicos irrelevantes" not in sys_msg
        # Cabeçalho de mesmo nível (### Políticas do Usuário) encerra o descarte
        assert "### Políticas do Usuário" in sys_msg
        assert "Siga rigorosamente as orientações da equipe." in sys_msg

    async def test_compact_system_prompt_preserva_nivel_externo_com_cabecalhos_aninhados(
        self,
    ) -> None:
        payload_captured = {}

        def handler(req):
            nonlocal payload_captured
            payload_captured = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, json=_ok_response("Ok"))

        client = _client(handler, compact_system_prompt=True)
        system_text = (
            "## Identity\n"
            "Preâmbulo de identidade geral que deve ser descartado.\n"
            "### Tools\n"
            "Documentação de ferramentas que também deve ser descartada.\n"
            "### Outra descrição\n"
            "Documentação ainda pertencente a Identity que NÃO deve reentrar.\n"
            "## Regras\n"
            "Execute testes antes de concluir.\n"
        )
        await client.agenerate(
            [
                text_message(MessageRole.SYSTEM, system_text),
                text_message(MessageRole.USER, "olá"),
            ]
        )

        sys_msg = payload_captured["messages"][0]["content"]
        assert "Preâmbulo de identidade geral" not in sys_msg
        assert "Documentação de ferramentas" not in sys_msg
        assert "Documentação ainda pertencente a Identity" not in sys_msg
        assert "Execute testes antes de concluir." in sys_msg


class TestPromptCache:
    def _client(self, model: str, origin: str) -> OpenRouterChatClient:
        from backend.llm.openrouter.chat_client import OpenRouterChatClient

        http = OpenRouterClient(
            api_key="k",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
            ),
        )
        return OpenRouterChatClient(model, http, media_model_id=f"{origin}:{model}")

    def _messages(self):
        from backend.vtypes.message import MessageRole, text_message

        return [
            text_message(MessageRole.SYSTEM, "regras"),
            text_message(MessageRole.USER, "primeira"),
            text_message(MessageRole.ASSISTANT, "resposta"),
            text_message(MessageRole.USER, "segunda"),
        ]

    def test_anthropic_model_on_openrouter_marks_cache_breakpoints(self) -> None:
        body = self._client("anthropic/claude-sonnet-4.5", "openrouter")._payload(
            self._messages(), tools=None, temperature=None, max_tokens=None
        )

        marked = [
            m["role"]
            for m in body["messages"]
            if isinstance(m["content"], list) and m["content"][-1].get("cache_control")
        ]
        assert marked == ["system", "assistant", "user"]
        assert body["messages"][1]["content"] == "primeira"

    def test_model_without_explicit_cache_is_untouched(self) -> None:
        body = self._client("meta-llama/llama-4", "openrouter")._payload(
            self._messages(), tools=None, temperature=None, max_tokens=None
        )
        assert all(isinstance(m["content"], str) for m in body["messages"])
        assert "cache_prompt" not in body

    def test_llamacpp_asks_to_reuse_prefix(self) -> None:
        body = self._client("qwen", "llamacpp")._payload(
            self._messages(), tools=None, temperature=None, max_tokens=None
        )
        assert body["cache_prompt"] is True


class TestRouterCacheKey:
    def _payload(self, origin: str):
        from backend.llm.prompt_cache import prompt_cache_key

        client = TestPromptCache()._client("cc/claude-sonnet", origin)
        token = prompt_cache_key.set("thread-9")
        try:
            return client._payload(
                TestPromptCache()._messages(),
                tools=None,
                temperature=None,
                max_tokens=None,
            )
        finally:
            prompt_cache_key.reset(token)

    def test_9router_receives_thread_key(self) -> None:
        assert self._payload("nine_router")["prompt_cache_key"] == "thread-9"

    def test_openrouter_receives_session_id_for_sticky_routing(self) -> None:
        body = self._payload("openrouter")
        assert body["session_id"] == "thread-9"
        assert "prompt_cache_key" not in body

    def test_llamacpp_does_not_receive_session_key(self) -> None:
        body = self._payload("llamacpp")
        assert "prompt_cache_key" not in body
        assert "session_id" not in body


class TestRouterSessionAndUsage:
    def _client(self, origin: str):
        return TestPromptCache()._client("modelo", origin)

    def _with_key(self, origin: str):
        from backend.llm.prompt_cache import prompt_cache_key

        token = prompt_cache_key.set("thread-7")
        try:
            return self._client(origin)._session_headers()
        finally:
            prompt_cache_key.reset(token)

    def test_session_header_for_openrouter_and_9router(self) -> None:
        assert self._with_key("openrouter") == {"x-session-id": "thread-7"}
        assert self._with_key("nine_router") == {"x-session-id": "thread-7"}

    def test_no_header_for_local_runtimes_or_without_thread(self) -> None:
        assert self._with_key("llamacpp") is None
        assert self._client("openrouter")._session_headers() is None

    def test_usage_reads_cache_read_and_write_in_both_formats(self) -> None:
        from backend.llm.openrouter.chat_client import _usage_metadata

        openrouter = _usage_metadata(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "cost": 0.0042,
                "prompt_tokens_details": {
                    "cached_tokens": 800,
                    "cache_write_tokens": 50,
                },
            }
        )
        omniroute_usage = _usage_metadata(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_tokens_details": {
                    "cached_tokens": 800,
                    "cache_creation_tokens": 70,
                },
            }
        )

        assert openrouter is not None and omniroute_usage is not None
        assert openrouter["cached_input_tokens"] == 800
        assert openrouter["cache_write_tokens"] == 50
        assert openrouter["cost_usd"] == 0.0042
        assert omniroute_usage["cache_write_tokens"] == 70
