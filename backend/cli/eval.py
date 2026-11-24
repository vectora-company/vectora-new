"""CLI para o Evaluation Lab (vectora eval run / compare)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from backend.evaluation.cassette import Cassette
from backend.evaluation.installed import run_installed_smoke
from backend.evaluation.replay_client import ReplayChatClient
from backend.evaluation.runner import compare_cassettes
from backend.evaluation.scenarios import assess_scenario_readiness, load_scenarios
from backend.vtypes import JsonObject
from backend.vtypes.message import VMessage


async def _replay_suite(cassette: Cassette) -> list[JsonObject]:
    """Executa a operação replay suite."""
    client = ReplayChatClient(cassette)
    results: list[JsonObject] = []
    for idx, interaction in enumerate(cassette.interactions):
        msgs: list[VMessage] = []
        for m in interaction.messages:
            payload = dict(m)
            payload.setdefault("role", "user")
            content = payload.get("content", "")
            if isinstance(content, str):
                payload["content"] = [{"kind": "text", "text": content}]
            msgs.append(TypeAdapter(VMessage).validate_python(payload))
        res = await client.agenerate(msgs)
        results.append(
            {
                "index": idx,
                "interaction_id": interaction.interaction_id,
                "role": res.role.value,
                "response_text": res.text(),
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "args": tc.args}
                    for tc in res.tool_calls
                ],
            }
        )
    return results


def _handle_compare(args: argparse.Namespace) -> int:
    """Executa a operação handle compare."""
    baseline_path = getattr(args, "baseline", None)
    candidate_path = getattr(args, "candidate", None)
    if not baseline_path or not candidate_path:
        print(
            "Erro: caminhos de baseline e candidate são obrigatórios.",
            file=sys.stderr,
        )
        return 1

    try:
        b_cassette = Cassette.load(baseline_path)
        c_cassette = Cassette.load(candidate_path)
    except (OSError, ValueError) as exc:
        print(
            f"Erro ao carregar cassetes para comparação: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        report = compare_cassettes(b_cassette, c_cassette)
    except ValueError as exc:
        print(f"Erro ao comparar cassetes: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "output", "human") == "json":
        print(json.dumps(report.model_dump(), indent=2, ensure_ascii=False))
    else:
        print(f"=== Relatório de Avaliação: {report.suite_name} ===")
        print(f"Casos comparados: {report.total_cases}")
        print(f"Acurácia / Concordância: {report.accuracy * 100:.1f}%")
        print(f"Redução de latência: {report.avg_latency_reduction_pct:+.1f}%")
        print(f"Economia de tokens: {report.token_savings_pct:+.1f}%")
    return 0


def _handle_run(args: argparse.Namespace) -> int:
    """Executa a operação handle run."""
    suite_path = getattr(args, "suite", None)
    if not suite_path:
        print(
            "Erro: o parâmetro --suite é obrigatório para execução de avaliação.",
            file=sys.stderr,
        )
        return 1
    p = Path(suite_path)
    if not p.exists():
        print(f"Erro: suite '{suite_path}' não encontrada.", file=sys.stderr)
        return 1

    try:
        cassette = Cassette.load(p)
    except (OSError, ValueError) as exc:
        print(
            f"Erro ao carregar cassete '{suite_path}': {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        replayed = asyncio.run(_replay_suite(cassette))
    except Exception as exc:
        print(
            f"Erro durante o replay da suite '{cassette.cassette_id}': {exc}",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "output", "human") == "json":
        output_data = {
            "suite_id": cassette.cassette_id,
            "total_interactions": len(cassette.interactions),
            "replayed_interactions": len(replayed),
            "results": replayed,
        }
        print(json.dumps(output_data, indent=2, ensure_ascii=False))
    else:
        print(
            f"Suite '{cassette.cassette_id}' executada com sucesso ({len(replayed)} interações reproduzidas)."
        )
    return 0


def _handle_smoke(args: argparse.Namespace) -> int:
    """Abre a instalação real com perfil descartável e reporta o diagnóstico."""
    report = run_installed_smoke(
        args.app,
        timeout_seconds=args.timeout_seconds,
    )
    report_json = report.model_dump_json(indent=2)
    report_path = getattr(args, "report", None)
    if report_path:
        try:
            Path(report_path).expanduser().write_text(
                report_json + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"Não foi possível salvar o relatório: {exc}", file=sys.stderr)
            return 1

    if getattr(args, "output", "human") == "json":
        print(report_json)
    else:
        print(f"Evaluation Lab: {report.status} (run_id={report.run_id})")
        if report.reason:
            print(report.reason, file=sys.stderr)
    return 0 if report.status == "passed" else 2 if report.status == "blocked" else 1


def _handle_scenarios(args: argparse.Namespace) -> int:
    """Lista cenários versionados e seu estado de prontidão sem executá-los."""
    try:
        scenarios = load_scenarios()
    except (OSError, ValueError) as exc:
        print(f"Erro ao carregar catálogo de cenários: {exc}", file=sys.stderr)
        return 1

    capabilities = set(getattr(args, "available_capability", []) or [])
    results = [
        {
            "scenario": scenario.model_dump(mode="json"),
            "readiness": assess_scenario_readiness(
                scenario,
                available_capabilities=capabilities,
                provider_configured=getattr(args, "provider_configured", False),
                provider_is_remote=getattr(args, "provider_remote", False),
                remote_provider_opt_in=getattr(args, "allow_remote_provider", False),
            ).model_dump(mode="json"),
        }
        for scenario in scenarios
    ]
    if getattr(args, "output", "human") == "json":
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(
            f"Cenários versionados: {len(results)} (catálogo; nenhuma execução iniciada)"
        )
        for result in results:
            readiness = result["readiness"]
            print(
                f"{readiness['status']}: {readiness['scenario_id']} — {readiness['reason']}"
            )
    return 0


def handle_eval(args: argparse.Namespace) -> int:  # noqa: PLR0911
    """Roteia as ações de eval."""
    action = getattr(args, "action", None)

    if action == "export-run":
        from backend.evaluation.recording import cassette_from_run
        from backend.persistence.native.agent_run import AgentRun

        try:
            run = AgentRun.model_validate_json(
                Path(args.run_file).read_text(encoding="utf-8")
            )
            cassette_from_run(run).save(Path(args.target))
        except (OSError, ValueError) as exc:
            print(f"Erro ao exportar execução: {exc}", file=sys.stderr)
            return 1
        return 0

    if action == "compare":
        return _handle_compare(args)

    if action == "run":
        return _handle_run(args)

    if action == "smoke":
        return _handle_smoke(args)

    if action == "scenarios":
        return _handle_scenarios(args)

    print(
        "Subcomando eval desconhecido. Use 'vectora eval run' ou 'vectora eval compare'.",
        file=sys.stderr,
    )
    return 1
