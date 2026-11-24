"""Prompts should keep the agent responsible for completing delegated work."""

from __future__ import annotations

from backend.agents.souls import SOUL_CATALOG
from backend.services.agent_factory import _ORCHESTRATOR_PROMPT

CODER_PROMPT = SOUL_CATALOG["coder"].system_prompt


def test_orchestrator_remains_responsible_after_delegation():
    assert "Delegation through `task()` is optional" in _ORCHESTRATOR_PROMPT
    assert (
        "remain responsible for integrating and verifying the result"
        in _ORCHESTRATOR_PROMPT
    )


def test_orchestrator_does_not_finish_with_raw_subagent_output():
    assert "never finish with only raw subagent output" in _ORCHESTRATOR_PROMPT


def test_coder_confirma_path_do_artifact_e_contagem_de_todos():
    assert "create_artifact" in CODER_PROMPT
    assert "file path" in CODER_PROMPT
    assert "write_todos" in CODER_PROMPT
    assert "how many tasks" in CODER_PROMPT


def test_writer_docs_e_planner_tambem_confirmam_path_do_artifact():
    """Gap real (revisão de 2026-08-30): `writer-docs` e `planner` também
    usam `create_artifact` (pra salvar doc/plano), mas só `coder` tinha a
    instrução de confirmar o path no texto final — as duas ficavam sujeitas
    ao mesmo sintoma que a instrução do `coder` existe pra evitar (turno
    fechando sem o usuário conseguir saber que o artifact foi salvo)."""
    for nome in ("writer-docs", "planner"):
        prompt = SOUL_CATALOG[nome].system_prompt
        assert "create_artifact" in prompt
        assert "file path" in prompt, f"{nome} não confirma o path do artifact"
