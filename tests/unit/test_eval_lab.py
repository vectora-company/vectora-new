"""Testes unitários para o Evaluation Lab, ReplayChatClient e comparação de cassetes."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from backend.cli.eval import handle_eval
from backend.evaluation.cassette import Cassette, CassetteInteraction
from backend.evaluation.replay_client import ReplayChatClient
from backend.evaluation.runner import _total_tokens, compare_cassettes
from backend.vtypes import JsonObject
from backend.vtypes.message import MessageRole, text_message


def test_export_run_cli_preserves_provenance_and_rejects_invalid_recording(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from backend.persistence.native.agent_run import AgentRun

    interaction = CassetteInteraction(
        interaction_id="one",
        run_id="run-export",
        messages=[],
        response_text="api_key=private-export-key",
    )
    run = AgentRun(
        run_id="run-export",
        thread_id="thread-export",
        evidence=[
            {
                "type": "evaluation_interaction",
                "interaction": interaction.model_dump(mode="json"),
            }
        ],
    )
    source = tmp_path / "run.json"
    target = tmp_path / "cassette.json"
    source.write_text(run.model_dump_json(), encoding="utf-8")
    args = argparse.Namespace(
        action="export-run", run_file=str(source), target=str(target)
    )
    assert handle_eval(args) == 0
    exported = Cassette.load(target)
    assert exported.source_run_id == run.run_id
    assert "private-export-key" not in target.read_text(encoding="utf-8")
    assert exported.interactions[0].reproducible is False
    previous = target.read_bytes()
    run.evidence[0]["interaction"] = {
        **interaction.model_dump(mode="json"),
        "run_id": "other-run",
    }
    source.write_text(run.model_dump_json(), encoding="utf-8")
    assert handle_eval(args) == 1
    assert target.read_bytes() == previous
    assert "another run" in capsys.readouterr().err
    run.evidence[0].pop("interaction")
    source.write_text(run.model_dump_json(), encoding="utf-8")
    assert handle_eval(args) == 1
    assert "missing or invalid" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_fixture_replay_preserves_tool_history_without_external_calls() -> None:
    from backend.evaluation.verification import replay_cassette

    cassette = Cassette(
        cassette_id="tool-history",
        interactions=[
            CassetteInteraction(
                interaction_id="after-tool",
                messages=[
                    {
                        "role": "assistant",
                        "content": [],
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "name": "file_read",
                                "args": {"path": "a.txt"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "content": [{"kind": "text", "text": "recorded content"}],
                        "tool_call_id": "call-1",
                        "name": "file_read",
                    },
                ],
                response_text="recorded final response",
            )
        ],
    )
    report = await replay_cassette(cassette, command=["vectora", "eval", "run"])
    assert report.status == "replayed"
    assert report.cases[0].recorded_tool_call_count == 0
    assert report.evidence_level == "fixture_replay"
    cassette.interactions[0].messages[0]["role"] = "invalid-role"
    failed = await replay_cassette(cassette, command=["vectora", "eval", "run"])
    assert failed.status == "failed"
    assert (
        failed.cases[0].reason
        == "mensagem da cassete não corresponde ao schema do runtime"
    )


@pytest.mark.parametrize("missing_side", ["baseline", "candidate"])
def test_comparison_rejects_missing_decision_evidence_on_one_side(
    missing_side: str,
) -> None:
    recorded = Cassette(
        cassette_id="recorded",
        interactions=[
            CassetteInteraction(
                interaction_id="decision",
                messages=[],
                response_text="ok",
                systemone_decision_evidence={
                    "decision": "allow",
                    "policy_digest": "policy",
                },
            )
        ],
    )
    missing = recorded.model_copy(deep=True)
    missing.interactions[0].systemone_decision_evidence = None
    baseline, candidate = (
        (missing, recorded) if missing_side == "baseline" else (recorded, missing)
    )
    report = compare_cassettes(baseline, candidate)
    assert report.accuracy == 0
    assert report.details[0]["decision_evidence_match"] is False


def test_cassette_save_and_load_with_redaction(tmp_path: Path) -> None:
    """Verifica o comportamento de cassette save and load with redaction."""
    target = tmp_path / "test_cassette.json"
    cassette = Cassette(
        cassette_id="cas-001",
        model="systemone:decision",
        interactions=[
            CassetteInteraction(
                interaction_id="int-1",
                messages=[
                    {
                        "role": "user",
                        "content": "minha chave e sk-1234567890abcdef12345678",
                    }
                ],
                response_text="Código privado da empresa: def segredo_interno(): return 42",
                systemone_decision="allow api_key=private-decision-key",
                duration_ms=120,
                usage={"input": 50, "output": 10},
            )
        ],
    )
    cassette.save(target)

    # Verifica que o arquivo persistido redigiu o segredo
    raw_content = target.read_text(encoding="utf-8")
    assert "sk-1234567890abcdef12345678" not in raw_content
    assert "private-decision-key" not in raw_content
    assert "[REDACTED]" in raw_content
    # A ocultação de credenciais não anonimiza código nem dados pessoais.
    assert "segredo_interno" in raw_content
    assert "minha chave e [REDACTED]" in raw_content

    loaded = Cassette.load(target)
    assert loaded.cassette_id == "cas-001"
    assert len(loaded.interactions) == 1
    assert loaded.interactions[0].duration_ms == 120
    assert loaded.interactions[0].usage == {"input": 50, "output": 10}


@pytest.mark.asyncio
async def test_replay_chat_client() -> None:
    """Verifica o comportamento de replay chat client."""
    cassette = Cassette(
        cassette_id="cas-replay",
        interactions=[
            CassetteInteraction(
                interaction_id="int-1",
                messages=[{"role": "user", "content": "listar arquivos"}],
                response_text="Vou listar os arquivos",
                tool_calls=[
                    {"id": "call_1", "name": "file_read", "args": {"path": "."}}
                ],
                duration_ms=15,
                usage={"input": 20, "output": 5},
            )
        ],
    )

    client = ReplayChatClient(cassette)

    # Teste de agenerate
    user_msg = text_message(MessageRole.USER, "listar arquivos")
    res = await client.agenerate([user_msg])
    assert res.role == MessageRole.ASSISTANT
    assert res.text() == "Vou listar os arquivos"
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "file_read"

    # Reset do cursor e teste de astream
    client.reset()
    chunks = [chunk async for chunk in client.astream([user_msg])]
    assert len(chunks) >= 1
    assert any(c.delta_text == "Vou listar os arquivos" for c in chunks)


@pytest.mark.asyncio
async def test_replay_chat_client_rejects_different_history() -> None:
    """Verifica o comportamento de replay chat client rejects different history."""
    cassette = Cassette(
        cassette_id="cas-history",
        interactions=[
            CassetteInteraction(
                interaction_id="int-1",
                messages=[{"role": "user", "content": "comando autorizado"}],
                response_text="ok",
            )
        ],
    )
    client = ReplayChatClient(cassette)
    with pytest.raises(RuntimeError, match="histórico não corresponde"):
        await client.agenerate([text_message(MessageRole.USER, "comando diferente")])


@pytest.mark.asyncio
async def test_replay_chat_client_accepts_native_usage_keys() -> None:
    """Verifica o comportamento de replay chat client accepts native usage keys."""
    cassette = Cassette(
        cassette_id="cas-usage",
        interactions=[
            CassetteInteraction(
                interaction_id="int-1",
                messages=[],
                response_text="ok",
                usage={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            )
        ],
    )
    client = ReplayChatClient(cassette)
    chunks = [chunk async for chunk in client.astream([])]
    usage = next(chunk.usage for chunk in chunks if chunk.usage is not None)
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 3
    assert usage["total_tokens"] == 15


def test_compare_cassettes() -> None:
    """Verifica o comportamento de compare cassettes."""
    b_cassette = Cassette(
        cassette_id="baseline",
        interactions=[
            CassetteInteraction(
                interaction_id="1",
                messages=[],
                response_text="A",
                tool_calls=[{"id": "c1", "name": "tool_a", "args": {}}],
                duration_ms=500,
                usage={"total": 200},
                systemone_decision_evidence={
                    "decision": "allow",
                    "provider": "clef-api",
                    "surface": "hitl",
                    "policy_digest": "sha256:policy",
                },
            )
        ],
    )
    c_cassette = Cassette(
        cassette_id="candidate",
        interactions=[
            CassetteInteraction(
                interaction_id="1",
                messages=[],
                response_text="A",
                tool_calls=[{"id": "c1", "name": "tool_a", "args": {}}],
                duration_ms=50,
                usage={"total": 40},
                systemone_decision="fast_path",
                systemone_decision_evidence={
                    "decision": "allow",
                    "provider": "clef-api",
                    "surface": "hitl",
                    "policy_digest": "sha256:policy",
                },
            )
        ],
    )

    report = compare_cassettes(b_cassette, c_cassette)
    assert report.total_cases == 1
    assert report.matches == 1
    assert report.accuracy == 1.0
    assert report.avg_latency_reduction_pct == 90.0
    assert report.token_savings_pct == 80.0
    assert report.details[0]["candidate_decision_evidence"] == {
        "decision": "allow",
        "provider": "clef-api",
        "surface": "hitl",
        "policy_digest": "sha256:policy",
    }


def test_compare_cassettes_unequal_lengths() -> None:
    """Verifica o comportamento de compare cassettes unequal lengths."""
    b_cassette = Cassette(
        cassette_id="base",
        interactions=[
            CassetteInteraction(
                interaction_id="1",
                messages=[],
                response_text="A",
                tool_calls=[{"id": "c1", "name": "t1", "args": {}}],
                duration_ms=100,
                usage={"total": 10},
            ),
            CassetteInteraction(
                interaction_id="2",
                messages=[],
                response_text="B",
                tool_calls=[{"id": "c2", "name": "t2", "args": {}}],
                duration_ms=100,
                usage={"total": 10},
            ),
        ],
    )
    c_cassette = Cassette(
        cassette_id="cand",
        interactions=[
            CassetteInteraction(
                interaction_id="1",
                messages=[],
                response_text="A",
                tool_calls=[{"id": "c1", "name": "t1", "args": {}}],
                duration_ms=50,
                usage={"total": 5},
            )
        ],
    )

    report = compare_cassettes(b_cassette, c_cassette)
    assert report.total_cases == 2
    assert report.matches == 1
    assert report.accuracy == 0.5
    assert len(report.details) == 2
    assert report.details[1].get("missing") == "candidate"


def test_compare_cassettes_rejects_different_tool_arguments() -> None:
    """Nomes iguais com argumentos diferentes não são a mesma execução."""
    baseline = Cassette(
        cassette_id="base",
        interactions=[
            CassetteInteraction(
                interaction_id="1",
                messages=[],
                tool_calls=[{"id": "a", "name": "read_file", "args": {"path": "a"}}],
            )
        ],
    )
    candidate = Cassette(
        cassette_id="candidate",
        interactions=[
            CassetteInteraction(
                interaction_id="1",
                messages=[],
                tool_calls=[
                    {"id": "different", "name": "read_file", "args": {"path": "b"}}
                ],
            )
        ],
    )

    report = compare_cassettes(baseline, candidate)

    assert report.matches == 0
    assert report.details[0]["tool_presence_match"] is True
    assert report.details[0]["tool_arguments_match"] is False


def test_compare_cassettes_rejects_divergent_response_without_tools() -> None:
    """A resposta divergente não pode produzir 100% de equivalência."""
    baseline = Cassette(
        cassette_id="base",
        interactions=[
            CassetteInteraction(interaction_id="1", messages=[], response_text="A")
        ],
    )
    candidate = Cassette(
        cassette_id="candidate",
        interactions=[
            CassetteInteraction(interaction_id="1", messages=[], response_text="B")
        ],
    )

    report = compare_cassettes(baseline, candidate)

    assert report.matches == 0
    assert report.accuracy == 0.0
    assert report.details[0]["response_match"] is False


def test_compare_cassettes_detects_decision_evidence_divergence() -> None:
    """A changed policy or decision must be visible in evaluation accuracy."""
    baseline = Cassette(
        cassette_id="base",
        interactions=[
            CassetteInteraction(
                interaction_id="same",
                messages=[],
                response_text="approved",
                systemone_decision_evidence={
                    "run_id": "run-old",
                    "decision_id": "decision-old",
                    "decision": "allow",
                    "policy_digest": "sha256:policy-a",
                    "surface": "hitl",
                },
            )
        ],
    )
    candidate = Cassette(
        cassette_id="candidate",
        interactions=[
            CassetteInteraction(
                interaction_id="same",
                messages=[],
                response_text="approved",
                systemone_decision_evidence={
                    "run_id": "run-new",
                    "decision_id": "decision-new",
                    "decision": "allow",
                    "policy_digest": "sha256:policy-b",
                    "surface": "hitl",
                },
            )
        ],
    )

    report = compare_cassettes(baseline, candidate)

    assert report.matches == 0
    assert report.accuracy == 0.0
    assert report.details[0]["decision_evidence_match"] is False
    baseline_evidence = report.details[0]["baseline_decision_evidence"]
    assert isinstance(baseline_evidence, dict)
    assert baseline_evidence.get("policy_digest") == "sha256:policy-a"


def test_compare_cassettes_ignores_decision_execution_identity() -> None:
    """Different run IDs do not make otherwise identical decisions diverge."""
    baseline = Cassette(
        cassette_id="base",
        interactions=[
            CassetteInteraction(
                interaction_id="same",
                messages=[],
                systemone_decision_evidence={
                    "run_id": "run-old",
                    "decision_id": "decision-old",
                    "decision": "requires_human",
                    "policy_digest": "sha256:policy",
                },
            )
        ],
    )
    candidate = Cassette(
        cassette_id="candidate",
        interactions=[
            CassetteInteraction(
                interaction_id="same",
                messages=[],
                systemone_decision_evidence={
                    "run_id": "run-new",
                    "decision_id": "decision-new",
                    "decision": "requires_human",
                    "policy_digest": "sha256:policy",
                },
            )
        ],
    )

    report = compare_cassettes(baseline, candidate)

    assert report.matches == 1
    assert report.details[0]["decision_evidence_match"] is True


def test_compare_cassettes_rejects_duplicate_interaction_ids() -> None:
    """Duplicate IDs must not be silently aligned as separate cases."""
    cassette = Cassette(
        cassette_id="dup",
        interactions=[
            CassetteInteraction(interaction_id="same", messages=[]),
            CassetteInteraction(interaction_id="same", messages=[]),
        ],
    )
    with pytest.raises(ValueError, match="duplicado"):
        compare_cassettes(cassette, Cassette(cassette_id="other"))


def test_cassette_preserves_structured_decision_evidence_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    """Decision provenance survives round-trip without persisting credentials."""
    path = tmp_path / "decision.json"
    evidence: JsonObject = {
        "decision": "requires_human",
        "provider": "clef-api",
        "model": "decision-v1",
        "confidence": 0.42,
        "policy_digest": "sha256:policy",
        "surface": "hitl",
        "reason": "low_confidence",
        "response_redacted": {"authorization": "Bearer secret-token"},
    }
    Cassette(
        cassette_id="decision-run",
        source_run_id="run-1",
        interactions=[
            CassetteInteraction(
                interaction_id="tool-1",
                messages=[],
                run_id="run-1",
                systemone_decision="requires_human",
                systemone_decision_evidence=evidence,
            )
        ],
    ).save(path)

    raw = path.read_text(encoding="utf-8")
    assert "secret-token" not in raw
    loaded = Cassette.load(path)
    interaction = loaded.interactions[0]
    assert interaction.run_id == loaded.source_run_id == "run-1"
    assert interaction.systemone_decision == "requires_human"
    assert interaction.systemone_decision_evidence is not None
    assert interaction.systemone_decision_evidence["decision"] == "requires_human"
    assert interaction.systemone_decision_evidence["policy_digest"] == "sha256:policy"
    assert "[REDACTED]" in str(
        interaction.systemone_decision_evidence["response_redacted"]
    )


def test_cassette_load_keeps_legacy_decision_string_compatible(
    tmp_path: Path,
) -> None:
    """Legacy cassettes without structured provenance remain loadable."""
    path = tmp_path / "legacy.json"
    path.write_text(
        '{"cassette_id":"legacy","interactions":[{"interaction_id":"i1",'
        '"messages":[],"systemone_decision":"allow"}]}',
        encoding="utf-8",
    )
    interaction = Cassette.load(path).interactions[0]
    assert interaction.systemone_decision == "allow"
    assert interaction.systemone_decision_evidence is None


def test_total_tokens_helper() -> None:
    """Verifica o comportamento de total tokens helper."""
    assert _total_tokens({}) == 0
    assert _total_tokens({"total": 120}) == 120
    assert _total_tokens({"total_tokens": 150}) == 150
    assert _total_tokens({"input": 40, "output": 60}) == 100
    assert _total_tokens({"input": 40, "output": 60, "cost_usd": 0.25}) == 100


def test_cli_eval_compare(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verifica o comportamento de cli eval compare."""
    b_path = tmp_path / "base.json"
    c_path = tmp_path / "cand.json"
    Cassette(cassette_id="base").save(b_path)
    Cassette(cassette_id="cand").save(c_path)

    args = argparse.Namespace(
        action="compare",
        baseline=str(b_path),
        candidate=str(c_path),
        output="human",
    )
    code = handle_eval(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "Relatório de Avaliação" in captured.out


def test_cli_eval_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verifica o comportamento de cli eval run."""
    suite_path = tmp_path / "suite.json"
    cassette = Cassette(
        cassette_id="my-suite",
        interactions=[
            CassetteInteraction(
                interaction_id="int-1",
                messages=[{"role": "user", "content": "olá"}],
                response_text="olá mundo",
                duration_ms=10,
                usage={"total": 5},
            )
        ],
    )
    cassette.save(suite_path)

    # Execução em modo human
    args = argparse.Namespace(
        action="run",
        suite=str(suite_path),
        output="human",
    )
    code = handle_eval(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "Replay da cassete 'my-suite': replayed" in captured.out
    assert "não testa app instalado" in captured.out

    # Execução em modo json
    args_json = argparse.Namespace(
        action="run",
        suite=str(suite_path),
        output="json",
    )
    code_json = handle_eval(args_json)
    assert code_json == 0
    captured_json = capsys.readouterr()
    replay_json = json.loads(captured_json.out)
    assert replay_json["mode"] == "fixture_replay"
    assert replay_json["evidence_level"] == "fixture_replay"
    assert replay_json["status"] == "replayed"
    assert replay_json["app_installed_tested"] is False
    assert replay_json["real_provider_tested"] is False
    assert replay_json["external_effects_executed"] is False
    assert "olá mundo" not in captured_json.out

    report_dir = tmp_path / "fixture-report"
    report_args = argparse.Namespace(
        action="run",
        suite=str(suite_path),
        report_dir=str(report_dir),
        output="human",
    )
    assert handle_eval(report_args) == 0
    assert (report_dir / "report.json").is_file()
    markdown = (report_dir / "report.md").read_text(encoding="utf-8")
    assert "Relatório de replay determinístico" in markdown
    assert "| `int-1` | replayed |" in markdown
    assert "olá mundo" not in markdown

    # O relatório nunca sobrescreve dados existentes.
    assert handle_eval(report_args) == 1
    assert "File exists" in capsys.readouterr().err

    # `--suite` aponta para um único cassette; uma lista de cassettes é recusada.
    list_path = tmp_path / "list.json"
    list_path.write_text(
        f"[{suite_path.read_text(encoding='utf-8')}]", encoding="utf-8"
    )
    code_list = handle_eval(
        argparse.Namespace(action="run", suite=str(list_path), output="human")
    )
    assert code_list == 1
    assert "Erro ao carregar cassete" in capsys.readouterr().err


def test_cli_eval_missing_suite(capsys: pytest.CaptureFixture[str]) -> None:
    """Verifica o comportamento de cli eval missing suite."""
    args = argparse.Namespace(
        action="run",
        suite=None,
        output="human",
    )
    code = handle_eval(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "obrigatório" in captured.err


def test_empty_cassette_is_unsupported_not_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "empty-cassette.json"
    Cassette(
        cassette_id="empty",
        interactions=[CassetteInteraction(interaction_id="no-input", messages=[])],
    ).save(source)

    result = handle_eval(
        argparse.Namespace(action="run", suite=str(source), output="json")
    )

    captured = capsys.readouterr()
    assert result == 2
    assert json.loads(captured.out)["status"] == "unsupported"


def test_fixture_replay_reports_failed_case_and_continues_later_cases() -> None:
    from backend.evaluation.verification import replay_cassette

    cassette = Cassette(
        cassette_id="partial",
        interactions=[
            CassetteInteraction(
                interaction_id="bad",
                messages=[{"role": "not-a-role", "content": "fixture"}],
                response_text="unused",
            ),
            CassetteInteraction(
                interaction_id="good",
                messages=[{"role": "user", "content": "fixture"}],
                response_text="recorded answer",
            ),
        ],
    )

    report = asyncio.run(replay_cassette(cassette, command=["vectora", "eval", "run"]))

    assert report.status == "failed"
    assert [case.status for case in report.cases] == ["failed", "replayed"]
    assert report.app_installed_tested is False
    assert report.real_provider_tested is False


@pytest.mark.parametrize(
    ("approval_events", "expected_status", "expected_reason"),
    [
        (
            [],
            "unsupported",
            "decisão exige aprovação humana, mas não há pedido registrado",
        ),
        (
            [
                {
                    "interrupt_id": "interrupt-1",
                    "tool_call_id": "call-1",
                    "tool_name": "file_write",
                }
            ],
            "unsupported",
            "pedido de aprovação humana não tem decisão final registrada",
        ),
        (
            [
                {
                    "interrupt_id": "interrupt-1",
                    "tool_call_id": "call-1",
                    "tool_name": "file_write",
                },
                {
                    "source": "human",
                    "decision": "approve",
                    "interrupt_id": "interrupt-1",
                    "tool_call_id": "call-1",
                    "tool_name": "file_write",
                },
            ],
            "replayed",
            None,
        ),
    ],
)
def test_fixture_replay_requires_a_complete_human_approval_audit(
    approval_events: list[dict[str, str]],
    expected_status: str,
    expected_reason: str | None,
) -> None:
    from backend.evaluation.verification import replay_cassette

    cassette = Cassette(
        cassette_id="human-approval",
        interactions=[
            CassetteInteraction(
                interaction_id="protected-tool",
                messages=[{"role": "user", "content": "write the fixture"}],
                response_text="recorded tool request",
                tool_calls=[{"id": "call-1", "name": "file_write", "args": {}}],
                systemone_decision_evidence={
                    "decisions": [
                        {
                            "tool_call_id": "call-1",
                            "decision": "requires_human",
                        }
                    ],
                    "approval_events": approval_events,
                },
            )
        ],
    )

    report = asyncio.run(replay_cassette(cassette, command=["vectora", "eval", "run"]))

    assert report.cases[0].status == expected_status
    assert report.cases[0].reason == expected_reason


def test_cassette_export_keeps_approval_evidence_for_related_tool_calls() -> None:
    from backend.evaluation.recording import cassette_from_run
    from backend.persistence.native.agent_run import AgentRun

    approval = {
        "source": "human",
        "decision": "approve",
        "interrupt_id": "interrupt-1",
        "tool_call_id": "call-1",
        "tool_name": "file_write",
    }
    run = AgentRun(
        run_id="run-approval",
        thread_id="thread-approval",
        approvals=[approval, {"tool_call_id": "unrelated-call"}],
        evidence=[
            {
                "type": "evaluation_interaction",
                "model": "fixture-model",
                "interaction": {
                    "interaction_id": "protected-tool",
                    "run_id": "run-approval",
                    "messages": [{"role": "user", "content": "write the fixture"}],
                    "tool_calls": [{"id": "call-1", "name": "file_write", "args": {}}],
                },
            },
            {
                "type": "decision_model_decision",
                "tool_call_id": "call-1",
                "decision": "requires_human",
            },
        ],
    )

    cassette = cassette_from_run(run)

    assert cassette.interactions[0].systemone_decision_evidence is not None
    assert cassette.interactions[0].systemone_decision_evidence["approval_events"] == [
        approval
    ]


def test_fixture_replay_does_not_create_temporary_execution_directories(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cassette.json"
    Cassette(
        cassette_id="no-temp",
        interactions=[
            CassetteInteraction(
                interaction_id="one",
                messages=[{"role": "user", "content": "fixture"}],
                response_text="safe output",
            )
        ],
    ).save(source)
    before = set(Path(tempfile.gettempdir()).glob("vectora-eval-*"))
    report_dir = tmp_path / "report"

    assert (
        handle_eval(
            argparse.Namespace(
                action="run",
                suite=str(source),
                report_dir=str(report_dir),
                output="human",
            )
        )
        == 0
    )

    assert set(Path(tempfile.gettempdir()).glob("vectora-eval-*")) == before


def test_fixture_report_directory_is_removed_when_markdown_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.evaluation.verification import (
        FixtureReplayReport,
        ReplayCaseResult,
        write_replay_report,
    )

    report = FixtureReplayReport(
        status="replayed",
        report_id="report-1",
        cassette_id="fixture-1",
        command=["vectora", "eval", "run"],
        summary={"replayed": 1, "failed": 0, "unsupported": 0},
        cases=[
            ReplayCaseResult(
                interaction_id="interaction-1",
                status="replayed",
                replay_duration_ms=0,
                recorded_duration_ms=0,
                recorded_tool_call_count=0,
            )
        ],
    )
    report_dir = tmp_path / "partial-report"
    original_write_text = Path.write_text

    def fail_markdown_write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path.name == "report.md":
            raise OSError("simulated markdown write failure")
        return original_write_text(
            path, data, encoding=encoding, errors=errors, newline=newline
        )

    monkeypatch.setattr(Path, "write_text", fail_markdown_write)

    with pytest.raises(OSError, match="simulated markdown write failure"):
        write_replay_report(report, str(report_dir))

    assert not report_dir.exists()


def test_cli_eval_corrupted_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verifica o comportamento de cli eval corrupted file."""
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{bad json", encoding="utf-8")

    args = argparse.Namespace(
        action="run",
        suite=str(corrupt),
        output="human",
    )
    code = handle_eval(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "Erro ao carregar cassete" in captured.err


@pytest.mark.parametrize(
    "raw", ["[]", "null", "42", '{"cassette_id":"x","redaction_version":2}']
)
def test_cassette_load_rejects_invalid_shape_and_redaction_version(
    tmp_path: Path, raw: str
) -> None:
    """Loading untrusted JSON must fail with a validation error, never AttributeError."""
    source = tmp_path / "invalid.json"
    source.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        Cassette.load(source)


@pytest.mark.parametrize("action", ["run", "compare"])
@pytest.mark.parametrize("raw", ["[]", '{"cassette_id":"x","redaction_version":2}'])
def test_cli_reports_invalid_cassette_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    action: str,
    raw: str,
) -> None:
    """Both CLI entry points explain invalid cassettes and return failure."""
    source = tmp_path / "invalid.json"
    source.write_text(raw, encoding="utf-8")
    args = argparse.Namespace(
        action=action,
        suite=str(source),
        baseline=str(source),
        candidate=str(source),
    )
    assert handle_eval(args) == 1
    output = capsys.readouterr()
    assert "Erro ao carregar" in output.err
    assert "Traceback" not in output.err
    assert (
        "objeto JSON" if raw == "[]" else "redaction_version não suportada"
    ) in output.err


def test_cli_compare_reports_incomparable_cassettes_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Loadable cassettes that cannot be compared fail with a clear message."""
    interaction = CassetteInteraction(
        interaction_id="1", messages=[], response_text="A", tool_calls=[]
    )
    valid = tmp_path / "valid.json"
    duplicated = tmp_path / "duplicated.json"
    valid.write_text(
        Cassette(cassette_id="valid", interactions=[interaction]).model_dump_json(),
        encoding="utf-8",
    )
    duplicated.write_text(
        Cassette(
            cassette_id="duplicated", interactions=[interaction, interaction]
        ).model_dump_json(),
        encoding="utf-8",
    )

    def _compare(candidate: Path) -> int:
        return handle_eval(
            argparse.Namespace(
                action="compare", baseline=str(valid), candidate=str(candidate)
            )
        )

    assert _compare(valid) == 0
    capsys.readouterr()

    assert _compare(duplicated) == 1
    output = capsys.readouterr()
    assert "Erro ao comparar cassetes" in output.err
    assert "interaction_id duplicado" in output.err
    assert "Traceback" not in output.err
    assert output.out == ""


@pytest.mark.asyncio
async def test_recorded_run_export_survives_store_restart_and_session_purge(
    tmp_path: Path,
) -> None:
    from backend.evaluation.recording import cassette_from_run, record_interaction
    from backend.persistence.native.agent_run import AgentRun
    from backend.persistence.native.session_store import SessionStore
    from backend.storage.sqlite.pool import AsyncConnectionPool
    from backend.vtypes.message import MessageRole, text_message

    path = str(tmp_path / "recording.db")
    pool = AsyncConnectionPool(path, min_size=1, max_size=2)
    await pool.open()
    store = SessionStore(pool)
    await store.create_session("recorded-thread", user_id="owner")
    await store.upsert_agent_run(
        AgentRun(run_id="recorded-run", thread_id="recorded-thread", user_id="owner")
    )
    await record_interaction(
        store,
        run_id="recorded-run",
        messages=[text_message(MessageRole.USER, "hello")],
        response="recorded",
        tool_calls=[],
        usage=None,
        model="fixture",
        duration_ms=1,
        policy_digest="snapshot",
    )
    await pool.close()
    reopened = AsyncConnectionPool(path, min_size=1, max_size=2)
    await reopened.open()
    try:
        durable = SessionStore(reopened)
        run = await durable.get_agent_run("recorded-run")
        assert run is not None
        cassette = cassette_from_run(run)
        assert cassette.interactions[0].response_text == "recorded"
        from backend.evaluation.verification import replay_cassette

        replay = await replay_cassette(cassette, command=["vectora", "eval", "run"])
        assert replay.status == "replayed"
        await durable.delete_session("recorded-thread")
        assert await durable.get_agent_run("recorded-run") is None
    finally:
        await reopened.close()
