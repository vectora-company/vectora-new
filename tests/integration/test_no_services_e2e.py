"""`vectora start --no-services` de ponta a ponta: o processo real não fala com o Services.

Sobe o servidor de verdade num subprocesso cujo Python registra todo `getaddrinfo`
e `connect` para fora do loopback. O teste confere que, depois de iniciar, de
usar as rotas de conta e de esperar as tarefas de segundo plano, nenhum destino
do Vectora foi consultado. Um controle prova que o registrador enxerga uma
consulta de DNS, para o resultado vazio não ser um falso verde.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_RECORDER = textwrap.dedent(
    """
    import os, socket
    _log = os.environ["VECTORA_NETLOG"]
    _LOCAL = (None, "localhost", "127.0.0.1", "::1", "0.0.0.0")

    def _rec(kind, target):
        with open(_log, "a") as stream:
            stream.write(f"{kind} {target}\\n")

    _getaddrinfo = socket.getaddrinfo
    def getaddrinfo(host, *args, **kwargs):
        if host not in _LOCAL:
            _rec("dns", host)
        return _getaddrinfo(host, *args, **kwargs)
    socket.getaddrinfo = getaddrinfo

    _connect = socket.socket.connect
    def connect(self, address):
        if isinstance(address, tuple) and address and address[0] not in _LOCAL:
            _rec("connect", address[0])
        return _connect(self, address)
    socket.socket.connect = connect
    """
)

_SERVICES_HOSTS = ("vectora.company", "vectora.chat", "vectora.run")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port: int, method: str, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


@pytest.fixture
def recorder_dir(tmp_path: Path) -> Path:
    (tmp_path / "sitecustomize.py").write_text(_RECORDER, encoding="utf-8")
    return tmp_path


def _env(recorder_dir: Path, home: Path, log: Path) -> dict[str, str]:
    env = {**os.environ}
    env.pop("VECTORA_NO_SERVICES", None)
    env.update(
        PYTHONPATH=os.pathsep.join(
            [str(recorder_dir), *filter(None, [env.get("PYTHONPATH")])]
        ),
        VECTORA_NETLOG=str(log),
        VECTORA_HOME=str(home),
        VECTORA_AUTH_REQUIRED="false",
    )
    return env


def _lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_o_registrador_enxerga_uma_consulta_de_dns(
    recorder_dir: Path, tmp_path: Path
) -> None:
    """Controle: sem ele, "nenhuma consulta" poderia ser só um registrador mudo."""
    log = tmp_path / "controle.log"
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import socket\n"
                "try:\n"
                "    socket.getaddrinfo('services.vectora.company', 443)\n"
                "except OSError:\n"
                "    pass\n"
            ),
        ],
        env=_env(recorder_dir, tmp_path / "home", log),
        check=True,
        timeout=30,
    )

    assert "dns services.vectora.company" in _lines(log)


def test_start_sem_services_nao_consulta_nenhum_destino_do_vectora(
    recorder_dir: Path, tmp_path: Path
) -> None:
    log = tmp_path / "rede.log"
    home = tmp_path / "home"
    port = _free_port()
    processo = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backend.main",
            "start",
            "--headless",
            "--no-services",
            "--port",
            str(port),
        ],
        env=_env(recorder_dir, home, log),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if processo.poll() is not None:
                pytest.fail(f"o servidor encerrou com código {processo.returncode}")
            try:
                if _request(port, "GET", "/health")[0] == 200:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(1)
        else:
            pytest.fail("o servidor não respondeu a /health em 90 s")

        licenca = _request(port, "GET", "/license/status")
        gateway = _request(port, "GET", "/gateway/status")
        portal = _request(port, "POST", "/license/portal")
        conta = _request(port, "POST", "/license/oauth/init")
        # As tarefas de segundo plano (licença, gateway, catálogos) disparam logo
        # depois do início: espera um pouco antes de olhar o registro.
        time.sleep(8)
    finally:
        os.killpg(os.getpgid(processo.pid), signal.SIGTERM)
        try:
            processo.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(processo.pid), signal.SIGKILL)

    assert licenca[0] == 200 and licenca[1]["services_enabled"] is False
    assert gateway == (
        200,
        {"connected": False, "state": "disabled", "detail": "--no-services"},
    )
    assert portal[0] == 503 and "--no-services" in portal[1]["detail"]
    assert conta[0] == 503

    destinos = [linha.split(" ", 1)[1] for linha in _lines(log)]
    do_vectora = [
        d
        for d in destinos
        if any(d == h or d.endswith("." + h) for h in _SERVICES_HOSTS)
    ]
    assert do_vectora == [], f"consultou o Services: {do_vectora}"
