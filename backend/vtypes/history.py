"""Validated contracts exchanged by the native history service and API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from backend.vtypes.contracts import JsonObject


class HistoryDiffHunk(BaseModel):
    """Serializable diff hunk stored with an edited file."""

    header: str = ""
    lines: list[str] = Field(default_factory=list)


class HistoryEditedFile(BaseModel):
    """Validated file-change payload attached to one assistant message."""

    path: str
    status: str = "M"
    additions: int = 0
    deletions: int = 0
    hunks: list[HistoryDiffHunk] = Field(default_factory=list)


class HistoryReasoningPart(BaseModel):
    """Raciocínio que o provider devolveu para a mensagem."""

    kind: Literal["reasoning"] = "reasoning"
    text: str


class HistoryToolCallPart(BaseModel):
    """Tool call da mensagem com o resultado resumido, quando já existe."""

    kind: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool_name: str
    args_json: str = "{}"
    render_hint: str = "json"
    category: str = "general"
    destructive: bool = False
    icon: str = "tool"
    # Resultado cortado em ``RESULT_PREVIEW_CHARS``; ``None`` se a tool não terminou.
    result_json: str | None = None
    is_error: bool | None = None


class HistoryVerification(BaseModel):
    """Desfecho da verificação do turno que gerou a mensagem."""

    status: str
    outcome: str
    detail: str = ""


HistoryPart = Annotated[
    HistoryReasoningPart | HistoryToolCallPart, Field(discriminator="kind")
]

# Tamanho máximo do resultado de tool devolvido no histórico.
RESULT_PREVIEW_CHARS = 4000


class ThreadHistoryEntry(BaseModel):
    """Named, validated history entry returned by the native history service."""

    role: Literal["human", "assistant"]
    text: str
    checkpoint_id: str
    message_id: int | None = None
    # ``None`` means the storage snapshot could not provide the flag.  It is
    # intentionally distinct from ``False`` so a transient read failure never
    # masquerades as a known non-error message.
    is_error: bool | None = False
    parts: list[HistoryPart] = Field(default_factory=list)
    verification: HistoryVerification | None = None
    attachments: list[JsonObject] = Field(default_factory=list)
    edited_files: list[HistoryEditedFile] = Field(default_factory=list)
