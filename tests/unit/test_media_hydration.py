"""Imagens anexadas chegam ao provider depois de passar pelo histórico.

Regressão: o histórico persistido zera ``image_url`` de blocos com
``asset_id`` e o loop reconstrói cada turno a partir dele. Sem a
reidratação, a imagem anexada nunca chegava ao modelo.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from backend.api.handlers.chat import _build_user_vmessage
from backend.api.schemas import Attachment, AttachmentKind
from backend.persistence.native.session_store import SessionStore
from backend.services.assets import AssetStore
from backend.services.media_hydration import hydrate_media_blocks
from backend.settings import settings
from backend.storage.sqlite.pool import AsyncConnectionPool
from backend.vtypes.message import ContentBlock, MessageRole, VMessage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
async def store(tmp_path: Path):
    pool = AsyncConnectionPool(str(tmp_path / "sessions.db"), min_size=1, max_size=2)
    await pool.open()
    session_store = SessionStore(pool)
    await session_store.setup()
    try:
        yield session_store
    finally:
        await pool.close()


@pytest.fixture
def assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AssetStore:
    monkeypatch.setattr(settings, "vectora_home", tmp_path)
    asset_store = AssetStore()
    monkeypatch.setattr("backend.services.assets.asset_store", asset_store)
    return asset_store


async def test_imagem_anexada_chega_ao_provider_apos_o_historico(
    store: SessionStore, assets: AssetStore
) -> None:
    thread_id = "thread-img"
    await store.create_session(thread_id, user_id="user-1", workspace_id="ws-1")
    message = await _build_user_vmessage(
        "o que tem nesta imagem?",
        [
            Attachment(
                kind=AttachmentKind.IMAGE,
                name="foto.png",
                mime_type="image/png",
                base64_data=base64.b64encode(PNG).decode(),
            )
        ],
        thread_id,
        user_id="user-1",
        workspace_id="ws-1",
    )
    await store.append_message(thread_id, message)

    history = await store.get_history(thread_id)
    stored_block = history[-1].content[1]
    assert stored_block.kind == "image_url"
    assert stored_block.image_url is None
    assert stored_block.asset_id

    sent = hydrate_media_blocks(
        history, user_id="user-1", workspace_id="ws-1", thread_id=thread_id
    )
    expected = "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert sent[-1].content[1].image_url == expected
    # A cópia persistida continua sem os bytes.
    assert (await store.get_history(thread_id))[-1].content[1].image_url is None


async def test_asset_de_outro_usuario_vira_aviso_e_nao_vaza(
    store: SessionStore, assets: AssetStore
) -> None:
    thread_id = "thread-outro"
    await store.create_session(thread_id, user_id="user-1", workspace_id="ws-1")
    message = await _build_user_vmessage(
        "veja",
        [
            Attachment(
                kind=AttachmentKind.IMAGE,
                name="foto.png",
                mime_type="image/png",
                base64_data=base64.b64encode(PNG).decode(),
            )
        ],
        thread_id,
        user_id="user-1",
        workspace_id="ws-1",
    )
    await store.append_message(thread_id, message)

    sent = hydrate_media_blocks(
        await store.get_history(thread_id),
        user_id="intruso",
        workspace_id="ws-1",
        thread_id=thread_id,
    )
    block = sent[-1].content[1]
    assert block.kind == "text"
    assert block.text is not None and "indisponível" in block.text


def test_bloco_sem_asset_vira_aviso_e_mensagens_sem_midia_sao_reusadas() -> None:
    plain = VMessage(
        role=MessageRole.USER, content=[ContentBlock(kind="text", text="oi")]
    )
    orphan = VMessage(
        role=MessageRole.USER,
        content=[ContentBlock(kind="image_url", attachment_name="a.png")],
    )
    sent = hydrate_media_blocks(
        [plain, orphan],
        user_id="u",
        workspace_id="",
        thread_id="t",
        reader=lambda *_a, **_k: None,
    )
    assert sent[0] is plain
    assert sent[1].content[0].kind == "text"
    assert sent[1].content[0].text == "[Imagem anexada indisponível: a.png]"


def test_url_inline_e_remota_nao_sao_alteradas() -> None:
    remote = ContentBlock(kind="image_url", image_url="https://example.com/a.png")
    message = VMessage(role=MessageRole.USER, content=[remote])

    def _fail[**P](*_a: P.args, **_k: P.kwargs) -> None:
        raise AssertionError("não deveria ler asset")

    assert hydrate_media_blocks(
        [message], user_id="u", workspace_id="", thread_id="t", reader=_fail
    ) == [message]


async def test_loop_envia_a_imagem_do_historico_ao_chat_client(
    store: SessionStore, assets: AssetStore
) -> None:
    from backend.engine.conversation_loop import LoopConfig, run_conversation
    from backend.tools.context import ToolContext
    from backend.tools.registry import ToolRegistry
    from backend.vtypes.message import VMessageChunk

    thread_id = "thread-loop"
    await store.create_session(thread_id, user_id="alice", workspace_id="ws-1")
    message = await _build_user_vmessage(
        "descreva",
        [
            Attachment(
                kind=AttachmentKind.IMAGE,
                name="foto.png",
                mime_type="image/png",
                base64_data=base64.b64encode(PNG).decode(),
            )
        ],
        thread_id,
        user_id="alice",
        workspace_id="ws-1",
    )
    await store.append_message(thread_id, message)

    received: list[list[VMessage]] = []

    class _Client:
        async def astream(
            self, messages, *, tools=None, temperature=None, max_tokens=None
        ):
            received.append(list(messages))
            yield VMessageChunk(delta_text="uma imagem")

        async def agenerate(
            self, messages, *, tools=None, temperature=None, max_tokens=None
        ):
            raise NotImplementedError

    await run_conversation(
        session_store=store,
        chat_client=_Client(),
        tool_registry=ToolRegistry(),
        ctx=ToolContext(user_id="alice", workspace_id="ws-1", thread_id=thread_id),
        thread_id=thread_id,
        config=LoopConfig(),
    )

    image_blocks = [
        block
        for sent in received[0]
        for block in sent.content
        if block.kind == "image_url"
    ]
    assert [block.image_url for block in image_blocks] == [
        "data:image/png;base64," + base64.b64encode(PNG).decode()
    ]


async def test_anexos_sobrevivem_tool_call_e_recarga_do_historico(
    store: SessionStore, assets: AssetStore
) -> None:
    """Imagens e arquivo textual continuam no contexto após uma tool call."""
    from pydantic import create_model

    from backend.engine.conversation_loop import LoopConfig, run_conversation
    from backend.tools.context import ToolContext
    from backend.tools.registry import ToolExtras, ToolRegistry, ToolSpec
    from backend.vtypes.message import ToolCallChunk, VMessageChunk

    thread_id = "thread-attachments-tool"
    await store.create_session(thread_id, user_id="alice", workspace_id="ws-1")
    image_a = PNG + b"image-a"
    image_b = PNG + b"image-b"
    text_file = "const answer = 42;\nconsole.log(answer);\n"
    message = await _build_user_vmessage(
        "Compare as duas capturas com este código.",
        [
            Attachment(
                kind=AttachmentKind.IMAGE,
                name="primeira.png",
                mime_type="image/png",
                base64_data=base64.b64encode(image_a).decode(),
            ),
            Attachment(
                kind=AttachmentKind.IMAGE,
                name="segunda.png",
                mime_type="image/png",
                base64_data=base64.b64encode(image_b).decode(),
            ),
            Attachment(
                kind=AttachmentKind.CODE,
                name="exemplo.js",
                mime_type="text/plain",
                base64_data=base64.b64encode(text_file.encode()).decode(),
            ),
        ],
        thread_id,
        user_id="alice",
        workspace_id="ws-1",
    )
    await store.append_message(thread_id, message)

    tool_calls: list[str] = []

    async def inspect_project() -> str:
        tool_calls.append("called")
        return "inspeção concluída"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="inspect_project",
            description="Inspeciona o projeto.",
            args_model=create_model("AttachmentRegressionArgs"),
            handler=inspect_project,
            extras=ToolExtras(destructive=False),
            needs_ctx=False,
        )
    )

    received: list[list[VMessage]] = []

    class _Client:
        async def astream(
            self, messages, *, tools=None, temperature=None, max_tokens=None
        ):
            received.append(list(messages))
            if len(received) == 1:
                yield VMessageChunk(
                    tool_call_chunks=[
                        ToolCallChunk(
                            index=0,
                            id="call-inspect",
                            name="inspect_project",
                            args_fragment="{}",
                        )
                    ],
                    finish_reason="tool_calls",
                )
            else:
                yield VMessageChunk(delta_text="Comparei as imagens e o código.")

        async def agenerate(
            self, messages, *, tools=None, temperature=None, max_tokens=None
        ):
            raise NotImplementedError

    await run_conversation(
        session_store=store,
        chat_client=_Client(),
        tool_registry=registry,
        ctx=ToolContext(user_id="alice", workspace_id="ws-1", thread_id=thread_id),
        thread_id=thread_id,
        config=LoopConfig(),
    )

    assert tool_calls == ["called"]
    assert len(received) == 2
    second_request = received[1]
    images = [
        block.image_url
        for sent in second_request
        for block in sent.content
        if block.kind == "image_url"
    ]
    assert images == [
        "data:image/png;base64," + base64.b64encode(image_a).decode(),
        "data:image/png;base64," + base64.b64encode(image_b).decode(),
    ]
    text_blocks = [
        block.text or ""
        for sent in second_request
        for block in sent.content
        if block.kind in {"text", "media"}
    ]
    assert any("Compare as duas capturas" in text for text in text_blocks)
    assert any(text_file in text for text in text_blocks)
    assert any(
        sent.role is MessageRole.TOOL and "inspeção concluída" in sent.text()
        for sent in second_request
    )
    reloaded = await store.get_history(thread_id)
    persisted_user = next(item for item in reloaded if item.role is MessageRole.USER)
    persisted_images = [
        block for block in persisted_user.content if block.kind == "image_url"
    ]
    assert len(persisted_images) == 2
    assert all(block.asset_id and block.image_url is None for block in persisted_images)
    assert any(
        block.kind == "media" and text_file in (block.text or "")
        for block in persisted_user.content
    )
    assert any(item.role is MessageRole.TOOL for item in reloaded)
    assert any(
        item.role is MessageRole.ASSISTANT
        and "Comparei as imagens e o código." in item.text()
        for item in reloaded
    )

    after_reload = hydrate_media_blocks(
        reloaded,
        user_id="alice",
        workspace_id="ws-1",
        thread_id=thread_id,
    )
    reloaded_user = next(item for item in after_reload if item.role is MessageRole.USER)
    assert [
        block.image_url for block in reloaded_user.content if block.kind == "image_url"
    ] == images
