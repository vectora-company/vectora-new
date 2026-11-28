"""Partes dos objetos das plataformas de chat que os adapters usam."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import asyncio

    from discord import Client
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_slack_response import AsyncSlackResponse
    from telegram import Message as TelegramMessage
    from telegram.ext import Application, ContextTypes, ExtBot, JobQueue


class PlatformId(Protocol):
    """Autor, canal ou chat identificado por número."""

    @property
    def id(self) -> int: ...


class DiscordIncoming(Protocol):
    """Mensagem recebida do Discord."""

    @property
    def author(self) -> PlatformId: ...
    @property
    def channel(self) -> PlatformId: ...
    @property
    def content(self) -> str | None: ...


class TelegramText(Protocol):
    """Mensagem recebida do Telegram."""

    @property
    def text(self) -> str | None: ...
    @property
    def caption(self) -> str | None: ...
    @property
    def chat(self) -> PlatformId: ...


class TelegramIncoming(Protocol):
    """Atualização recebida do Telegram."""

    @property
    def message(self) -> TelegramText | None: ...


class SlackSay(Protocol):
    """Função do Bolt que responde no canal do evento."""

    def __call__(self, text: str) -> Awaitable[AsyncSlackResponse | None]: ...


type TelegramData = dict[str, str]
type TelegramApp = Application[
    ExtBot[None],
    ContextTypes.DEFAULT_TYPE,
    TelegramData,
    TelegramData,
    TelegramData,
    JobQueue[ContextTypes.DEFAULT_TYPE],
]
type ConnectHandle = TelegramApp | Client | AsyncSocketModeHandler | asyncio.Task[None]


class TelegramReplyable(TelegramText, Protocol):
    """Mensagem do Telegram que aceita resposta em texto."""

    def reply_text(self, text: str, /) -> Awaitable[TelegramMessage | None]: ...


class TelegramUpdateWithReply(Protocol):
    """Atualização entregue ao handler registrado no ``Application``."""

    @property
    def message(self) -> TelegramReplyable | None: ...
