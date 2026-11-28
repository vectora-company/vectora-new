"""Rede do jail: sem rota própria, só o proxy de saída decide o que sai."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import socket
import ssl
import sys
from pathlib import Path

import pytest

from backend.sandbox import egress_bridge, egress_proxy, workspace_jail
from backend.sandbox.dry_run import build_bwrap_command
from backend.sandbox.egress_proxy import (
    JAIL_PROXY_PORT,
    JAIL_SOCKET_DIR,
    SOCKET_NAME,
    EgressProxy,
    EgressRegistry,
    _split_authority,
    egress_dir_for,
)
from backend.sandbox.policy import (
    REGISTRY_HOSTS,
    CredentialBinding,
    SandboxPolicy,
    effective_allow_hosts,
    effective_network,
    parse_policy,
    resolve_policy,
    trusted_policy_path,
)
from tests.unit.test_sandbox_hardening import _bwrap_works, _exec

real_bwrap = pytest.mark.skipif(
    not _bwrap_works(), reason="bwrap sem user namespaces neste ambiente"
)


def _process_memory_contains(pid: int, needle: bytes) -> bool:
    """Procura um segredo no espaço legível do processo filho do teste."""
    maps = Path(f"/proc/{pid}/maps").read_text(encoding="ascii")
    memory_fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    try:
        for line in maps.splitlines():
            address_range, permissions, *_ = line.split(maxsplit=2)
            if "r" not in permissions:
                continue
            start_text, end_text = address_range.split("-", maxsplit=1)
            offset = int(start_text, 16)
            end = int(end_text, 16)
            overlap = b""
            while offset < end:
                try:
                    chunk = os.pread(memory_fd, min(1024 * 1024, end - offset), offset)
                except OSError:
                    break
                if not chunk:
                    break
                searchable = overlap + chunk
                if needle in searchable:
                    return True
                overlap = searchable[-(len(needle) - 1) :]
                offset += len(chunk)
    finally:
        os.close(memory_fd)
    return False


@pytest.fixture(autouse=True)
def _sem_deteccao_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.sandbox import policy as policy_module

    monkeypatch.setattr(policy_module, "_native_sandbox_available_sync", lambda: False)


def _setenv(argv: list[str]) -> dict[str, str]:
    return {
        argv[i + 1]: argv[i + 2] for i, item in enumerate(argv) if item == "--setenv"
    }


# ── política de rede ────────────────────────────────────────────────────────


def test_o_padrao_e_rede_filtrada() -> None:
    assert SandboxPolicy(enabled=True).network == "filtered"


def test_lockdown_sempre_corta_a_rede() -> None:
    assert effective_network(SandboxPolicy(enabled=True, lockdown=True)) == "off"
    assert (
        effective_network(SandboxPolicy(enabled=True, lockdown=True, network="open"))
        == "off"
    )


def test_destinos_permitidos_somam_registries_e_extras_sem_repetir() -> None:
    policy = SandboxPolicy(enabled=True, allow_hosts=("api.exemplo.com", "pypi.org"))

    hosts = effective_allow_hosts(policy)

    assert set(REGISTRY_HOSTS) <= set(hosts)
    assert {"api.exemplo.com"} <= set(hosts)
    assert hosts.count("pypi.org") == 1


def test_sem_registries_so_os_destinos_declarados() -> None:
    policy = SandboxPolicy(enabled=True, registry_hosts=False, allow_hosts=("a.com",))

    assert effective_allow_hosts(policy) == ("a.com",)


def test_parse_le_rede_e_destinos(tmp_path: Path) -> None:
    arquivo = tmp_path / "p.toml"
    arquivo.write_text(
        '[sandbox]\nnetwork = "open"\nallow_hosts = ["a.com"]\nregistry_hosts = false\n'
    )

    policy = parse_policy(arquivo)

    assert (policy.network, policy.allow_hosts, policy.registry_hosts) == (
        "open",
        ("a.com",),
        False,
    )


def test_modo_de_rede_desconhecido_falha_fechado(tmp_path: Path) -> None:
    arquivo = tmp_path / "p.toml"
    arquivo.write_text('[sandbox]\nnetwork = "tudo"\n')

    policy = parse_policy(arquivo)

    assert policy.lockdown is True


def test_repositorio_nao_abre_a_rede(tmp_path: Path) -> None:
    (tmp_path / "vectora.toml").write_text('[sandbox]\nnetwork = "open"\n')

    policy = resolve_policy(tmp_path)

    assert policy.network == "filtered"
    assert "network" in policy.ignored_repo_keys


def test_repositorio_pode_cortar_a_rede(tmp_path: Path) -> None:
    (tmp_path / "vectora.toml").write_text('[sandbox]\nnetwork = "off"\n')

    assert resolve_policy(tmp_path).network == "off"


def test_repositorio_nao_amplia_destinos(tmp_path: Path) -> None:
    trusted_policy_path(tmp_path).write_text('[sandbox]\nallow_hosts = ["a.com"]\n')
    (tmp_path / "vectora.toml").write_text(
        '[sandbox]\nallow_hosts = ["a.com", "evil.com"]\nregistry_hosts = true\n'
    )

    policy = resolve_policy(tmp_path)

    assert policy.allow_hosts == ("a.com",)
    assert "allow_hosts" in policy.ignored_repo_keys


def test_politica_recusa_binding_com_variavel_de_ambiente_reservada(
    tmp_path: Path,
) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        '[sandbox]\ncredential_bindings = [{name = "PATH", '
        'host = "api.example.com", header = "Authorization"}]\n'
    )

    assert parse_policy(path).lockdown is True


def test_repositorio_pode_tirar_os_registries(tmp_path: Path) -> None:
    trusted_policy_path(tmp_path).write_text("[sandbox]\nregistry_hosts = true\n")
    (tmp_path / "vectora.toml").write_text("[sandbox]\nregistry_hosts = false\n")

    assert resolve_policy(tmp_path).registry_hosts is False


# ── comando do bwrap ────────────────────────────────────────────────────────


def test_rede_aberta_herda_a_do_host() -> None:
    argv = build_bwrap_command(
        SandboxPolicy(enabled=True, network="open"), "/ws", ["true"]
    )

    assert "--unshare-net" not in argv
    assert "HTTPS_PROXY" not in _setenv(argv)


def test_sem_rede_corta_e_nao_liga_ponte() -> None:
    argv = build_bwrap_command(
        SandboxPolicy(enabled=True, network="off"), "/ws", ["true"]
    )

    assert "--unshare-net" in argv
    assert "backend.sandbox.egress_bridge" not in argv


def test_filtrada_sem_proxy_nunca_abre_por_omissao() -> None:
    argv = build_bwrap_command(SandboxPolicy(enabled=True), "/ws", ["true"])

    assert "--unshare-net" in argv
    assert "backend.sandbox.egress_bridge" not in argv


def test_filtrada_com_proxy_monta_o_socket_aponta_o_ambiente_e_liga_a_ponte(
    tmp_path: Path,
) -> None:
    argv = build_bwrap_command(
        SandboxPolicy(enabled=True), "/ws", ["echo", "oi"], egress_dir=str(tmp_path)
    )
    env = _setenv(argv)

    assert "--unshare-net" in argv
    bind = argv.index(JAIL_SOCKET_DIR)
    assert argv[bind - 2 : bind + 1] == ["--bind", str(tmp_path), JAIL_SOCKET_DIR]
    assert env["HTTPS_PROXY"] == f"http://127.0.0.1:{JAIL_PROXY_PORT}"
    assert env["https_proxy"] == env["HTTPS_PROXY"]
    assert "127.0.0.1" in env["NO_PROXY"]
    tail = argv[argv.index("--", argv.index("--chdir")) + 1 :]
    assert tail[:3] == [sys.executable, "-m", "backend.sandbox.egress_bridge"]
    assert tail[-3:] == ["--", "echo", "oi"]
    assert f"{JAIL_SOCKET_DIR}/{SOCKET_NAME}" in tail


# ── proxy ───────────────────────────────────────────────────────────────────


async def _pedir(directory: str, request: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(
        str(Path(directory) / SOCKET_NAME)
    )
    writer.write(request)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(65536), timeout=10)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_connect_para_destino_fora_da_allowlist_e_bloqueado_e_registrado() -> (
    None
):
    proxy = EgressProxy(SandboxPolicy(enabled=True))
    directory = await proxy.start()
    try:
        resposta = await _pedir(
            directory, b"CONNECT evil.example.org:443 HTTP/1.1\r\n\r\n"
        )
    finally:
        await proxy.stop()

    assert resposta.startswith(b"HTTP/1.1 403")
    assert b"X-Vectora-Egress: blocked" in resposta
    (decisao,) = proxy.decisions
    assert (decisao.host, decisao.port, decisao.allowed) == (
        "evil.example.org",
        443,
        False,
    )
    assert "allowlist" in decisao.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destino", ["127.0.0.1:443", "169.254.169.254:80", "10.0.0.5:443", "[::1]:443"]
)
async def test_connect_para_ip_privado_ou_de_metadados_e_bloqueado(
    destino: str,
) -> None:
    proxy = EgressProxy(SandboxPolicy(enabled=True, allow_hosts=("*",)))
    # Mesmo com o destino na allowlist, o IP privado é recusado.
    proxy._egress.allowed_hosts = []
    directory = await proxy.start()
    try:
        resposta = await _pedir(
            directory, f"CONNECT {destino} HTTP/1.1\r\n\r\n".encode()
        )
    finally:
        await proxy.stop()

    assert resposta.startswith(b"HTTP/1.1 403")
    assert not proxy.decisions[0].allowed


@pytest.mark.asyncio
async def test_porta_fora_de_80_e_443_e_bloqueada() -> None:
    proxy = EgressProxy(SandboxPolicy(enabled=True))
    directory = await proxy.start()
    try:
        resposta = await _pedir(directory, b"CONNECT pypi.org:22 HTTP/1.1\r\n\r\n")
    finally:
        await proxy.stop()

    assert resposta.startswith(b"HTTP/1.1 403")
    assert "Porta" in proxy.decisions[0].reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requisicao",
    [
        b"GET http://pypi.org/ HTTP/1.1\r\n\r\n",
        b"POST / HTTP/1.1\r\n\r\n",
        b"CONNECT sem-porta HTTP/1.1\r\n\r\n",
        b"lixo\r\n\r\n",
    ],
)
async def test_so_aceita_connect_bem_formado(requisicao: bytes) -> None:
    proxy = EgressProxy(SandboxPolicy(enabled=True))
    directory = await proxy.start()
    try:
        resposta = await _pedir(directory, requisicao)
    finally:
        await proxy.stop()

    assert resposta.split()[1] in {b"400", b"403"}
    assert not proxy.decisions or not proxy.decisions[0].allowed


@pytest.mark.asyncio
async def test_destino_permitido_abre_o_tunel_ate_o_ip_validado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def eco(reader, writer) -> None:
        writer.write(b"eco:" + await reader.read(100))
        await writer.drain()
        writer.close()

    servidor = await asyncio.start_server(eco, "127.0.0.1", 0)
    porta = servidor.sockets[0].getsockname()[1]

    async def liberado(url, policy=None):
        # O proxy conecta ao IP que a validação devolveu, não ao nome.
        return True, "", "127.0.0.1"

    monkeypatch.setattr(egress_proxy, "async_is_url_allowed", liberado)
    proxy = EgressProxy(SandboxPolicy(enabled=True))
    directory = await proxy.start()
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(Path(directory) / SOCKET_NAME)
        )
        writer.write(f"CONNECT pypi.org:{porta} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        cabecalho = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        writer.write(b"ola")
        await writer.drain()
        dados = await asyncio.wait_for(reader.read(100), 10)
        writer.close()
    finally:
        await proxy.stop()
        servidor.close()

    assert cabecalho.startswith(b"HTTP/1.1 200")
    assert dados == b"eco:ola"
    assert proxy.decisions[0].allowed is True


@pytest.mark.asyncio
async def test_credencial_so_e_injetada_no_host_vinculado(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend.sandbox.tls_interception import WorkspaceTLSAuthority

    authority = WorkspaceTLSAuthority(tmp_path)
    upstream_seen: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    async def upstream(reader, writer) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        upstream_seen.set_result(request)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    upstream_server_context = authority.server_context("api.example.com")
    upstream_server = await asyncio.start_server(
        upstream, "127.0.0.1", 0, ssl=upstream_server_context
    )
    port = upstream_server.sockets[0].getsockname()[1]

    async def allow_local(url, policy=None):
        return True, "", "127.0.0.1"

    original_open_connection = asyncio.open_connection

    async def connect_upstream(host=None, port=None, *args, **kwargs):
        if host == "127.0.0.1" and port == 443:
            port = upstream_server.sockets[0].getsockname()[1]
        return await original_open_connection(host, port, *args, **kwargs)

    class Secrets:
        async def get(self, user_id, key) -> str:
            assert (user_id, key) == ("owner", "sandbox:ws:API_TOKEN")
            return "top-secret-value"

    monkeypatch.setattr(egress_proxy, "async_is_url_allowed", allow_local)
    monkeypatch.setattr(asyncio, "open_connection", connect_upstream)
    monkeypatch.setattr(egress_proxy, "_workspace_credential_user", lambda _: "owner")
    monkeypatch.setattr("backend.secrets.get_secrets_provider", Secrets)
    monkeypatch.setattr(ssl, "create_default_context", ssl._create_unverified_context)
    proxy = EgressProxy(
        SandboxPolicy(
            enabled=True,
            allow_hosts=("api.example.com",),
            credential_bindings=(
                CredentialBinding(
                    "API_TOKEN", "api.example.com", "Authorization", "Bearer "
                ),
            ),
        ),
        workspace_id="ws",
    )
    directory = await proxy.start()
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(Path(directory) / SOCKET_NAME)
        )
        writer.write(b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.load_verify_locations(
            cafile=str(Path(directory) / "sandbox-ca.pem")
        )
        client_context.check_hostname = True
        # start_tls wraps the existing proxy stream after CONNECT.
        await writer.start_tls(client_context, server_hostname="api.example.com")
        writer.write(
            b"GET /private HTTP/1.1\r\nHost: api.example.com\r\n"
            b"Authorization: attacker-value\r\nConnection: keep-alive\r\n\r\n"
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), 5)
        assert upstream_seen.done(), response
        forwarded = await asyncio.wait_for(upstream_seen, 5)
        writer.close()
    finally:
        await proxy.stop()
        upstream_server.close()
        await upstream_server.wait_closed()
        authority.close()

    assert b"HTTP/1.1 200 OK" in response
    assert b"Authorization: Bearer top-secret-value\r\n" in forwarded
    assert b"attacker-value" not in forwarded
    assert b"top-secret-value" not in response


@pytest.mark.asyncio
async def test_host_sem_binding_permanece_tunel_tls_sem_credencial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend.sandbox.tls_interception import WorkspaceTLSAuthority

    authority = WorkspaceTLSAuthority(tmp_path)
    upstream_seen = asyncio.get_running_loop().create_future()

    async def upstream(reader, writer) -> None:
        upstream_seen.set_result(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(
        upstream, "127.0.0.1", 0, ssl=authority.server_context("other.example.com")
    )
    upstream_port = server.sockets[0].getsockname()[1]
    original_open_connection = asyncio.open_connection

    async def connect_upstream(host=None, port=None, *args, **kwargs):
        if host == "127.0.0.1" and port == 443:
            port = upstream_port
        return await original_open_connection(host, port, *args, **kwargs)

    async def allow_local(url, policy=None):
        return True, "", "127.0.0.1"

    monkeypatch.setattr(asyncio, "open_connection", connect_upstream)
    monkeypatch.setattr(egress_proxy, "async_is_url_allowed", allow_local)
    proxy = EgressProxy(
        SandboxPolicy(
            enabled=True,
            allow_hosts=("api.example.com", "other.example.com"),
            credential_bindings=(
                CredentialBinding(
                    "API_TOKEN", "api.example.com", "Authorization", "Bearer "
                ),
            ),
        ),
        workspace_id="ws",
    )
    directory = await proxy.start()
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(Path(directory) / SOCKET_NAME)
        )
        writer.write(b"CONNECT other.example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        context = ssl.create_default_context(cafile=str(authority.ca_file))
        await writer.start_tls(context, server_hostname="other.example.com")
        writer.write(
            b"GET / HTTP/1.1\r\nHost: other.example.com\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), 5)
        forwarded = await asyncio.wait_for(upstream_seen, 5)
        writer.close()
    finally:
        await proxy.stop()
        server.close()
        await server.wait_closed()
        authority.close()

    assert b"HTTP/1.1 200 OK" in response
    assert b"Authorization:" not in forwarded
    assert b"Bearer " not in forwarded


@real_bwrap
@pytest.mark.asyncio
async def test_jail_real_usa_placeholder_e_proxy_injeta_segredo_no_destino(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend.sandbox.tls_interception import WorkspaceTLSAuthority
    from backend.vtypes import Workspace
    from backend.workspace import workspace as ws_mod

    workspace = Workspace(
        id="ws-credential",
        name="ws-credential",
        cwd=str(tmp_path),
        created_at="2024-01-01T00:00:00+00:00",
        trusted=True,
        trusted_by="owner",
    )
    monkeypatch.setattr(
        ws_mod.workspace_registry,
        "get",
        lambda workspace_id: workspace if workspace_id == "ws-credential" else None,
    )
    authority = WorkspaceTLSAuthority(tmp_path)
    forwarded = asyncio.get_running_loop().create_future()

    async def upstream(reader, writer) -> None:
        forwarded.set_result(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(
        upstream,
        "127.0.0.1",
        0,
        ssl=authority.server_context("api.example.com"),
    )
    upstream_port = server.sockets[0].getsockname()[1]
    original_open_connection = asyncio.open_connection

    async def connect_upstream(host=None, port=None, *args, **kwargs):
        if host == "127.0.0.1" and port == 443:
            port = upstream_port
        return await original_open_connection(host, port, *args, **kwargs)

    async def allow_local(url, policy=None):
        return True, "", "127.0.0.1"

    class Secrets:
        async def get(self, user_id, key) -> str:
            assert (user_id, key) == (
                "owner",
                "sandbox:ws-credential:API_TOKEN",
            )
            return "server-only-secret"

    monkeypatch.setattr(asyncio, "open_connection", connect_upstream)
    monkeypatch.setattr(egress_proxy, "async_is_url_allowed", allow_local)
    monkeypatch.setattr(ssl, "create_default_context", ssl._create_unverified_context)
    monkeypatch.setattr("backend.secrets.get_secrets_provider", Secrets)
    policy = SandboxPolicy(
        enabled=True,
        allow_hosts=("api.example.com",),
        credential_bindings=(
            CredentialBinding(
                "API_TOKEN", "api.example.com", "Authorization", "Bearer "
            ),
        ),
    )
    manager = workspace_jail.WorkspaceJailManager()
    try:
        worker = await manager.get_or_spawn("ws-credential", str(tmp_path), policy)
        command = f"{sys.executable} -c " + shlex.quote(
            "import os,urllib.request; "
            "print(os.environ['API_TOKEN']); "
            "print(urllib.request.urlopen('https://api.example.com/', timeout=8).read().decode())"
        )
        result = await _exec(worker, command)
        assert result["exit_code"] == 0, result
        received = await asyncio.wait_for(forwarded, 10)
        assert not _process_memory_contains(worker.proc.pid, b"server-only-secret")
        assert all(
            b"server-only-secret" not in path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    finally:
        await manager.close("ws-credential")
        server.close()
        await server.wait_closed()
        authority.close()

    assert result["exit_code"] == 0
    assert result["stdout"].splitlines() == [
        "__VECTORA_CREDENTIAL_API_TOKEN__",
        "ok",
    ]
    assert b"Authorization: Bearer server-only-secret\r\n" in received
    assert "server-only-secret" not in result["stdout"]


@pytest.mark.asyncio
async def test_decisoes_chegam_ao_callback_de_auditoria() -> None:
    vistas = []
    proxy = EgressProxy(SandboxPolicy(enabled=True), on_decision=vistas.append)
    directory = await proxy.start()
    try:
        await _pedir(directory, b"CONNECT evil.example.org:443 HTTP/1.1\r\n\r\n")
    finally:
        await proxy.stop()

    assert [d.host for d in vistas] == ["evil.example.org"]


@pytest.mark.asyncio
async def test_pasta_do_socket_e_privada_e_some_ao_parar() -> None:
    proxy = EgressProxy(SandboxPolicy(enabled=True))
    directory = await proxy.start()

    assert oct(Path(directory).stat().st_mode & 0o777) == "0o700"
    assert oct((Path(directory) / SOCKET_NAME).stat().st_mode & 0o777) == "0o600"
    await proxy.stop()
    assert not Path(directory).exists()


@pytest.mark.asyncio
async def test_registry_reusa_o_proxy_e_recria_quando_a_rede_muda() -> None:
    registry = EgressRegistry()
    politica = SandboxPolicy(enabled=True)
    try:
        a = await registry.ensure("ws", politica)
        b = await registry.ensure("ws", politica)
        c = await registry.ensure(
            "ws", SandboxPolicy(enabled=True, allow_hosts=("x.com",))
        )
    finally:
        await registry.close_all()

    assert a is b
    assert c is not a
    assert registry.get("ws") is None


@pytest.mark.asyncio
async def test_egress_dir_for_so_para_jail_local_com_rede_filtrada() -> None:
    try:
        assert await egress_dir_for("ws-a", SandboxPolicy(enabled=False)) is None
        assert (
            await egress_dir_for("ws-a", SandboxPolicy(enabled=True, backend="docker"))
            is None
        )
        assert (
            await egress_dir_for("ws-a", SandboxPolicy(enabled=True, network="open"))
            is None
        )
        assert (
            await egress_dir_for("ws-a", SandboxPolicy(enabled=True, lockdown=True))
            is None
        )
        assert await egress_dir_for("ws-a", SandboxPolicy(enabled=True))
    finally:
        await egress_proxy.egress_registry.close_all()


def test_authority_de_connect() -> None:
    assert _split_authority("pypi.org:443") == ("pypi.org", 443)
    assert _split_authority("[::1]:8443") == ("::1", 8443)
    assert _split_authority("sem-porta") == (None, None)
    assert _split_authority("a:0") == (None, None)
    assert _split_authority("a:99999") == (None, None)


# ── ponte ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ponte_liga_o_loopback_ao_socket_unix(tmp_path: Path) -> None:
    async def eco(reader, writer) -> None:
        writer.write(b"unix:" + await reader.read(100))
        await writer.drain()
        writer.close()

    caminho = str(tmp_path / "s.sock")
    servidor = await asyncio.start_unix_server(eco, path=caminho)
    with socket.socket() as livre:
        livre.bind(("127.0.0.1", 0))
        porta = livre.getsockname()[1]
    pronta = asyncio.Event()
    tarefa = asyncio.create_task(
        egress_bridge.serve(caminho, porta, on_ready=pronta.set)
    )
    try:
        await asyncio.wait_for(pronta.wait(), 5)
        reader, writer = await asyncio.open_connection("127.0.0.1", porta)
        writer.write(b"oi")
        await writer.drain()
        dados = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa
        servidor.close()

    assert dados == b"unix:oi"


# ── jail real ───────────────────────────────────────────────────────────────

_TUNEL = (
    "import socket;"
    "s=socket.create_connection(('127.0.0.1',{porta}),timeout=10);"
    "s.sendall(b'CONNECT {destino} HTTP/1.1\\r\\n\\r\\n');"
    "print(s.recv(4096).decode('latin-1').splitlines()[0]);"
    "{depois}"
)


@real_bwrap
@pytest.mark.asyncio
async def test_jail_real_so_alcanca_a_rede_pelo_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def eco(reader, writer) -> None:
        writer.write(b"eco:" + await reader.read(100))
        await writer.drain()
        writer.close()

    servidor = await asyncio.start_server(eco, "127.0.0.1", 0)
    porta_eco = servidor.sockets[0].getsockname()[1]
    original = egress_proxy.async_is_url_allowed

    async def decide(url, policy=None):
        # Só um nome de teste vira o eco local; o resto segue a regra real.
        if "liberado.test" in url:
            return True, "", "127.0.0.1"
        return await original(url, policy)

    monkeypatch.setattr(egress_proxy, "async_is_url_allowed", decide)
    manager = workspace_jail.WorkspaceJailManager()
    politica = SandboxPolicy(enabled=True, allow_hosts=("liberado.test",))
    try:
        worker = await manager.get_or_spawn("ws", str(tmp_path), politica)
        bloqueado = await _exec(
            worker,
            f'{sys.executable} -c "'
            + _TUNEL.format(
                porta=JAIL_PROXY_PORT,
                destino="evil.example.org:443",
                depois="",
            )
            + '"',
        )
        direto = await _exec(
            worker,
            f'{sys.executable} -c "import socket;'
            "socket.create_connection(('1.1.1.1',443),timeout=3)\"",
        )
        tunel = await _exec(
            worker,
            f'{sys.executable} -c "'
            + _TUNEL.format(
                porta=JAIL_PROXY_PORT,
                destino=f"liberado.test:{porta_eco}",
                depois="s.sendall(b'oi');print(s.recv(100).decode())",
            )
            + '"',
        )
    finally:
        await manager.close("ws")
        await egress_proxy.egress_registry.close_all()
        servidor.close()

    assert bloqueado["stdout"].startswith("HTTP/1.1 403")
    assert (
        direto["exit_code"] != 0
    )  # sem rota: não existe caminho que não passe pelo proxy
    assert tunel["stdout"].splitlines() == [
        "HTTP/1.1 200 Connection Established",
        "eco:oi",
    ]
