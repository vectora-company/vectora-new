"""Relatórios separados para replay determinístico de cassetes."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from backend.evaluation.cassette import Cassette, CassetteInteraction
from backend.evaluation.replay_client import ReplayChatClient
from backend.persistence.native.agent_run import redact_sensitive_data
from backend.vtypes.message import VMessage

ReplayCaseStatus = Literal["replayed", "failed", "unsupported"]
ReplayReportStatus = Literal["replayed", "failed", "unsupported"]


if TYPE_CHECKING:
    from backend.vtypes.contracts import JsonObject, JsonValue


class ReplayCaseResult(BaseModel):
    """Resultado de uma interação reproduzida, sem copiar seu conteúdo."""

    interaction_id: str
    status: ReplayCaseStatus
    replay_duration_ms: float = Field(ge=0)
    recorded_duration_ms: int = Field(ge=0)
    recorded_tool_call_count: int = Field(ge=0)
    reason: str | None = None


class FixtureReplayReport(BaseModel):
    """Evidência de replay, explicitamente distinta de execução live."""

    schema_version: Literal[1] = 1
    mode: Literal["fixture_replay"] = "fixture_replay"
    evidence_level: Literal["fixture_replay"] = "fixture_replay"
    status: ReplayReportStatus
    report_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    cassette_id: str
    source_run_id: str | None = None
    model: str | None = None
    command: list[str]
    app_installed_tested: Literal[False] = False
    real_provider_tested: Literal[False] = False
    external_effects_executed: Literal[False] = False
    summary: dict[str, int]
    cases: list[ReplayCaseResult] = Field(default_factory=list)

    def to_markdown(self) -> str:
        """Renderiza um resumo que não inclui prompts ou respostas gravadas."""
        rows = [
            "# Relatório de replay determinístico",
            "",
            f"- Modo: `{self.mode}`",
            f"- Estado: `{self.status}`",
            f"- Cassete: `{self.cassette_id}`",
            f"- Run de origem: `{self.source_run_id or 'não informado'}`",
            f"- Modelo gravado: `{self.model or 'não informado'}`",
            "- App instalado testado: `não`",
            "- Provider real chamado: `não`",
            "- Efeitos externos executados: `não`",
            "",
            "| Interação | Estado | Replay (ms) | Original (ms) | Tool calls gravadas | Motivo |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for case in self.cases:
            reason = (case.reason or "").replace("|", "\\|").replace("\n", " ")
            rows.append(
                f"| `{case.interaction_id}` | {case.status} | "
                f"{case.replay_duration_ms:.3f} | {case.recorded_duration_ms} | "
                f"{case.recorded_tool_call_count} | {reason} |"
            )
        rows.extend(
            [
                "",
                (
                    "Este relatório valida somente a reprodução local do contrato da cassete. "
                    "Ele não comprova funcionamento da interface, do app instalado, do provider, "
                    "da qualidade da resposta nem a repetição de efeitos de tools."
                ),
                "",
            ]
        )
        return "\n".join(rows)


def _to_vmessage(raw: Mapping[str, JsonValue]) -> VMessage:
    """Converte uma mensagem validada da cassete para o modelo do runtime."""
    payload: JsonObject = dict(raw)
    payload.setdefault("role", "user")
    content = payload.get("content", "")
    if isinstance(content, str):
        payload["content"] = [{"kind": "text", "text": content}]
    return TypeAdapter(VMessage).validate_python(payload)


def _approval_evidence_error(interaction: CassetteInteraction) -> str | None:
    """Reject replay as evidence when a recorded human gate lacks its audit trail."""
    evidence = interaction.systemone_decision_evidence
    if not isinstance(evidence, dict):
        return None
    decisions = evidence.get("decisions")
    approval_events = evidence.get("approval_events")
    if not isinstance(decisions, list):
        return None
    events = approval_events if isinstance(approval_events, list) else []
    for decision in decisions:
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != "requires_human"
        ):
            continue
        tool_call_id = decision.get("tool_call_id")
        matching = [
            event
            for event in events
            if isinstance(event, dict) and event.get("tool_call_id") == tool_call_id
        ]
        request = next((event for event in matching if "source" not in event), None)
        if request is None:
            return "decisão exige aprovação humana, mas não há pedido registrado"
        resolved = any(
            event.get("source") == "human"
            and isinstance(event.get("decision"), str)
            and event["decision"] in {"approve", "edit", "option"}
            and event.get("interrupt_id") == request.get("interrupt_id")
            for event in matching
        )
        if not resolved:
            return "pedido de aprovação humana não tem decisão final registrada"
    return None


async def replay_cassette(
    cassette: Cassette, *, command: list[str]
) -> FixtureReplayReport:
    """Reproduz interações uma a uma sem instanciar provider ou tools externas."""
    cases: list[ReplayCaseResult] = []
    for interaction in cassette.interactions:
        started_ns = time.monotonic_ns()
        approval_error = _approval_evidence_error(interaction)
        if approval_error:
            cases.append(
                ReplayCaseResult(
                    interaction_id=interaction.interaction_id,
                    status="unsupported",
                    replay_duration_ms=0,
                    recorded_duration_ms=interaction.duration_ms,
                    recorded_tool_call_count=len(interaction.tool_calls),
                    reason=approval_error,
                )
            )
            continue
        if not interaction.messages:
            cases.append(
                ReplayCaseResult(
                    interaction_id=interaction.interaction_id,
                    status="unsupported",
                    replay_duration_ms=0,
                    recorded_duration_ms=interaction.duration_ms,
                    recorded_tool_call_count=len(interaction.tool_calls),
                    reason="interação sem mensagens de entrada não pode ser verificada",
                )
            )
            continue
        try:
            messages = [_to_vmessage(item) for item in interaction.messages]
            single_interaction = cassette.model_copy(
                update={"interactions": [interaction]}
            )
            client = ReplayChatClient(single_interaction)
            await client.agenerate(messages)
        except Exception as exc:
            if isinstance(exc, ValidationError):
                reason = "mensagem da cassete não corresponde ao schema do runtime"
            elif isinstance(exc, RuntimeError):
                reason = "histórico gravado não corresponde à entrada reproduzida"
            else:
                reason = f"falha de replay ({type(exc).__name__})"
            cases.append(
                ReplayCaseResult(
                    interaction_id=interaction.interaction_id,
                    status="failed",
                    replay_duration_ms=max(0, time.monotonic_ns() - started_ns)
                    / 1_000_000,
                    recorded_duration_ms=interaction.duration_ms,
                    recorded_tool_call_count=len(interaction.tool_calls),
                    reason=reason,
                )
            )
        else:
            cases.append(
                ReplayCaseResult(
                    interaction_id=interaction.interaction_id,
                    status="replayed",
                    replay_duration_ms=max(0, time.monotonic_ns() - started_ns)
                    / 1_000_000,
                    recorded_duration_ms=interaction.duration_ms,
                    recorded_tool_call_count=len(interaction.tool_calls),
                )
            )

    replayed = sum(case.status == "replayed" for case in cases)
    failed = sum(case.status == "failed" for case in cases)
    unsupported = sum(case.status == "unsupported" for case in cases)
    status: ReplayReportStatus = (
        "failed"
        if failed
        else "unsupported"
        if unsupported or not cases
        else "replayed"
    )
    raw = FixtureReplayReport(
        status=status,
        report_id=uuid4().hex,
        cassette_id=cassette.cassette_id,
        source_run_id=cassette.source_run_id,
        model=cassette.model or None,
        command=command,
        summary={"replayed": replayed, "failed": failed, "unsupported": unsupported},
        cases=cases,
    )
    return FixtureReplayReport.model_validate(
        redact_sensitive_data(raw.model_dump(mode="json"))
    )


def write_replay_report(
    report: FixtureReplayReport, report_dir: str
) -> tuple[str, str]:
    """Cria uma pasta nova com JSON e Markdown, sem sobrescrever relatórios."""
    from pathlib import Path

    target = Path(report_dir).expanduser()
    target.mkdir(parents=True, exist_ok=False)
    json_path = target / "report.json"
    markdown_path = target / "report.md"
    try:
        json_path.write_text(
            json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    except Exception:
        import shutil

        shutil.rmtree(target, ignore_errors=True)
        raise
    return str(json_path), str(markdown_path)
