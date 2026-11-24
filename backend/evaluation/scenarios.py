"""Catálogo versionado e planejamento honesto de cenários de avaliação."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ScenarioStatus = Literal["passed", "failed", "blocked", "skipped", "not_run"]
_CATALOG = Path(__file__).parent / "scenarios" / "v1" / "catalog.json"


class BenchmarkScenario(BaseModel):
    """Contrato de cenário, fixture imutável e evidência esperada."""

    schema_version: Literal[1] = 1
    scenario_id: str
    category: str
    capability: str
    task: str
    fixture: str
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context: dict[str, object] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    policy: dict[str, object] = Field(default_factory=dict)
    setup: list[str] = Field(default_factory=list)
    teardown: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(min_length=1)
    required_capabilities: list[str] = Field(default_factory=list)
    requires_provider: bool = False
    remote_provider_allowed: bool = False


class ScenarioReadiness(BaseModel):
    """Resultado de prontidão sem alegar que o cenário foi executado."""

    scenario_id: str
    status: ScenarioStatus
    reason: str | None = None


def load_scenarios(catalog_path: Path | str = _CATALOG) -> list[BenchmarkScenario]:
    """Carrega o catálogo e valida fixtures, hashes e caminhos confinados."""
    path = Path(catalog_path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("versão de catálogo de cenários não suportada")
    entries = raw.get("scenarios")
    if not isinstance(entries, list):
        raise ValueError("catálogo precisa conter uma lista scenarios")
    scenarios = [BenchmarkScenario.model_validate(entry) for entry in entries]
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario_id duplicado no catálogo")

    root = path.parent.resolve()
    for scenario in scenarios:
        fixture_path = (root / scenario.fixture).resolve()
        if not fixture_path.is_relative_to(root):
            raise ValueError(f"fixture fora do catálogo: {scenario.scenario_id}")
        payload = fixture_path.read_bytes()
        actual_digest = hashlib.sha256(payload).hexdigest()
        if actual_digest != scenario.fixture_sha256:
            raise ValueError(f"hash de fixture inválido: {scenario.scenario_id}")
        json.loads(payload)
    return scenarios


def assess_scenario_readiness(
    scenario: BenchmarkScenario,
    *,
    available_capabilities: set[str],
    provider_configured: bool = False,
    provider_is_remote: bool = False,
    remote_provider_opt_in: bool = False,
) -> ScenarioReadiness:
    """Classifica pré-condições; só execução real pode retornar `passed`."""
    missing = sorted(set(scenario.required_capabilities) - available_capabilities)
    if missing:
        return ScenarioReadiness(
            scenario_id=scenario.scenario_id,
            status="blocked",
            reason=f"capabilities ausentes: {', '.join(missing)}",
        )
    if scenario.requires_provider and not provider_configured:
        return ScenarioReadiness(
            scenario_id=scenario.scenario_id,
            status="blocked",
            reason="provider necessário não está configurado",
        )
    if (
        scenario.requires_provider
        and provider_is_remote
        and not scenario.remote_provider_allowed
    ):
        return ScenarioReadiness(
            scenario_id=scenario.scenario_id,
            status="blocked",
            reason="a política do cenário não permite provider remoto",
        )
    if scenario.requires_provider and provider_is_remote and not remote_provider_opt_in:
        return ScenarioReadiness(
            scenario_id=scenario.scenario_id,
            status="skipped",
            reason="opt-in para provider remoto não foi concedido",
        )
    return ScenarioReadiness(
        scenario_id=scenario.scenario_id,
        status="not_run",
        reason="pré-condições disponíveis; executor do fluxo ainda não foi iniciado",
    )
