"""Contratos para estudos técnicos rastreáveis e refutáveis."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvidenceKind = Literal[
    "external_claim", "source_code", "reproduced_experiment", "inference"
]
StudyState = Literal["planned", "completed"]
StudyConclusion = Literal["not_run", "supported", "refuted", "inconclusive"]


class StudyEvidence(BaseModel):
    """Uma referência identificável com o papel epistêmico declarado."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    kind: EvidenceKind
    source: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)


class TechnicalStudy(BaseModel):
    """Plano ou conclusão de estudo sem confundir alegação com réplica."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    study_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    title: str = Field(min_length=1)
    state: StudyState
    claim_under_test: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    falsification_criterion: str = Field(min_length=1)
    method: str = Field(min_length=1)
    sample: str = Field(min_length=1)
    environment: dict[str, str] = Field(min_length=1)
    independent_variable: str = Field(min_length=1)
    controls: list[str] = Field(min_length=1)
    decision_rule: str = Field(min_length=1)
    conclusion: StudyConclusion
    evidence: list[StudyEvidence] = Field(min_length=1)
    observed_results: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)
    related_code: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_and_conclusion(self) -> TechnicalStudy:
        """Exige evidência adequada para o estado e a força da conclusão."""
        kinds = {item.kind for item in self.evidence}
        if self.state == "planned":
            if self.conclusion != "not_run" or self.observed_results:
                raise ValueError("estudo planejado deve usar not_run sem resultados")
        elif not ("source_code" in kinds or "reproduced_experiment" in kinds):
            raise ValueError(
                "estudo concluído exige evidência de código ou experimento reproduzido"
            )
        elif not self.observed_results:
            raise ValueError("estudo concluído precisa registrar resultados observados")

        if self.conclusion in {"supported", "refuted"} and not (
            "source_code" in kinds or "reproduced_experiment" in kinds
        ):
            raise ValueError(
                "alegação externa ou inferência isolada não sustenta conclusão"
            )
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("evidence_id precisa ser único no estudo")
        return self
