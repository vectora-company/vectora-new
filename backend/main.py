"""Vectora CLI — ponto de entrada unificado.

vectora                 imprime o help (descobre a CLI de configuração)
vectora start           sobe backend + SPA (fullstack)
vectora start --headless  sobe sem janela (bandeja + backend)
vectora web             sobe o webapp com Electron/Chromium, sem bandeja Python
Configuração (operacional, para VPS via SSH):
  vectora config                 mostra ~/.vectora/settings.json
  vectora config --set K=V       edita uma chave de settings
  vectora config keys            wizard de API keys + LLM provider
  vectora config docker [up|down|status]
  vectora config qdrant <url> [--api-key KEY]
  vectora config redis <url>
  vectora storage <ação>         migrations, diagnóstico, backup/restore
  vectora auth <ação>            signup | login | logout | whoami | refresh
  vectora sessions               lista as sessões salvas
  vectora mcp list               lista MCPs instalados
  vectora skills list            lista skills instaladas
"""

from __future__ import annotations

import os

# GitPython chama Git.refresh() na importação (`import git`) e, por padrão,
# levanta ImportError se não achar o executável git no PATH — sem isso, o
# processo inteiro crasha no boot em qualquer máquina sem git instalado.
# Precisa rodar antes do primeiro `import git` transitivo (tools/git.py,
# persistence/checkpoint.py); tools de git degradam de forma limpa quando
# chamadas sem o binário disponível.
os.environ.setdefault("GIT_PYTHON_REFRESH", "quiet")

import argparse
import asyncio
import contextlib
import logging
import signal
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _find_free_port(preferred: int | None = None) -> int:
    """Devolve uma porta TCP livre em 127.0.0.1.

    Se ``preferred`` for fornecida e estiver disponível, é retornada como está;
    caso contrário (ou se já estiver ocupada), o SO escolhe uma porta efêmera.
    """
    if preferred is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# Configure UTF-8 on Windows before any output
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# Project root no sys.path (necessário ao rodar como script direto). Usa
# parent.parent (raiz), não parent (pacote src/), para não sombrear pacotes de
# terceiros com o mesmo nome (ex.: src/mcp/ sombrearia o pacote `mcp`).
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.runtime_profile import (
    normalize_runtime_home,
    runtime_home_for_profile,
    sanitize_runtime_profile,
)
from backend.services.log_setup import setup_logging

setup_logging()
logger = logging.getLogger(__name__)
_SHUTDOWN_EVENTS: dict[int, asyncio.Event] = {}


# ---------------------------------------------------------------------------
# Terminal shutdown helpers
# ---------------------------------------------------------------------------


def _should_install_terminal_signals(
    env: dict[str, str], *, force_web: bool = False
) -> bool:
    """Decide se o handler de sinal customizado (Ctrl+C/SIGTERM/SIGHUP →
    shutdown gracioso do uvicorn, que aciona o `finally` do lifespan e
    limpa sidecars como o NATS) deve ser instalado.

    Todo backend desktop instala os handlers: o Electron envia SIGTERM ao
    sair, e um Ctrl+C no terminal também chega ao processo filho. Assim,
    ambos os caminhos acionam o shutdown gracioso do Uvicorn e do lifespan.
    `VECTORA_SPAWN_ELECTRON=1` identifica o modo de desenvolvimento em que o
    próprio backend sobe o Electron, inclusive quando stdin não é um TTY.
    """
    # ``vectora web`` is the process owner even when launched by a service
    # manager, CI runner, or detached VPS shell where stdin is not a TTY.
    # Gating this mode on ``isatty`` leaves SIGINT/SIGTERM at the mercy of the
    # parent process and can keep the Python host alive after uvicorn shuts
    # down its application resources.
    if force_web:
        return True

    desktop = bool(env.get("VECTORA_DESKTOP"))
    owns_itself = bool(env.get("VECTORA_SPAWN_ELECTRON"))
    if desktop:
        # Electron sends SIGTERM when quitting, and terminals deliver Ctrl+C
        # to both Electron and its backend child. Handle both through the
        # graceful shutdown path so Uvicorn can close its lifespan cleanly.
        return True
    if owns_itself:
        # Backend-primário em dev: sempre instala, mesmo sem tty — é o único
        # dono do processo, e sys.stdin.isatty() pode vir False dependendo
        # de como `uv run`/o shell pai herdou os handles de stdin, gate que
        # antes calava o Ctrl+C sem nenhum sinal disso pro usuário (o único
        # jeito de fechar virava o Sair da bandeja do Electron).
        return True
    return sys.stdin.isatty()


#: Prazo máximo do shutdown gracioso antes de forçar a saída.
_SHUTDOWN_DEADLINE_S = 15.0
#: Prazo para o uvicorn fechar conexões abertas (SSE) ao receber o sinal.
_GRACEFUL_SHUTDOWN_TIMEOUT_S = 5


def _descendant_pids(root_pid: int) -> list[int]:
    """PIDs descendentes de ``root_pid`` lidos de ``/proc`` (Linux)."""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            # O nome do processo fica entre parênteses e pode conter espaços.
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        parents[int(entry.name)] = ppid
    found: list[int] = []
    frontier = [root_pid]
    while frontier:
        current = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == current and pid not in found:
                found.append(pid)
                frontier.append(pid)
    return found


def _force_exit() -> None:
    """Encerra os filhos (Electron, NATS, llama.cpp) e sai do processo.

    Usado quando o shutdown gracioso não termina no prazo ou o usuário repete
    o Ctrl+C. Sem isso, o processo e os sidecars ficam vivos até alguém fechar
    pelo gerenciador de tarefas.
    """
    try:
        if sys.platform == "win32":
            import shutil
            import subprocess  # nosec B404

            taskkill = shutil.which("taskkill")
            if taskkill:
                subprocess.run(  # nosec B603  # noqa: S603
                    [taskkill, "/PID", str(os.getpid()), "/T", "/F"],
                    check=False,
                    timeout=5,
                )
        elif Path("/proc").is_dir():
            import time

            pids = _descendant_pids(os.getpid())
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for pid in pids:
                    with contextlib.suppress(OSError):
                        os.kill(pid, sig)
                if sig == signal.SIGTERM:
                    time.sleep(1.5)
    except Exception:
        logger.debug("Vectora: falha ao encerrar processos filhos", exc_info=True)
    os._exit(130)


def _install_terminal_signals(server: Any, icon_ref: list[Any]) -> None:
    """Instala handlers de SIGINT/SIGTERM/SIGHUP para shutdown limpo — ver
    `_should_install_terminal_signals` para quando isso é chamado."""
    shutdown_started = False

    def _shutdown(_signum: int, _frame: Any) -> None:
        """Executa a operação shutdown."""
        nonlocal shutdown_started
        if shutdown_started:
            # Segundo Ctrl+C: o usuário não quer esperar o shutdown gracioso.
            logger.warning("Vectora: segundo sinal — encerrando à força")
            _force_exit()
            return
        shutdown_started = True
        # Rede de segurança: conexões SSE abertas ou uma thread presa não
        # podem manter o processo vivo para sempre depois do primeiro Ctrl+C.
        deadline = threading.Timer(_SHUTDOWN_DEADLINE_S, _force_exit)
        deadline.daemon = True
        deadline.start()
        from backend.services.log_setup import mark_shutdown_requested

        mark_shutdown_requested()
        logger.info(
            "Vectora: sinal recebido (signal=%s, pid=%s) — iniciando shutdown",
            _signum,
            os.getpid(),
        )
        server.should_exit = True
        shutdown_event = _SHUTDOWN_EVENTS.get(id(server))
        if shutdown_event is not None:
            shutdown_event.set()
        if icon_ref[0] is not None:
            icon_ref[0].stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _shutdown)  # type: ignore[attr-defined]
    if hasattr(signal, "SIGBREAK"):
        # Windows process groups expose Ctrl+Break as the only targeted
        # console event. Treat it like Ctrl+C so supervised processes and
        # process-level shutdown tests exercise the same cleanup path.
        signal.signal(signal.SIGBREAK, _shutdown)  # type: ignore[attr-defined]


def _disable_uvicorn_signal_capture(server: Any) -> None:
    """Keep the process-owner handlers active while Uvicorn serves.

    Versões novas do Uvicorn instalam os handlers em ``capture_signals`` (e
    reexecutam o sinal só no fim do servidor); as antigas, em
    ``install_signal_handlers``. Neutraliza as duas: com apenas a segunda, o
    handler do processo só rodava depois do shutdown e o filtro de logs não
    sabia que o encerramento era esperado.
    """
    server.install_signal_handlers = lambda: None
    server.capture_signals = contextlib.nullcontext


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Executa a operação build parser."""
    from backend.version import __version__

    parser = argparse.ArgumentParser(
        prog="vectora",
        description="Vectora — workspace de IA com RAG nativo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""exemplos:
  vectora start                        abre o app desktop (Electron)
  vectora start --headless             servidor headless (VPS/systemd)
  vectora start --port 9000            porta customizada
  vectora web                          webapp com Electron/Chromium, sem bandeja
  vectora --tests                      testa esta instalação (baixa pytest sob demanda)

  vectora config                       mostra configuração completa
  vectora config keys                  wizard: API keys + LLM provider
  vectora config --set active_model=gemini-2.5-pro
  vectora config --set storage_mode=complete
  vectora config --set postgres_dsn=postgresql+asyncpg://user:pass@host/db
  vectora config --set google_api_key=AIza...
  vectora config docker up             sobe Postgres + Redis + Qdrant local
  vectora config qdrant https://… --api-key KEY
  vectora config redis redis://…

  vectora auth login                   autentica no servidor Vectora
  vectora sessions                     lista as sessões salvas
  vectora storage info                 status dos backends de dados
  vectora storage migrate history      mostra o histórico de migrations
  vectora storage migrate plan         mostra o plano sem alterar o banco
  vectora storage migrate upgrade      aplica migrations SQLite pendentes
""",
    )

    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"vectora {__version__}",
    )
    parser.add_argument(
        "--tests",
        action="store_true",
        help="Baixa pytest sob demanda e valida a instalação real do app",
    )

    sub = parser.add_subparsers(dest="command", metavar="subcommand")

    help_p = sub.add_parser("help", help="Mostra esta ajuda ou a de um subcomando")
    help_p.add_argument(
        "topic", nargs="?", help="Subcomando cuja ajuda deve ser exibida"
    )

    def add_marketplace_parser(resource: str, help_text: str) -> None:
        """Executa a operação add marketplace parser."""
        resource_parser = sub.add_parser(resource, help=help_text)
        resource_parser.set_defaults(command=resource, resource=resource)
        resource_sub = resource_parser.add_subparsers(dest="action", required=True)
        list_parser = resource_sub.add_parser("list", help="lista itens instalados")
        list_parser.set_defaults(output="human")
        search_parser = resource_sub.add_parser("search", help="pesquisa no catálogo")
        search_parser.add_argument("query", nargs="?", default=None)
        search_parser.set_defaults(output="human")
        info_parser = resource_sub.add_parser("info", help="mostra um item do catálogo")
        info_parser.add_argument("identifier")
        info_parser.set_defaults(output="human")
        install_parser = resource_sub.add_parser("install", help="instala um item")
        if resource == "mcp":
            install_parser.add_argument("identifier")
        else:
            install_parser.add_argument("identifier")
        install_parser.set_defaults(output="human")
        remove_parser = resource_sub.add_parser(
            "remove", help="remove um item instalado"
        )
        remove_parser.add_argument("identifier")
        remove_parser.set_defaults(output="human")
        if resource == "skills":
            validate_parser = resource_sub.add_parser(
                "validate", help="valida uma skill instalada"
            )
            validate_parser.add_argument("identifier")
            validate_parser.set_defaults(output="human")
        for child in resource_sub.choices.values():
            child.add_argument("--output", choices=("human", "json"), default="human")

    add_marketplace_parser("mcp", "Gerencia servidores MCP")
    add_marketplace_parser("skills", "Gerencia skills")

    media_p = sub.add_parser("media", help="Lista e executa ferramentas de mídia")
    media_p.set_defaults(command="media")
    media_sub = media_p.add_subparsers(dest="action", required=True)
    media_list = media_sub.add_parser("list", help="lista ferramentas de mídia")
    media_list.set_defaults(output="human")
    image = media_sub.add_parser("image", help="gera imagem")
    image.add_argument("prompt")
    image.set_defaults(output="human")
    speech = media_sub.add_parser("speech", help="converte texto em fala")
    speech.add_argument("text")
    speech.add_argument("--voice", default="")
    speech.set_defaults(output="human")
    video = media_sub.add_parser("video", help="gera vídeo")
    video.add_argument("prompt")
    video.set_defaults(output="human")
    analyze = media_sub.add_parser("analyze-video", help="analisa vídeo autorizado")
    analyze.add_argument("path")
    analyze.add_argument("question")
    analyze.set_defaults(output="human")
    for child in media_sub.choices.values():
        child.add_argument("--output", choices=("human", "json"), default="human")
        child.add_argument("--user-id", default="local")
        child.add_argument("--model", default="")
        child.add_argument("--thread-id", default="cli")

    hooks_p = sub.add_parser("hooks", help="Lista e testa os hooks do workspace")
    hooks_p.set_defaults(command="hooks")
    hooks_sub = hooks_p.add_subparsers(dest="action", required=True)
    hooks_sub.add_parser("list", help="lista as regras de [[hooks.rules]]")
    hooks_test = hooks_sub.add_parser(
        "test", help="executa uma regra com um contexto sintético (dry-run)"
    )
    hooks_test.add_argument("rule_id")
    hooks_test.add_argument("--tool", default="run_command")
    hooks_test.add_argument("--args", default="{}", help="argumentos da tool (JSON)")
    hooks_test.add_argument("--result", default="", help="resultado (post_tool)")
    hooks_test.add_argument("--response", default="", help="resposta (pre_response)")
    for child in hooks_sub.choices.values():
        child.add_argument("--workspace", default=".")
        child.add_argument("--output", choices=("human", "json"), default="human")

    # ── start — backend + SPA (fullstack/headless) ─────────────────────────────
    start_p = sub.add_parser(
        "start",
        help="Abre o app desktop (Electron + backend)",
        description=(
            "Abre o app desktop: backend (FastAPI) + janela Electron por IPC.\n\n"
            "  vectora start              -> app desktop (falha sem o build do Electron)\n"
            "  vectora start --headless   -> só bandeja + backend, sem janela\n"
            "  vectora web                -> webapp pelo navegador (TCP)\n\n"
            "Em host sem display (VPS/Docker), roda como servidor puro."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    start_p.add_argument(
        "--headless",
        action="store_true",
        help="Não abre janela — mantém backend + bandeja ativos.",
    )
    graphics = start_p.add_argument_group("gráficos do Electron")
    graphics.add_argument(
        "--vulkan",
        dest="graphics_angle",
        action="store_const",
        const="vulkan",
        default=None,
    )
    graphics.add_argument(
        "--gl", "--opengl", dest="graphics_angle", action="store_const", const="gl"
    )
    graphics.add_argument(
        "--auto", dest="graphics_angle", action="store_const", const="default"
    )
    graphics.add_argument(
        "--d3d11", dest="graphics_angle", action="store_const", const="d3d11"
    )
    graphics.add_argument(
        "--d3d11on12", dest="graphics_angle", action="store_const", const="d3d11on12"
    )
    graphics.add_argument(
        "--d3d9", dest="graphics_angle", action="store_const", const="d3d9"
    )
    graphics.add_argument(
        "--metal", dest="graphics_angle", action="store_const", const="metal"
    )
    graphics.add_argument(
        "--x11", dest="graphics_ozone", action="store_const", const="x11", default=None
    )
    graphics.add_argument(
        "--wayland", dest="graphics_ozone", action="store_const", const="wayland"
    )
    graphics.add_argument(
        "--cosmic", dest="graphics_ozone", action="store_const", const="cosmic"
    )
    start_p.add_argument("--host", default="0.0.0.0", help="Host de escuta")  # noqa: S104  # nosec B104
    start_p.add_argument("--port", type=int, default=None, help="Porta (default: 8080)")
    start_p.add_argument(
        "--ssl-certfile",
        metavar="PEM",
        default=None,
        help=(
            "Certificado TLS (PEM fullchain) — sobe em https://. Também via env "
            "SSL_CERTFILE. Necessário para Secure Context ao acessar via IP de LAN."
        ),
    )
    start_p.add_argument(
        "--ssl-keyfile",
        metavar="PEM",
        default=None,
        help="Chave privada TLS correspondente. Também via env SSL_KEYFILE.",
    )

    # ── web — HTTP + Electron/Chromium, sem bandeja Python ───────────────────
    web_p = sub.add_parser(
        "web",
        help="Sobe o webapp com Electron/Chromium (sem bandeja Python)",
        description=(
            "Sobe o backend completo (FastAPI) servindo a SPA via uvicorn,\n"
            "mantém o Electron/Chromium como sidecar e não sobe a bandeja\n"
            "Python. O backend continua acessível pela porta HTTP informada."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    web_p.add_argument("--host", default="0.0.0.0", help="Host de escuta")  # noqa: S104  # nosec B104
    web_p.add_argument("--port", type=int, default=None, help="Porta (default: 8080)")
    web_p.add_argument(
        "--ssl-certfile",
        metavar="PEM",
        default=None,
        help=(
            "Certificado TLS (PEM fullchain) — sobe em https://. Também via env "
            "SSL_CERTFILE. Necessário para Secure Context ao acessar via IP de LAN."
        ),
    )
    web_p.add_argument(
        "--ssl-keyfile",
        metavar="PEM",
        default=None,
        help="Chave privada TLS correspondente. Também via env SSL_KEYFILE.",
    )

    for launch_parser in (start_p, web_p):
        launch_parser.add_argument(
            "--no-services",
            action="store_true",
            help="Inicia sem integrações com Vectora Services nesta execução.",
        )

    # ── config — settings + keys/docker/qdrant/redis ──────────────────────────
    config_p = sub.add_parser(
        "config",
        help="Configuração do aplicativo (settings, keys, docker, qdrant, redis)",
        description=(
            "Sem ação, mostra ou edita ~/.vectora/settings.json.\n"
            "Com ação:\n"
            "  keys                 wizard de API keys + LLM provider\n"
            "  docker [up|down|status]  infra local (Postgres, Redis, Qdrant)\n"
            "  qdrant <url> [--api-key KEY]  testa e persiste Qdrant\n"
            "  redis <url>          testa e persiste Redis\n"
            "  integrations|connect|preferences [--get KEY]... [--set KEY=VALUE]...\n"
            "                       schema declarativo (backend/config/registry.py) —\n"
            "                       mesmas categorias do frontend (Ambiente/Preferências)\n"
            "  provider-routing|memory|account --list [--user-id ID]\n"
            "                       recursos de coleção do registry (backend/config/\n"
            "                       collections.py) — memory/account são por usuário\n"
            "                       (default: 'local', o mesmo sentinela do desktop)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    config_p.add_argument(
        "config_action",
        nargs="?",
        default=None,
        choices=[
            "keys",
            "docker",
            "qdrant",
            "redis",
            "integrations",
            "connect",
            "preferences",
            "provider-routing",
            "memory",
            "account",
        ],
        help="Ação de configuração (sem ação = mostra/edita settings).",
    )
    config_p.add_argument(
        "config_arg",
        nargs="?",
        default=None,
        help="Argumento da ação: up|down|status (docker) ou URL (qdrant/redis).",
    )
    config_p.add_argument(
        "--api-key",
        dest="api_key",
        default=None,
        help="[qdrant] API key opcional.",
    )
    config_p.add_argument(
        "--set",
        metavar="KEY=VALUE",
        action="append",
        dest="set_values",
        help=(
            "Edita uma chave. Repetível. "
            "LLM: active_provider, active_model. "
            "Storage: storage_mode, postgres_dsn, redis_url, qdrant_url, qdrant_api_key. "
            "API keys: google_api_key, openai_api_key, anthropic_api_key, cohere_api_key, tavily_api_key. "
            "Categorias do registry (integrations/connect/preferences): mesmo formato KEY=VALUE."
        ),
    )
    config_p.add_argument(
        "--get",
        metavar="KEY",
        action="append",
        dest="get_values",
        help="[integrations|connect|preferences] Lê uma chave do registry. Repetível.",
    )
    config_p.add_argument(
        "--list",
        action="store_true",
        dest="list_collection",
        help="[provider-routing|memory|account] Lista os itens do recurso de coleção.",
    )
    config_p.add_argument(
        "--user-id",
        dest="user_id",
        default=None,
        help="[memory|account] Dono do recurso per-usuário (default: 'local').",
    )

    # ── sessions ──────────────────────────────────────────────────────────────
    sub.add_parser(
        "sessions",
        help="Lista todas as sessões salvas",
        description="Mostra sessões com ID, data, contagem de mensagens e diretório.",
    )

    # ── doctor ────────────────────────────────────────────────────────────────
    doctor_p = sub.add_parser(
        "doctor",
        help="Encontra e limpa sidecars órfãos (nats-server)",
        description=(
            "Varre o sistema por processos nats-server órfãos (por nome de "
            "imagem, não só os PIDs conhecidos) e oferece finalizá-los."
        ),
    )
    doctor_p.add_argument(
        "--yes",
        action="store_true",
        help="Finaliza sem pedir confirmação (uso não-interativo/scriptável).",
    )

    # ── storage ───────────────────────────────────────────────────────────────
    storage_p = sub.add_parser(
        "storage",
        help="Storage: migrations, diagnóstico, backup/restore, wizard BaaS",
        description=(
            "Comandos de storage:\n"
            "  info               status de saúde de todos os backends\n"
            "  up / down          sobe/para Postgres+pgvector, Redis e Qdrant\n"
            "  test <DSN>         testa conectividade a um banco\n"
            "  wizard             configura o backend interativamente (BaaS)\n"
            "  migrate status|history|plan|upgrade\n"
            "  migrate-embedding-queue --offline\n"
            "  backup / restore <arquivo>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    storage_p.add_argument(
        "action",
        nargs="?",
        choices=[
            "info",
            "up",
            "down",
            "test",
            "wizard",
            "migrate",
            "migrate-embedding-queue",
            "backup",
            "restore",
        ],
        default="info",
        help="Ação de storage (default: info)",
    )
    storage_p.add_argument(
        "subaction",
        nargs="?",
        default=None,
        help="Sub-ação: status/history/plan/upgrade (migrate), DSN (test), arquivo (restore)",
    )
    storage_p.add_argument(
        "version", nargs="?", default=None, help="Parâmetro extra (migrações de dados)"
    )
    storage_p.add_argument(
        "--db", metavar="PATH", default=None, help="Caminho do SQLite (settings.db_dsn)"
    )
    storage_p.add_argument(
        "--output", metavar="FILE", default=None, help="Arquivo de saída do backup"
    )
    storage_p.add_argument(
        "--offline",
        action="store_true",
        help="Confirma que todas as instâncias e workers antigos estão parados",
    )

    # ── auth ──────────────────────────────────────────────────────────────────
    auth_p = sub.add_parser(
        "auth",
        help="Autenticação no servidor Vectora",
        description=(
            "Comandos de autenticação:\n"
            "  signup | login | logout | whoami | refresh\n\n"
            "Sem login, o CLI opera como root local (acesso via filesystem)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    auth_p.add_argument(
        "action",
        nargs="?",
        choices=["signup", "login", "logout", "whoami", "refresh"],
        help="Ação de autenticação.",
    )

    # ── eval ──────────────────────────────────────────────────────────────────
    eval_p = sub.add_parser(
        "eval",
        help="Evaluation Lab para benchmark e replay de decisões",
        description=(
            "Comandos do Evaluation Lab:\n"
            "  smoke --app EXECUTABLE [--timeout-seconds N]\n"
            "  ui-smoke --app EXECUTABLE [--timeout-seconds N]\n"
            "  scenarios [--output human|json]\n"
            "  scenario --scenario-id ID --runner EXECUTABLE --report-dir DIR\n"
            "  suite --runner EXECUTABLE --report-dir DIR [--timeout-seconds N]\n"
            "  report --runs FILE... --report FILE [--format json|csv]\n"
            "  compare-runs --baseline FILE... --candidate FILE... --report FILE\n"
            "  study-validate --study FILE [--output human|json]\n"
            "  run --suite FILE [--report-dir DIR]\n"
            "  compare BASELINE CANDIDATE"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    eval_sub = eval_p.add_subparsers(dest="action", required=True)
    eval_run = eval_sub.add_parser("run", help="Executa benchmark ou suite de cassetes")
    eval_run.add_argument(
        "--suite", default=None, help="Caminho para arquivo de cassete/suite"
    )
    eval_run.add_argument(
        "--report-dir",
        default=None,
        help="Diretório novo para gravar report.json e report.md do replay",
    )
    eval_run.add_argument("--output", choices=("human", "json"), default="human")

    eval_smoke = eval_sub.add_parser(
        "smoke",
        help="Valida a inicialização de uma instalação em perfil temporário",
    )
    eval_smoke.add_argument(
        "--app",
        required=True,
        help="Caminho para o executável instalado do Vectora",
    )
    eval_smoke.add_argument(
        "--timeout-seconds",
        type=float,
        default=120,
        help="Limite para a inicialização e o encerramento do app (padrão: 120)",
    )
    eval_smoke.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Número de perfis novos a iniciar (2 a 100 gera distribuições)",
    )
    eval_smoke.add_argument(
        "--report",
        default=None,
        help="Salva o relatório estruturado neste caminho",
    )
    eval_smoke.add_argument("--output", choices=("human", "json"), default="human")

    eval_ui_smoke = eval_sub.add_parser(
        "ui-smoke",
        help="Valida a montagem do renderer React numa instalação isolada",
    )
    eval_ui_smoke.add_argument(
        "--app",
        required=True,
        help="Caminho para o executável instalado do Vectora",
    )
    eval_ui_smoke.add_argument(
        "--timeout-seconds",
        type=float,
        default=120,
        help="Limite para iniciar, inspecionar e encerrar o app (padrão: 120)",
    )
    eval_ui_smoke.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Número de perfis novos a iniciar (2 a 100 gera distribuições)",
    )
    eval_ui_smoke.add_argument(
        "--report",
        default=None,
        help="Salva o relatório estruturado neste caminho",
    )
    eval_ui_smoke.add_argument("--output", choices=("human", "json"), default="human")

    eval_scenarios = eval_sub.add_parser(
        "scenarios",
        help="Lista cenários versionados e suas pré-condições sem executá-los",
    )
    eval_scenarios.add_argument(
        "--available-capability",
        action="append",
        default=[],
        help="Capability disponível no executor local; pode ser repetido",
    )
    eval_scenarios.add_argument(
        "--provider-configured",
        action="store_true",
        help="Indica que há provider configurado para cenários que o exigem",
    )
    eval_scenarios.add_argument(
        "--provider-remote",
        action="store_true",
        help="Indica que o provider configurado é remoto",
    )
    eval_scenarios.add_argument(
        "--allow-remote-provider",
        action="store_true",
        help="Opt-in explícito para enviar dados sintéticos ao provider remoto",
    )
    eval_scenarios.add_argument("--output", choices=("human", "json"), default="human")

    eval_scenario = eval_sub.add_parser(
        "scenario",
        help="Executa um runner filho com fixture e workspace temporários",
    )
    eval_scenario.add_argument("--scenario-id", required=True)
    eval_scenario.add_argument(
        "--runner", required=True, help="Executável do runner de cenário"
    )
    eval_scenario.add_argument(
        "--runner-arg",
        action="append",
        default=[],
        help="Argumento do runner; pode ser repetido",
    )
    eval_scenario.add_argument(
        "--catalog",
        default=None,
        help="Catálogo versionado (padrão: catálogo v1 do Vectora)",
    )
    eval_scenario.add_argument("--timeout-seconds", type=float, default=120)
    eval_scenario.add_argument("--report-dir", required=True)
    eval_scenario.add_argument("--output", choices=("human", "json"), default="human")

    eval_suite = eval_sub.add_parser(
        "suite",
        help="Executa cenários da suite em subprocessos isolados",
    )
    eval_suite.add_argument(
        "--runner", required=True, help="Executável do runner de cenário"
    )
    eval_suite.add_argument(
        "--runner-arg",
        action="append",
        default=[],
        help="Argumento do runner; pode ser repetido",
    )
    eval_suite.add_argument(
        "--scenario-id",
        action="append",
        default=None,
        help="Executa somente este cenário; pode ser repetido",
    )
    eval_suite.add_argument(
        "--catalog",
        default=None,
        help="Catálogo versionado (padrão: catálogo v1 do Vectora)",
    )
    eval_suite.add_argument("--timeout-seconds", type=float, default=120)
    eval_suite.add_argument("--report-dir", required=True)
    eval_suite.add_argument("--output", choices=("human", "json"), default="human")

    eval_report = eval_sub.add_parser(
        "report", help="Agrega execuções e exporta JSON ou CSV"
    )
    eval_report.add_argument("--runs", nargs="+", required=True)
    eval_report.add_argument("--report", required=True, help="Arquivo de saída")
    eval_report.add_argument("--command", nargs="*", default=[])
    eval_report.add_argument("--format", choices=("json", "csv"), default="json")
    eval_report.add_argument("--public", action="store_true")
    eval_report.add_argument(
        "--reviewed",
        action="store_true",
        help="Confirma revisão de privacidade antes da publicação",
    )

    eval_compare_runs = eval_sub.add_parser(
        "compare-runs",
        help="Compara execuções e verifica a compatibilidade das condições",
    )
    eval_compare_runs.add_argument("--baseline", nargs="+", required=True)
    eval_compare_runs.add_argument("--candidate", nargs="+", required=True)
    eval_compare_runs.add_argument(
        "--report", required=True, help="Arquivo JSON de saída"
    )

    eval_study = eval_sub.add_parser(
        "study-validate",
        help="Valida método e confere referências locais de um estudo técnico",
    )
    eval_study.add_argument("--study", required=True, help="Arquivo JSON do estudo")
    eval_study.add_argument(
        "--repo-root",
        default=".",
        help="Clone Git para verificar referências repo:// (padrão: diretório atual)",
    )
    eval_study.add_argument(
        "--evidence-root",
        default=".",
        help="Diretório de relatórios e datasets locais (padrão: diretório atual)",
    )
    eval_study.add_argument("--output", choices=("human", "json"), default="human")

    eval_compare = eval_sub.add_parser(
        "compare", help="Compara duas cassetes (baseline vs candidate)"
    )
    eval_compare.add_argument("baseline", help="Caminho para cassete baseline")
    eval_compare.add_argument("candidate", help="Caminho para cassete candidata")
    eval_compare.add_argument("--output", choices=("human", "json"), default="human")

    eval_export = eval_sub.add_parser(
        "export-run", help="Exporta um AgentRun gravado para uma cassete"
    )
    eval_export.add_argument(
        "--run-file", required=True, help="Arquivo JSON obtido na consulta do AgentRun"
    )
    eval_export.add_argument(
        "--target", required=True, help="Arquivo de cassete de destino"
    )

    return parser


# ---------------------------------------------------------------------------
# start — backend + SPA via uvicorn
# ---------------------------------------------------------------------------


def _run_start(args: argparse.Namespace, *, force_web: bool = False) -> None:
    """``vectora start`` — sobe FastAPI servindo a SPA via uvicorn.

    Em ``--headless`` registra ``VECTORA_HEADLESS=1`` para a bandeja/Electron
    decidirem não abrir janela; o servidor em si é idêntico.

    ``force_web=True`` (``vectora web``) não sobe a bandeja Python, mas mantém
    o Electron como sidecar quando o binário e um display estiverem disponíveis.
    O servidor continua em TCP para uso pelo browser e por clientes externos.
    """
    if getattr(args, "no_services", False):
        os.environ["VECTORA_NO_SERVICES"] = "1"

    import uvicorn

    if force_web:
        # `vectora web` always exposes TCP, even when launched from an Electron
        # parent that left the desktop transport marker in the environment.
        os.environ.pop("VECTORA_DESKTOP", None)
        # Electron remains available as a hidden Chromium/Node sidecar for
        # browser workbench and CDP, but must never present the desktop app
        # window when the operator explicitly selected the web command.
        os.environ["VECTORA_WEB_MODE"] = "1"

    # `vectora web` keeps the HTTP server and omits only the Python tray. The
    # Electron sidecar remains enabled so its bundled Chromium is the single
    # browser runtime used by the Browser Workbench and the CDP client.
    headless = getattr(args, "headless", False)
    if headless:
        os.environ["VECTORA_HEADLESS"] = "1"

    if getattr(args, "graphics_angle", None):
        os.environ["VECTORA_ANGLE_BACKEND"] = args.graphics_angle
    if getattr(args, "graphics_ozone", None):
        os.environ["VECTORA_OZONE_PLATFORM"] = args.graphics_ozone

    # Precedência da porta: --port > VECTORA_PORT (env do Electron) > 8080.
    port = args.port or int(os.environ.get("VECTORA_PORT") or 0) or 8080
    os.environ["VECTORA_PORT"] = str(port)  # lido pelo sidecar Electron no lifespan
    uvicorn_log_level = os.environ.get("VECTORA_UVICORN_LOG_LEVEL", "warning")

    # Backend-primário mesmo em dev: em produção é o Electron quem spawna o
    # backend (VECTORA_DESKTOP=1 já vem setado por ele). Quando rodado direto
    # (`uv run vectora start`/binário fora do Electron) e nenhum dos dois já
    # está setado, este processo se autoelege "primário" — só a DECISÃO
    # acontece aqui (cedo, porque também define o transporte IPC logo
    # abaixo); o spawn em si roda depois, de dentro do lifespan async do
    # FastAPI (backend/services/electron_sidecar.py), como qualquer outro
    # sidecar (NATS) — não do bootstrap síncrono da CLI. Mantém `vectora
    # start` leve pra quem só quer a API REST: o processo ASGI decide
    # sozinho, no seu próprio startup, se faz sentido subir uma janela.
    from backend.services.electron_sidecar import (
        desktop_build_missing,
        should_spawn_electron,
    )

    dev_electron = should_spawn_electron()
    if not dev_electron and not force_web and desktop_build_missing():
        print(
            "❌ `vectora start` abre o app desktop, mas o build do Electron não foi "
            "encontrado.\n"
            "   Rode `scons frontend` e tente de novo. Para usar pelo navegador, "
            "rode `vectora web`; para servidor sem janela, `vectora start --headless`."
        )
        sys.exit(1)
    if dev_electron:
        # A development desktop must never reuse the installed app's
        # databases, sockets, PID file or NATS JetStream store. Keep an
        # explicit VECTORA_HOME untouched so callers can choose another
        # profile for automated runs.
        os.environ.setdefault("VECTORA_RUNTIME_PROFILE", "dev")
        # `vectora start` uses desktop IPC. `vectora web` must keep TCP
        # reachable for web clients while Electron attaches as a sidecar.
        if not force_web:
            os.environ["VECTORA_DESKTOP"] = "1"
        os.environ["VECTORA_SPAWN_ELECTRON"] = "1"
        logger.info(
            "Electron (dev) resolvido — sobe como sidecar no startup do FastAPI"
        )

    # Fixe o perfil e a home antes de importar módulos que podem construir o
    # singleton de settings. Assim o backend iniciado diretamente usa a mesma
    # home isolada que o Electron, inclusive para perfis nomeados.
    runtime_profile = sanitize_runtime_profile(
        os.environ.get("VECTORA_RUNTIME_PROFILE"),
        "dev" if dev_electron else "stable",
    )
    os.environ["VECTORA_RUNTIME_PROFILE"] = runtime_profile
    configured_home = os.environ.get("VECTORA_HOME")
    if configured_home:
        os.environ["VECTORA_HOME"] = str(normalize_runtime_home(configured_home))
    else:
        os.environ["VECTORA_HOME"] = str(runtime_home_for_profile(runtime_profile))

    from backend.api.server import create_app

    # TLS opcional — CLI tem prioridade; settings (env SSL_CERTFILE/SSL_KEYFILE
    # ou ~/.vectora/.env) é o fallback. Com cert+key o uvicorn serve https://,
    # necessário para Secure Context (crypto.randomUUID etc.) via IP de LAN.
    ssl_certfile = args.ssl_certfile
    ssl_keyfile = args.ssl_keyfile
    if not ssl_certfile or not ssl_keyfile:
        try:
            from backend.settings import settings as _settings

            ssl_certfile = ssl_certfile or _settings.ssl_certfile
            ssl_keyfile = ssl_keyfile or _settings.ssl_keyfile
        except Exception:
            pass
    if bool(ssl_certfile) != bool(ssl_keyfile):
        print(
            "❌ TLS requer certificado E chave: informe --ssl-certfile e "
            "--ssl-keyfile (ou SSL_CERTFILE/SSL_KEYFILE)."
        )
        sys.exit(1)
    use_tls = bool(ssl_certfile and ssl_keyfile)

    # Transporte desktop é IPC real: sob VECTORA_DESKTOP=1 (Electron) o backend
    # não expõe porta TCP ao SO. Em Linux/macOS usa unix socket; no Windows usa
    # named pipe (ipc_pipe_win). Web/VPS mantém TCP (servidor de rede, por design).
    uds_path: str | None = None
    if os.environ.get("VECTORA_DESKTOP") and sys.platform != "win32":
        from backend.settings import settings as _settings_ipc

        sock_dir = _settings_ipc.vectora_home
        sock_dir.mkdir(parents=True, exist_ok=True)
        uds_path = str(sock_dir / "vectora.sock")

    from backend.version import __version__

    # The desktop transport is the named pipe (Windows) bridging to this
    # listener; it must stay on loopback and never be reachable from the LAN.
    desktop_ipc = bool(os.environ.get("VECTORA_DESKTOP")) and not force_web
    host = "127.0.0.1" if desktop_ipc else args.host

    app = create_app()
    scheme = "https" if use_tls else "http"
    if uds_path:
        logger.info(
            "Iniciando Vectora v%s via unix socket %s (desktop IPC)",
            __version__,
            uds_path,
        )
        config = uvicorn.Config(
            app,
            uds=uds_path,
            log_level=uvicorn_log_level,
            access_log=False,
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
    else:
        logger.info(
            "Iniciando Vectora v%s em %s://%s:%d (%s)",
            __version__,
            scheme,
            host,
            port,
            "web+electron"
            if force_web
            else "desktop IPC"
            if desktop_ipc
            else ("headless" if headless else "fullstack"),
        )
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=uvicorn_log_level,
            access_log=False,
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
    server = uvicorn.Server(config)

    # Uvicorn configura handlers próprios para ``uvicorn.error`` durante a
    # criação de Config, fora dos handlers da raiz instalados pelo backend.
    # Anexa o filtro após essa configuração para cancelar sem traceback as
    # requisições interrompidas pelo shutdown gracioso.
    from backend.services.log_setup import install_uvicorn_shutdown_filter

    install_uvicorn_shutdown_filter()

    def run_with_runtime_lock(action: Callable[[], None]) -> None:
        from backend.runtime_lock import (
            RuntimeHomeAlreadyRunningError,
            lock_runtime_home,
            unix_socket_is_active,
        )

        runtime_home = Path(os.environ["VECTORA_HOME"])
        try:
            with lock_runtime_home(runtime_home):
                if uds_path:
                    socket_path = Path(uds_path)
                    if unix_socket_is_active(socket_path):
                        raise RuntimeHomeAlreadyRunningError(
                            f"Outra instância do Vectora já está ouvindo em {socket_path}."
                        )
                    with contextlib.suppress(FileNotFoundError):
                        socket_path.unlink()
                action()
        except RuntimeHomeAlreadyRunningError as exc:
            logger.log(logging.ERROR, "runtime já está em uso: %s", exc)
            raise SystemExit(1) from exc

    # Aplicado ANTES do branch Windows named-pipe abaixo — esse branch tem um
    # `return` próprio, e sem instalar o handler aqui primeiro ele nunca seria
    # alcançado nesse caminho (o mais comum em dev desktop no Windows).
    icon_ref: list[Any] = [None]
    if _should_install_terminal_signals(dict(os.environ), force_web=force_web):
        _install_terminal_signals(server, icon_ref)
        # Uvicorn installs its own capture handler inside ``serve``. Keep the
        # process-owner handler above authoritative for console signals.
        _disable_uvicorn_signal_capture(server)

    if os.environ.get("VECTORA_SPAWN_ELECTRON"):
        from backend.services.electron_sidecar import set_backend_shutdown_callback

        def _shutdown_from_electron() -> None:
            """Executa a operação shutdown from electron."""
            logger.info("Vectora: Electron encerrou — iniciando shutdown coordenado")
            # Sem a marca, o filtro de logs trata o CancelledError das respostas
            # streaming cortadas como erro real e imprime o traceback.
            from backend.services.log_setup import mark_shutdown_requested

            mark_shutdown_requested()
            server.should_exit = True
            shutdown_event = _SHUTDOWN_EVENTS.get(id(server))
            if shutdown_event is not None:
                shutdown_event.set()
            if icon_ref[0] is not None:
                icon_ref[0].stop()

        set_backend_shutdown_callback(_shutdown_from_electron)

    # Windows + VECTORA_DESKTOP: named pipe em vez de TCP — nenhuma porta TCP é
    # exposta ao SO. O Electron conecta via \\.\pipe\vectora-<pid>, lido de stdout.
    if sys.platform == "win32" and os.environ.get("VECTORA_DESKTOP"):
        from backend.services.ipc_pipe_win import PIPE_ENV_VAR, pipe_name, serve_pipe

        _pipe = pipe_name()
        os.environ[PIPE_ENV_VAR] = _pipe
        print(f"{PIPE_ENV_VAR}={_pipe}", flush=True)

        async def _run_win() -> None:
            """Executa a operação run win."""
            shutdown_event = asyncio.Event()
            _SHUTDOWN_EVENTS[id(server)] = shutdown_event
            pipe_task = asyncio.create_task(serve_pipe(_pipe, "127.0.0.1", port))

            async def _stop_pipe_on_shutdown() -> None:
                # O listener da pipe não faz parte do lifespan do FastAPI.
                # Se ele continuar aceitando clientes depois do Ctrl+C, o
                # Electron mantém tentativas TCP vivas enquanto o uvicorn já
                # está fechando, atrasando o retorno ao terminal.
                """Executa a operação stop pipe on shutdown."""
                await shutdown_event.wait()
                pipe_task.cancel()

            shutdown_pipe_task = asyncio.create_task(_stop_pipe_on_shutdown())
            try:
                await server.serve()
            finally:
                _SHUTDOWN_EVENTS.pop(id(server), None)
                pipe_task.cancel()
                shutdown_pipe_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pipe_task
                with contextlib.suppress(asyncio.CancelledError):
                    await shutdown_pipe_task

        run_with_runtime_lock(lambda: asyncio.run(_run_win()))
        # Mesmo `return` cedo do ramo Windows+desktop precisa do os._exit(0)
        # abaixo — sem ele, threads não-daemon (httpx/SQLite do tracer)
        # mantêm o interpreter vivo mesmo após o shutdown gracioso
        # do uvicorn (Ctrl+C nunca fechava o processo; só o kill via tray
        # do Electron encerrava, por caminho totalmente diferente).
        logger.info("Vectora: encerrando processo")
        os._exit(_exit_status())
        return  # pragma: no cover - os._exit não retorna fora de teste

    if force_web:
        # `vectora web`: sem bandeja Python, mas com Electron sidecar quando
        # disponível. O backend continua em TCP para clientes web externos.
        logger.warning(
            "\n\n  ✨  Vectora rodando em %s://localhost:%d com Electron/Chromium — "
            "acesse pelo app Electron ou pelo browser.\n",
            scheme,
            port,
        )
        run_with_runtime_lock(lambda: asyncio.run(server.serve()))
        logger.info("Vectora: server.serve retornou após shutdown")
    else:
        # Sobe o servidor e, quando há display, a bandeja do sistema (Python).
        # Sem display (VPS/Docker) ou sem pystray, degrada para servidor puro.
        # A bandeja bloqueia a main thread até "Sair"; o servidor roda em
        # thread de fundo.
        from backend.services.tray import run_server_with_tray

        run_with_runtime_lock(
            lambda: run_server_with_tray(
                server,
                f"{scheme}://localhost:{port}",
                headless=headless,
                icon_ref=icon_ref,
            )
        )

    # Retorno do tray/servidor = shutdown concluído. No Windows, threads
    # não-daemon de libs externas (httpx, SQLite do tracer) mantêm o
    # interpreter vivo; os._exit ignora-as e libera o terminal. Os recursos
    # críticos já foram fechados no lifespan.
    logger.info("Vectora: encerrando processo")
    os._exit(_exit_status())


def _exit_status() -> int:
    """1 quando o app desktop não abriu a janela; 0 num shutdown normal."""
    from backend.services.electron_sidecar import startup_failed

    return 1 if startup_failed() else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _run_config_command(args: argparse.Namespace) -> None:
    """Executa a operação run config command."""
    from backend.cli.config import run_config

    run_config(args)


def _run_sessions_command(_args: argparse.Namespace) -> None:
    """Executa a operação run sessions command."""
    from backend.cli.sessions import run_sessions

    run_sessions()


def _run_storage_command(args: argparse.Namespace) -> None:
    """Executa a operação run storage command."""
    from backend.cli.storage import run_storage

    run_storage(args)


def _run_doctor_command(args: argparse.Namespace) -> None:
    """Executa a operação run doctor command."""
    from backend.cli.doctor import run_doctor

    run_doctor(args)


def _run_marketplace_command(args: argparse.Namespace) -> None:
    """Executa a operação run marketplace command."""
    from backend.cli.marketplace import run_marketplace

    run_marketplace(args)


def _run_media_command(args: argparse.Namespace) -> None:
    """Executa a operação run media command."""
    from backend.cli.media import run_media

    run_media(args)


def _run_hooks_command(args: argparse.Namespace) -> None:
    """Executa ``vectora hooks``."""
    from backend.cli.hooks import run_hooks

    run_hooks(args)


def _run_auth_command(args: argparse.Namespace) -> None:
    """Executa a operação run auth command."""
    from backend.auth import (
        cmd_login,
        cmd_logout,
        cmd_refresh,
        cmd_signup,
        cmd_whoami,
    )

    action = getattr(args, "action", None)
    handlers = {
        "signup": cmd_signup,
        "login": cmd_login,
        "logout": cmd_logout,
        "whoami": cmd_whoami,
        "refresh": cmd_refresh,
    }
    handler = handlers.get(action or "")
    if handler is None:
        print(
            "Uso: vectora auth <ação>\n"
            "Ações: signup | login | logout | whoami | refresh\n"
            "Exemplo: vectora auth login"
        )
        sys.exit(1)
    sys.exit(handler(args))


def _run_eval_command(args: argparse.Namespace) -> None:
    """Executa a operação run eval command."""
    from backend.cli.eval import handle_eval

    sys.exit(handle_eval(args))


# Subcomandos que só precisam de "importa o módulo, chama o handler com
# args" — start/web (têm variantes force_web) e auth (tratado à parte
# acima, mas despachado pela mesma tabela) ficam fora por clareza no
# restante de `run()`.
_COMMAND_HANDLERS: dict[str, Any] = {
    "config": _run_config_command,
    "sessions": _run_sessions_command,
    "storage": _run_storage_command,
    "doctor": _run_doctor_command,
    "auth": _run_auth_command,
    "mcp": _run_marketplace_command,
    "skills": _run_marketplace_command,
    "media": _run_media_command,
    "hooks": _run_hooks_command,
    "eval": _run_eval_command,
}


def run() -> None:
    """Ponto de entrada síncrono — comando ``vectora``."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.tests:
        from backend.cli.runtime_tests import run_installed_runtime_tests

        raise SystemExit(run_installed_runtime_tests())

    command = getattr(args, "command", None)

    if command is None:
        parser.print_help()
        return

    if command == "help":
        topic = getattr(args, "topic", None)
        if topic:
            # Delegates to argparse so an unknown topic fails like any
            # invalid subcommand and a known one prints its own help.
            parser.parse_args([topic, "--help"])
        parser.print_help()
        return

    if command == "start":
        _run_start(args)
        return

    if command == "web":
        _run_start(args, force_web=True)
        return

    handler = _COMMAND_HANDLERS.get(command)
    if handler is not None:
        handler(args)
        return

    parser.print_help()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        run()
