"""Registros devolvidos pelos armazenamentos de sessão do motor nativo.

Os armazenamentos SQLite e Postgres devolvem estas mesmas formas, o que
permite trocar um pelo outro sem que o consumidor adivinhe chaves.
"""

from __future__ import annotations

from typing import TypedDict

from backend.vtypes.contracts import JsonObject, JsonValue, ToolOption


class SessionRecord(TypedDict):
    """Posse e configuração de uma sessão registrada."""

    thread_id: str
    user_id: str
    workspace_id: str | None
    parent_thread_id: str | None
    mode: str
    permission_mode: str
    created_at: str
    updated_at: str


class MessageSearchHit(TypedDict):
    """Trecho encontrado pela busca textual nas mensagens."""

    thread_id: str
    role: str
    snippet: str
    created_at: str


class ThreadTokenUsage(TypedDict):
    """Tokens somados das execuções de uma conversa."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    runs: int
    cached_input_tokens: int
    cache_write_tokens: int
    model: str


class BranchHead(TypedDict):
    """Ponta persistida de uma conversa."""

    head_message_id: int
    created_at: str
    active: bool
    message_count: int


class BranchComparison(TypedDict):
    """Diferença entre a ponta ativa e outra ponta da mesma conversa."""

    active_head_message_id: int | None
    selected_head_message_id: int
    common_message_ids: list[int]
    active_divergent_message_ids: list[int]
    selected_divergent_message_ids: list[int]


class PendingApprovalRecord(TypedDict):
    """Aprovação aguardando decisão do usuário."""

    interrupt_id: str
    tool_name: str
    tool_call_id: str
    run_id: str | None
    args: JsonObject
    reasoning: str | None
    options: list[ToolOption]
    priority: int
    expires_at: str | None
    created_at: str


class ContextProjectionRecord(TypedDict):
    """Projeção de contexto persistida fora do transcript canônico."""

    projection_id: str
    run_id: str
    source_hash: str
    covered_from_sequence: int
    covered_to_sequence: int
    model: str
    policy_version: str
    tokens_before: int
    tokens_after: int
    preserved_decisions: list[JsonValue]
    preserved_errors: list[JsonValue]
    preserved_approvals: list[JsonValue]
    pending_tool_calls: list[JsonValue]
    omitted_sequences: list[JsonValue]
    content: JsonValue
    created_at: str


class ContextCheckpointRecord(TypedDict):
    """Estado mínimo para retomar uma execução por snapshot e deltas."""

    checkpoint_id: str
    run_id: str
    last_sequence: int
    source_hash: str
    schema_version: str
    state: JsonObject
    artifact_refs: list[JsonValue]
    created_at: str


class PurgedContextCount(TypedDict):
    """Quantidade de registros de contexto removidos pela retenção."""

    projections: int
    checkpoints: int


class RunEvidence(TypedDict, total=False):
    """Evidências incrementais aceitas por um AgentRun."""

    evidence: JsonObject | None
    artifact: JsonObject | None
    command: JsonObject | None
    test_result: JsonObject | None
    tool_used: str | None
    approval: JsonObject | None
    file_touched: JsonObject | None
