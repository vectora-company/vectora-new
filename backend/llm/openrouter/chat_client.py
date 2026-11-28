"""``OpenRouterChatClient`` — chat nativo do OpenRouter (`/chat/completions`,
formato OpenAI-compatível), implementa o Protocol ``ChatClient``
(``backend/llm/base.py``), consumido diretamente pelo motor nativo
(``backend/engine/conversation_loop.py``).

Peculiaridades que o parser preserva:
- `reasoning` nunca entra no texto da mensagem — canal próprio
  (`ContentBlock(kind="reasoning")`/`VMessageChunk.delta_reasoning`),
  concatenar mistura raciocínio e resposta na tela.
- `usage.cost` (campo próprio do OpenRouter, ausente na API da OpenAI) é
  preservado como `cost_usd` para medição sem estimar preço.
- `tool_calls` fragmentado no streaming por `index` — acumulado pelo caller
  via `ToolCallChunk`, mesmo padrão que os demais 4 clients.
- `arguments` inválido (JSON malformado) não derruba a resposta — vira
  `ToolCall(args={"_parse_error": <texto bruto>})`, mesmo padrão de
  `backend/llm/openai/chat_client.py`.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, TypedDict

from backend.llm.media_parts import (
    AcceptedMedia,
    prepare_content,
    resolve_native_media,
)
from backend.llm.openrouter.client import OpenRouterClient, OpenRouterResponseError
from backend.llm.prompt_cache import prompt_cache_key
from backend.llm.tool_schema import chat_completions_tool
from backend.vtypes.contracts import JsonObject, JsonValue, UsageSnapshot, as_int
from backend.vtypes.message import (
    ContentBlock,
    MessageRole,
    ToolCall,
    ToolCallChunk,
    VMessage,
    VMessageChunk,
    coerce_json_object,
)

if TYPE_CHECKING:
    from backend.tools.registry import ToolSpec

_ROLE_POR_TIPO = {
    MessageRole.SYSTEM: "system",
    MessageRole.USER: "user",
    MessageRole.ASSISTANT: "assistant",
    MessageRole.TOOL: "tool",
}


class _UsagePayload(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    prompt_tokens_details: JsonObject


_AUDIO_FORMATS = {"audio/mpeg": "mp3", "audio/wav": "wav"}


def _media_part(bloco: ContentBlock) -> JsonObject | None:
    """Parte Chat-Completions de um anexo enviado como arquivo."""
    data_url = bloco.media_data_url or ""
    mime_type, _, dado = data_url.partition(";base64,")
    mime_type = mime_type.removeprefix("data:")
    if mime_type == "application/pdf":
        return {
            "type": "file",
            "file": {
                "filename": bloco.attachment_name or "anexo.pdf",
                "file_data": data_url,
            },
        }
    if mime_type in _AUDIO_FORMATS:
        return {
            "type": "input_audio",
            "input_audio": {"data": dado, "format": _AUDIO_FORMATS[mime_type]},
        }
    if mime_type.startswith("video/"):
        return {"type": "video_url", "video_url": {"url": data_url}}
    return None


def _to_openrouter_content(
    content: list[ContentBlock], accepted: AcceptedMedia | None = None
) -> str | list[JsonValue]:
    """Bloco `text` vira string simples (caso comum); imagens e anexos
    enviados como arquivo forçam o formato de lista de partes
    Chat-Completions — nunca descarta a imagem só porque `msg.text()` só
    concatena texto.

    ``accepted`` vem do catálogo do gateway para o modelo
    (:func:`backend.llm.media_parts.resolve_native_media`); sem ele, anexos
    que não são imagem seguem pela forma textual.
    """
    content = prepare_content(content, accepted or {})
    if not any(b.kind in {"image_url", "media"} for b in content):
        return "".join(b.text or "" for b in content if b.kind == "text")
    partes: list[JsonValue] = []
    for bloco in content:
        if bloco.kind == "text" and bloco.text:
            partes.append({"type": "text", "text": bloco.text})
        elif bloco.kind == "image_url" and bloco.image_url:
            partes.append({"type": "image_url", "image_url": {"url": bloco.image_url}})
        elif bloco.kind == "media":
            parte = _media_part(bloco)
            partes.append(parte or {"type": "text", "text": bloco.text or ""})
    return partes


def _to_openrouter_message(
    msg: VMessage, accepted: AcceptedMedia | None = None
) -> dict:
    """Executa a operação  to openrouter message."""
    role = _ROLE_POR_TIPO.get(msg.role)
    if role is None:
        erro = f"Mensagem de role {msg.role!r} não tem equivalente no OpenRouter"
        raise ValueError(erro)

    saida: JsonObject = {
        "role": role,
        "content": _to_openrouter_content(msg.content, accepted),
    }

    if role == "tool":
        # Obrigatório no formato OpenAI-compatível — sem ele o provider
        # recusa a mensagem de tool com 400.
        saida["tool_call_id"] = msg.tool_call_id or ""
        # Mensagem de tool só aceita texto; imagens da ferramenta seguem numa
        # mensagem de usuário logo depois (``_to_openrouter_messages``).
        saida["content"] = msg.text()

    if role == "assistant" and msg.tool_calls:
        saida["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.args, ensure_ascii=False),
                },
            }
            for tc in msg.tool_calls
        ]
        saida["content"] = msg.text() or None

    return saida


def _to_openrouter_messages(
    messages: list[VMessage], accepted: AcceptedMedia | None = None
) -> list[JsonObject]:
    """Traduz o histórico e entrega imagens de ferramentas (screenshots).

    Depois de cada sequência de mensagens de tool, as imagens que elas
    trouxeram seguem numa mensagem de usuário, o formato que o Chat
    Completions aceita para imagem.
    """
    saida: list[JsonObject] = []
    pendentes: list[JsonValue] = []
    for msg in messages:
        if msg.role != MessageRole.TOOL and pendentes:
            saida.append({"role": "user", "content": pendentes})
            pendentes = []
        saida.append(_to_openrouter_message(msg, accepted))
        if msg.role == MessageRole.TOOL:
            imagens: list[JsonValue] = [
                {"type": "image_url", "image_url": {"url": b.image_url}}
                for b in msg.content
                if b.kind == "image_url" and b.image_url
            ]
            if imagens:
                pendentes.extend(
                    [
                        {
                            "type": "text",
                            "text": f"Imagem retornada pela ferramenta {msg.name or ''}:",
                        },
                        *imagens,
                    ]
                )
    if pendentes:
        saida.append({"role": "user", "content": pendentes})
    return saida


def _parse_tool_calls(bruto: list) -> list[ToolCall]:
    """`arguments` inválido não derruba a resposta — vira `_parse_error`
    (mesmo padrão de `backend/llm/openai/chat_client.py`), preservando o
    texto já gerado no turno."""
    chamadas: list[ToolCall] = []
    for item in bruto or []:
        funcao = item.get("function") or {}
        nome = funcao.get("name", "")
        args_texto = funcao.get("arguments") or "{}"
        try:
            args = coerce_json_object(json.loads(args_texto), raw=args_texto)
        except (json.JSONDecodeError, TypeError):
            args = {"_parse_error": args_texto}
        chamadas.append(ToolCall(id=item.get("id") or "", name=nome, args=args))
    return chamadas


def _parse_tool_call_chunks(bruto: list) -> list[ToolCallChunk]:
    """Executa a operação  parse tool call chunks."""
    pedacos: list[ToolCallChunk] = []
    for item in bruto or []:
        funcao = item.get("function") or {}
        pedacos.append(
            ToolCallChunk(
                index=item.get("index") or 0,
                id=item.get("id"),
                name=funcao.get("name"),
                args_fragment=funcao.get("arguments") or "",
            )
        )
    return pedacos


_CACHE_CONTROL: JsonObject = {"type": "ephemeral"}


def mark_cache_breakpoints(messages: list[JsonObject]) -> None:
    """Marca system e as duas últimas mensagens com ``cache_control``.

    O conteúdo em string vira uma lista de partes para poder carregar a marca.
    """
    targets: list[JsonObject] = []
    system = next((m for m in messages if m.get("role") == "system"), None)
    if system is not None:
        targets.append(system)
    targets.extend(m for m in messages[-2:] if m is not system)
    for message in targets:
        content: JsonValue = message.get("content")
        if isinstance(content, str):
            if not content:
                continue
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list) or not content:
            continue
        last = content[-1]
        if isinstance(last, dict) and last.get("type") == "text":
            marked: list[JsonValue] = [
                *content[:-1],
                {**last, "cache_control": _CACHE_CONTROL},
            ]
            message["content"] = marked


def _usage_metadata(usage: _UsagePayload | None) -> UsageSnapshot | None:
    """Executa a operação  usage metadata."""
    if not usage:
        return None
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    snapshot: UsageSnapshot = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
    }
    details = usage.get("prompt_tokens_details") or {}
    cached = as_int(details.get("cached_tokens") or 0)
    if cached:
        snapshot["cached_input_tokens"] = cached
    # OpenRouter chama de `cache_write_tokens`; 9Router e OmniRoute, de
    # `cache_creation_tokens`.
    written = as_int(
        details.get("cache_write_tokens") or details.get("cache_creation_tokens") or 0
    )
    if written:
        snapshot["cache_write_tokens"] = written
    raw_cost = usage.get("cost")
    if (
        isinstance(raw_cost, (int, float))
        and not isinstance(raw_cost, bool)
        and math.isfinite(raw_cost)
        and raw_cost >= 0
    ):
        snapshot["cost_usd"] = float(raw_cost)
    return snapshot


class OpenRouterChatClient:
    """Chat client nativo do OpenRouter — implementa o Protocol
    ``ChatClient``, async-only (CLAUDE.md regra 10)."""

    def __init__(
        self,
        model: str,
        client: OpenRouterClient,
        *,
        compact_system_prompt: bool = False,
        media_model_id: str | None = None,
    ) -> None:
        """Inicializa o estado da instância.

        ``media_model_id`` (``"openrouter:<modelo>"`` ou
        ``"nine_router:<modelo>"``) liga a consulta ao catálogo do gateway
        para enviar PDF, áudio e vídeo como arquivo aos modelos que aceitam.
        """
        self.model = model
        self.client = client
        self.compact_system_prompt = compact_system_prompt
        self.media_model_id = media_model_id

    async def _accepted_media(self, messages: list[VMessage]) -> AcceptedMedia:
        """Tipos aceitos pelo modelo, consultados só quando há arquivo."""
        if not self.media_model_id or not any(
            block.kind == "media" and (block.media_data_url or block.media_preview_urls)
            for message in messages
            for block in message.content
        ):
            return {}
        return await resolve_native_media(self.media_model_id)

    def _payload(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None,
        temperature: float | None,
        max_tokens: int | None,
        accepted: AcceptedMedia | None = None,
    ) -> dict:
        """Executa a operação  payload."""
        translated = _to_openrouter_messages(messages, accepted)
        body: JsonObject = {"model": self.model}
        if self.compact_system_prompt:
            from backend.llm.prompt_projection import compact_small_model_system_prompt

            for message in translated:
                content = message.get("content")
                if message.get("role") == "system" and isinstance(content, str):
                    message["content"] = compact_small_model_system_prompt(content)
                    break
        if temperature is not None:
            # Tiny local models often enter a deterministic repetition loop
            # at temperature 0 when the prompt contains identity/tool rules.
            # A small floor keeps coding deterministic enough while avoiding
            # known repetition loops in tiny local models.
            body["temperature"] = (
                max(0.2, temperature) if self.compact_system_prompt else temperature
            )
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = [chat_completions_tool(t) for t in tools]
        body["messages"] = [*translated]
        self._apply_prompt_cache(body)
        return body

    def _session_headers(self) -> dict[str, str] | None:
        """``x-session-id`` com o id da conversa para OpenRouter, 9Router e OmniRoute.

        Os três usam esse header como identidade da sessão (sticky routing e
        afinidade de cache); ele passa por proxies que descartam underscores.
        """
        origin = (self.media_model_id or "").partition(":")[0]
        if origin not in {"openrouter", "nine_router"}:
            return None
        key = prompt_cache_key.get()
        return {"x-session-id": key[:256]} if key else None

    def _apply_prompt_cache(self, body: JsonObject) -> None:
        """Cache de prompt conforme o backend por trás deste client.

        - OpenRouter com modelo Anthropic ou Gemini: marca system e as duas
          últimas mensagens com ``cache_control``.
        - llama.cpp: pede reaproveitar o KV cache do prefixo (``cache_prompt``).
        """
        from backend.settings import settings

        if not settings.prompt_cache_enabled:
            return
        origin = (self.media_model_id or "").partition(":")[0]
        if origin == "llamacpp":
            body["cache_prompt"] = True
        elif origin == "nine_router":
            # 9Router e OmniRoute leem `prompt_cache_key` como identidade da
            # conversa: mantém a sessão estável e manda o prefixo à mesma conta.
            cache_key = prompt_cache_key.get()
            if cache_key:
                body["prompt_cache_key"] = cache_key
        elif origin == "openrouter":
            # `session_id` fixa a conversa num provedor (sticky routing, 60 min):
            # sem isso o acerto de cache depende de o roteador cair no mesmo lugar.
            session_id = prompt_cache_key.get()
            if session_id:
                body["session_id"] = session_id
            if self.model.startswith(("anthropic/", "google/gemini")):
                sent = body.get("messages")
                mark_cache_breakpoints(
                    [m for m in sent if isinstance(m, dict)]
                    if isinstance(sent, list)
                    else []
                )

    @staticmethod
    def _first_choice(body: dict) -> dict:
        """Executa a operação  primeira escolha."""
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            msg = (
                "OpenRouter respondeu sem `choices` — resposta inutilizável "
                f"(id={body.get('id')!r})"
            )
            raise OpenRouterResponseError(msg)
        return choices[0]

    async def agenerate(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> VMessage:
        """Executa a operação agenerate."""
        body = self._payload(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            accepted=await self._accepted_media(messages),
        )
        response = await self.client.post_json(
            "/chat/completions", body, headers=self._session_headers()
        )
        escolha = self._first_choice(response)
        mensagem = escolha.get("message") or {}

        chamadas = _parse_tool_calls(mensagem.get("tool_calls") or [])
        texto = mensagem.get("content") or ""
        content: list[ContentBlock] = []
        raciocinio = mensagem.get("reasoning")
        if raciocinio:
            content.append(ContentBlock(kind="reasoning", reasoning_text=raciocinio))
        if texto:
            content.append(ContentBlock(kind="text", text=texto))

        return VMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=chamadas,
            finish_reason="tool_calls" if chamadas else "stop",
        )

    async def astream(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[VMessageChunk]:
        """Executa a operação astream."""
        body = self._payload(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            accepted=await self._accepted_media(messages),
        )

        async for event in self.client.stream_sse(
            "/chat/completions", body, headers=self._session_headers()
        ):
            # O OpenRouter manda `usage` no último evento SSE do stream —
            # às vezes junto de `choices` vazio, às vezes acompanhando o
            # chunk final com finish_reason. Captura nos dois casos.
            usage = event.get("usage")
            if usage:
                yield VMessageChunk(usage=_usage_metadata(usage))

            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            raciocinio = delta.get("reasoning")
            if raciocinio:
                yield VMessageChunk(delta_reasoning=raciocinio)

            tool_calls_bruto = delta.get("tool_calls")
            if tool_calls_bruto:
                yield VMessageChunk(
                    tool_call_chunks=_parse_tool_call_chunks(tool_calls_bruto)
                )

            texto = delta.get("content")
            if texto:
                yield VMessageChunk(delta_text=texto)
