"""``AnthropicChatClient`` — chat nativo da Anthropic (Messages API), Protocol
``ChatClient``. Espelha `test_openai_chat_client.py`, adaptado
pro formato da Anthropic: `system` top-level, tool_result como mensagem
`user`, streaming por `content_block.index`, `usage` cumulativo.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import Request, Response

from backend.llm.anthropic.chat_client import AnthropicChatClient
from backend.llm.anthropic.client import AnthropicClient, AnthropicResponseError
from backend.tools.registry import ToolExtras, vtool
from backend.vtypes.contracts import JsonObject
from backend.vtypes.message import (
    ContentBlock,
    MessageRole,
    ToolCall,
    VMessage,
    text_message,
)
from tests.json_values import as_object, dig


@pytest.fixture(autouse=True)
def _sem_cache_de_prompt(monkeypatch: pytest.MonkeyPatch, request) -> None:
    """Os testes de conversão comparam o corpo cru; o cache tem testes próprios."""
    if request.node.get_closest_marker("prompt_cache"):
        return
    from backend.settings import settings

    monkeypatch.setattr(settings, "prompt_cache_enabled", False)


def _client(handler, **kwargs) -> AnthropicChatClient:
    http_client = AnthropicClient(
        api_key="sk-ant-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return AnthropicChatClient(model="claude-sonnet-test", client=http_client, **kwargs)


def _ok_response(text: str = "Olá", **extra) -> dict:
    return {
        "id": "msg_1",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 10, "output_tokens": 3},
        **extra,
    }


class TestAgenerate:
    async def test_prompt_simples_devolve_conteudo(self) -> None:
        def handler(_req: Request) -> Response:
            return httpx.Response(200, json=_ok_response("Olá da Anthropic"))

        resultado = await _client(handler).agenerate(
            [text_message(MessageRole.USER, "oi")]
        )

        assert resultado.text() == "Olá da Anthropic"
        assert resultado.role == MessageRole.ASSISTANT

    async def test_resposta_sem_content_lista_vira_erro_tipado(self) -> None:
        def handler(_req: Request) -> Response:
            return httpx.Response(200, json={"id": "msg_1"})

        with pytest.raises(AnthropicResponseError, match="content"):
            await _client(handler).agenerate([text_message(MessageRole.USER, "oi")])


class TestConversaoDeMensagens:
    async def test_system_vira_parametro_top_level_e_roles_traduzidos(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            capturado.update(_json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        await _client(handler).agenerate(
            [
                text_message(MessageRole.SYSTEM, "sistema"),
                text_message(MessageRole.USER, "usuário"),
                text_message(MessageRole.ASSISTANT, "assistente"),
            ]
        )

        assert capturado["system"] == "sistema"
        assert [m["role"] for m in capturado["messages"]] == ["user", "assistant"]
        assert "system" not in [m.get("role") for m in capturado["messages"]]

    async def test_tool_message_vira_user_com_bloco_tool_result(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            capturado.update(_json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        await _client(handler).agenerate(
            [
                text_message(MessageRole.USER, "usuário"),
                VMessage(
                    role=MessageRole.TOOL,
                    content=[ContentBlock(kind="text", text="resultado")],
                    tool_call_id="call_1",
                    is_error=True,
                ),
            ]
        )

        tool_msg = capturado["messages"][-1]
        assert tool_msg["role"] == "user"
        bloco = tool_msg["content"][0]
        assert bloco["type"] == "tool_result"
        assert bloco["tool_use_id"] == "call_1"
        assert bloco["content"] == "resultado"
        assert bloco["is_error"] is True

    async def test_assistant_com_tool_calls_vira_blocos_text_e_tool_use(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            capturado.update(_json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        mensagem = VMessage(
            role=MessageRole.ASSISTANT,
            content=[ContentBlock(kind="text", text="vou chamar a tool")],
            tool_calls=[ToolCall(id="call_1", name="somar", args={"a": 1})],
        )

        await _client(handler).agenerate(
            [text_message(MessageRole.USER, "x"), mensagem]
        )

        blocos = capturado["messages"][-1]["content"]
        assert blocos[0] == {"type": "text", "text": "vou chamar a tool"}
        assert blocos[1] == {
            "type": "tool_use",
            "id": "call_1",
            "name": "somar",
            "input": {"a": 1},
        }

    async def test_mensagem_de_role_desconhecido_falha(self) -> None:
        def handler(_req):
            return httpx.Response(200, json=_ok_response())

        estranha = text_message(MessageRole.USER, "?")
        estranha.role = "extraterrestre"  # ty: ignore[invalid-assignment]

        with pytest.raises(ValueError, match="extraterrestre"):
            await _client(handler).agenerate([estranha])

    async def test_bloco_de_imagem_nao_e_descartado(self) -> None:
        """Regressão: `_to_anthropic_messages` usava `msg.text()` pra
        mensagens `user`/`assistant`, que só concatena blocos `text` — um
        anexo de imagem desaparecia do payload sem erro nem log."""
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            capturado.update(_json.loads(req.content))
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
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
        } in partes

    async def test_imagem_com_url_direta_vira_bloco_source_url(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            capturado.update(_json.loads(req.content))
            return httpx.Response(200, json=_ok_response())

        mensagem = VMessage(
            role=MessageRole.USER,
            content=[
                ContentBlock(kind="image_url", image_url="https://exemplo.com/a.png"),
            ],
        )

        await _client(handler).agenerate([mensagem])

        partes = capturado["messages"][0]["content"]
        assert {
            "type": "image",
            "source": {"type": "url", "url": "https://exemplo.com/a.png"},
        } in partes


class TestToolCalling:
    async def test_tool_spec_vira_schema_nativo_anthropic(self) -> None:
        capturado: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            capturado.update(_json.loads(req.content))
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

        tool = capturado["tools"][0]
        assert tool["name"] == "buscar"
        assert "input_schema" in tool
        assert "type" not in tool  # Anthropic não usa o envelope da OpenAI

    async def test_tool_use_nao_streaming_volta_montada(self) -> None:
        def handler(_req: Request) -> Response:
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "somar",
                            "input": {"a": 1, "b": 2},
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


class TestStreaming:
    async def test_chunks_de_texto_saem_na_ordem(self) -> None:
        body = (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text"}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"Oi"}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":" mundo"}}\n\n'
            b'data: {"type":"message_stop"}\n\n'
        )

        def handler(_req: Request) -> Response:
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

    async def test_stream_sem_nenhum_chunk_util_nao_trava(self) -> None:
        def handler(_req: Request) -> Response:
            return httpx.Response(
                200,
                content=b'data: {"type":"message_stop"}\n\n',
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
    """`input_json_delta` fragmentado em vários chunks precisa reconstruir a
    tool call completa via ToolCallChunk acumulados pelo caller; `usage` de
    `message_delta` é cumulativo — cada emissão substitui, nunca soma."""

    async def test_tool_call_fragmentada_em_varios_deltas_sai_completa(self) -> None:
        body = (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"tool_use","id":"call_1",'
            b'"name":"file_read"}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"input_json_delta","partial_json":"\\"a.py\\"}"}}\n\n'
            b'data: {"type":"content_block_stop","index":0}\n\n'
            b'data: {"type":"message_stop"}\n\n'
        )

        def handler(_req: Request) -> Response:
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
        ids = {tc.id for c in chunks for tc in c.tool_call_chunks if tc.id}
        names = {tc.name for c in chunks for tc in c.tool_call_chunks if tc.name}

        assert fragments == '{"path":"a.py"}'
        assert ids == {"call_1"}
        # Nome só é emitido no primeiro fragmento — evita repetir em cada
        # chunk subsequente do mesmo índice.
        assert names == {"file_read"}

    async def test_usage_de_message_delta_vira_usage_metadata_cumulativo(self) -> None:
        body = (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text"}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"oi"}}\n\n'
            b'data: {"type":"message_delta","delta":{},'
            b'"usage":{"input_tokens":10,"output_tokens":1}}\n\n'
            b'data: {"type":"message_delta","delta":{},'
            b'"usage":{"input_tokens":10,"output_tokens":3}}\n\n'
            b'data: {"type":"message_stop"}\n\n'
        )

        def handler(_req: Request) -> Response:
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

        # Duas emissões de usage — cada uma o total até aquele ponto, nunca
        # somadas entre si (a segunda já é o valor final, não 10+3).
        assert len(com_usage) == 2
        assert com_usage[-1].usage == {
            "input_tokens": 10,
            "output_tokens": 3,
            "total_tokens": 13,
        }


class TestPromptCache:
    def test_marks_tools_system_and_last_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.llm.anthropic.chat_client import _with_prompt_cache
        from backend.settings import settings

        monkeypatch.setattr(settings, "prompt_cache_enabled", True)
        body: JsonObject = {
            "system": "regras",
            "tools": [{"name": "a"}, {"name": "b"}],
            "messages": [
                {"role": "user", "content": "um"},
                {"role": "assistant", "content": [{"type": "text", "text": "dois"}]},
                {"role": "user", "content": "três"},
            ],
        }

        out = _with_prompt_cache(body)

        assert "cache_control" not in as_object(dig(out, "tools", 0))
        assert dig(out, "tools", 1, "cache_control") == {"type": "ephemeral"}
        assert dig(out, "system", 0, "cache_control") == {"type": "ephemeral"}
        assert dig(out, "messages", 0, "content") == "um"
        assert dig(out, "messages", 1, "content", -1, "cache_control")
        assert dig(out, "messages", 2, "content", -1, "text") == "três"
        assert dig(out, "messages", 2, "content", -1, "cache_control")

    def test_disabled_does_not_change_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.llm.anthropic.chat_client import _with_prompt_cache
        from backend.settings import settings

        monkeypatch.setattr(settings, "prompt_cache_enabled", False)
        body: JsonObject = {
            "system": "regras",
            "messages": [{"role": "user", "content": "oi"}],
        }

        assert _with_prompt_cache(body) == {
            "system": "regras",
            "messages": [{"role": "user", "content": "oi"}],
        }


def test_anthropic_usage_sums_cache_and_exposes_cache_read() -> None:
    from backend.llm.anthropic.chat_client import _usage_metadata

    uso = _usage_metadata(
        {
            "input_tokens": 50,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
            "output_tokens": 20,
        }
    )

    assert uso == {
        "input_tokens": 1000,
        "output_tokens": 20,
        "total_tokens": 1020,
        "cached_input_tokens": 900,
        "cache_write_tokens": 50,
    }
