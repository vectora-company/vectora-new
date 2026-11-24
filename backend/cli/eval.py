"""CLI para o Evaluation Lab (vectora eval run / compare)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from backend.evaluation.cassette import Cassette
from backend.evaluation.installed import run_installed_smoke
from backend.evaluation.reports import (
    BenchmarkRun,
    aggregate_runs,
    compare_runs,
    export_csv,
    export_json,
)
from backend.evaluation.runner import compare_cassettes
from backend.evaluation.scenarios import assess_scenario_readiness, load_scenarios
from backend.evaluation.verification import replay_cassette, write_replay_report


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
        report = asyncio.run(
            replay_cassette(cassette, command=["vectora", "eval", "run", str(p)])
        )
        report_dir = getattr(args, "report_dir", None)
        report_paths = write_replay_report(report, report_dir) if report_dir else None
    except Exception as exc:
        print(
            f"Erro durante o replay da cassete '{cassette.cassette_id}': {exc}",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "output", "human") == "json":
        output_data = report.model_dump(mode="json")
        output_data["report_paths"] = (
            {"json": report_paths[0], "markdown": report_paths[1]}
            if report_paths
            else None
        )
        print(json.dumps(output_data, indent=2, ensure_ascii=False))
    else:
        print(f"Replay da cassete '{cassette.cassette_id}': {report.status}")
        print(
            f"Interações reproduzidas: {report.summary['replayed']}; "
            f"falhas: {report.summary['failed']}; "
            f"não suportadas: {report.summary['unsupported']}."
        )
        print(
            "Replay determinístico apenas: não testa app instalado, interface, "
            "provider real nem efeitos externos."
        )
        if report_paths:
            print(f"Relatórios: {report_paths[0]} e {report_paths[1]}")
    return (
        0 if report.status == "replayed" else 2 if report.status == "unsupported" else 1
    )


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
        print(
            json.dumps(
                {
                    "mode": "plan",
                    "status": "not_run",
                    "executable_started": False,
                    "report_written": False,
                    "scenarios": results,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(
            f"Plano: {len(results)} cenários versionados; nada foi executado "
            "e nenhum relatório foi gravado."
        )
        for result in results:
            readiness = result["readiness"]
            print(
                f"{readiness['status']}: {readiness['scenario_id']} — {readiness['reason']}"
            )
    return 0


def _handle_report(args: argparse.Namespace) -> int:
    """Agrega arquivos de execução e exporta JSON ou CSV."""
    try:
        runs = [
            BenchmarkRun.model_validate_json(
                Path(path).expanduser().read_text(encoding="utf-8")
            )
            for path in args.runs
        ]
        report = aggregate_runs(runs, command=args.command or [])
        content = (
            export_csv(report, public=args.public, reviewed=args.reviewed)
            if args.format == "csv"
            else export_json(report, public=args.public, reviewed=args.reviewed)
        )
        Path(args.report).expanduser().write_text(
            content + ("" if args.format == "csv" else "\n"), encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(f"Não foi possível gerar o relatório: {exc}", file=sys.stderr)
        return 1
    return 0


def _handle_compare_runs(args: argparse.Namespace) -> int:
    """Compara grupos de execuções sob condições compatíveis."""
    try:
        baseline = [
            BenchmarkRun.model_validate_json(Path(path).read_text(encoding="utf-8"))
            for path in args.baseline
        ]
        candidate = [
            BenchmarkRun.model_validate_json(Path(path).read_text(encoding="utf-8"))
            for path in args.candidate
        ]
        comparison = compare_runs(baseline, candidate)
        Path(args.report).expanduser().write_text(
            export_json(comparison) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(f"Não foi possível comparar as execuções: {exc}", file=sys.stderr)
        return 1
    print("compatível" if comparison.compatible else "incompatível")
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

    if action == "report":
        return _handle_report(args)

    if action == "compare-runs":
        return _handle_compare_runs(args)

    print(
        "Subcomando eval desconhecido. Use 'vectora eval run' ou 'vectora eval compare'.",
        file=sys.stderr,
    )
    return 1
