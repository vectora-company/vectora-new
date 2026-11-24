"""Testes do contrato de estudos técnicos."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.cli.eval import _handle_study_validate
from backend.evaluation.studies import TechnicalStudy


def _study(**updates: object) -> dict[str, object]:
    study: dict[str, object] = {
        "study_id": "context-cache-study",
        "title": "Teste de hipótese de cache",
        "state": "planned",
        "claim_under_test": "O cache reduz latência sem alterar a resposta.",
        "hypothesis": "Com cache aquecido, p50 de latência cai ao menos 10%.",
        "falsification_criterion": "Refutar se a queda for menor que 10%.",
        "method": "Executar pares frio/quente com a mesma fixture e configuração.",
        "sample": "30 pares em Linux x86_64 com o mesmo modelo local.",
        "environment": {"os": "Linux", "hardware": "x86_64"},
        "independent_variable": "Estado do cache (frio ou aquecido).",
        "controls": ["Mesmo modelo, prompt, fixture e limite de tokens."],
        "decision_rule": "Comparar p50 após validar equivalência funcional.",
        "conclusion": "not_run",
        "evidence": [
            {
                "evidence_id": "claim-1",
                "kind": "external_claim",
                "source": "https://example.invalid/paper",
                "observation": "O artigo relata melhora de latência.",
                "limitations": ["Resultado externo ainda não reproduzido."],
            }
        ],
        "observed_results": [],
        "limitations": ["A amostra usa apenas um host e um modelo."],
        "related_code": [],
    }
    study.update(updates)
    return study


def test_planned_study_keeps_external_claim_as_unverified() -> None:
    study = TechnicalStudy.model_validate(_study())

    assert study.state == "planned"
    assert study.conclusion == "not_run"
    assert study.evidence[0].kind == "external_claim"


def test_completed_study_requires_observed_results_and_direct_evidence() -> None:
    study = _study(
        state="completed",
        conclusion="supported",
        observed_results=["p50 caiu 12%; todas as respostas passaram na validação."],
    )

    with pytest.raises(ValidationError, match="evidência de código ou experimento"):
        TechnicalStudy.model_validate(study)

    study["evidence"] = [
        {
            "evidence_id": "run-1",
            "kind": "reproduced_experiment",
            "source": "run://reports/eval.json#eval-2026-11-24-001",
            "evidence_level": "fixture_runner",
            "observation": "30 pares válidos; p50 caiu 12%.",
            "limitations": ["Um host e um modelo."],
        }
    ]
    assert TechnicalStudy.model_validate(study).conclusion == "supported"


def test_study_cannot_conclude_before_running_or_duplicate_evidence_ids() -> None:
    with pytest.raises(ValidationError, match="estudo planejado"):
        TechnicalStudy.model_validate(_study(conclusion="supported"))

    duplicate_evidence = {
        "evidence_id": "claim-1",
        "kind": "external_claim",
        "source": "https://example.invalid/paper",
        "observation": "O artigo relata melhora de latência.",
        "limitations": ["Resultado externo ainda não reproduzido."],
    }
    duplicated = _study(evidence=[duplicate_evidence, duplicate_evidence.copy()])
    with pytest.raises(ValidationError, match="evidence_id precisa ser único"):
        TechnicalStudy.model_validate(duplicated)


def test_cli_validates_a_study_without_claiming_it_was_executed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "study.json"
    path.write_text(json.dumps(_study()), encoding="utf-8")

    result = _handle_study_validate(Namespace(study=str(path), output="human"))

    output = capsys.readouterr().out
    assert result == 0
    assert "estado=planned, conclusão=not_run" in output
    assert "não executa o experimento" in output


def test_template_is_valid_as_a_plan() -> None:
    path = Path("backend/evaluation/studies/v1/template.json")

    study = TechnicalStudy.model_validate_json(path.read_text(encoding="utf-8"))

    assert study.state == "planned"
    assert study.conclusion == "not_run"
