"""SshTransport — workspace remoto via SSH (asyncssh).

Connection pool por workspace.id; uma conexão persiste enquanto o
workspace estiver ativo. Chaves SSH são lidas do vault KeePassXC
(:mod:`src.services.secrets.keepass`), nunca do disco direto.
"""

from __future__ import annotations

import logging
import posixpath
from typing import TYPE_CHECKING

from backend.transport.base import DirEntry, RunResult

if TYPE_CHECKING:
    import asyncssh

logger = logging.getLogger(__name__)


def _parse_host(remote_host: str) -> tuple[str, str, int]:
    """Quebra ``user@host[:port]`` em ``(user, host, port)``.

    Default user = "root", port = 22 quando ausentes.
    """
    if "@" in remote_host:
        user, hostpart = remote_host.split("@", 1)
    else:
        user, hostpart = "root", remote_host
    if ":" in hostpart:
        host, port_str = hostpart.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            port = 22
    else:
        host, port = hostpart, 22
    return user, host, port


class SshTransport:
    """Transport via SSH com pool de 1 conexão por workspace.

    Implementa o Protocol :class:`TransportBackend`. Lazy connect:
    abre a conexão na primeira operação e a mantém até :meth:`close`.

    Para o terminal interativo (PTY) e operações de filesystem além de
    read/write, usar ``run`` com comandos shell.
    """

    def __init__(
        self,
        *,
        remote_host: str,
        ssh_key_id: str | None,
        user_id: str | None,
        passphrase: str | None = None,
    ) -> None:
        self._remote_host = remote_host
        self._ssh_key_id = ssh_key_id
        self._user_id = user_id
        self._passphrase = passphrase
        self._conn: asyncssh.SSHClientConnection | None = None

    async def _ensure_connection(self) -> asyncssh.SSHClientConnection:
        """Abre conexão se ainda não existe (idempotente)."""
        if self._conn is not None:
            return self._conn

        # Import tardio — asyncssh é dep opcional e relativamente pesado;
        # adiamos o custo até o primeiro uso de SSH.
        import asyncssh  # type: ignore[import-not-found]

        user, host, port = _parse_host(self._remote_host)

        client_keys: list[asyncssh.SSHKey] = []
        if self._ssh_key_id:
            key_bytes = await self._load_key_bytes()
            if key_bytes is not None:
                try:
                    private_key = asyncssh.import_private_key(
                        key_bytes, passphrase=self._passphrase
                    )
                    client_keys = [private_key]
                except Exception:
                    logger.exception(
                        "ssh: falha ao importar chave do vault (key_id=%s)",
                        self._ssh_key_id,
                    )

        try:
            self._conn = await asyncssh.connect(
                host=host,
                port=port,
                username=user,
                client_keys=client_keys or None,
                known_hosts=None,  # TLOFU; acrescentar TOFU em fase futura
                connect_timeout=10,
            )
        except Exception as exc:
            logger.exception("ssh: falha ao conectar %s@%s:%s", user, host, port)
            raise RuntimeError(f"SSH connect falhou: {exc}") from exc
        return self._conn

    async def _load_key_bytes(self) -> bytes | None:
        """Tenta carregar a chave SSH do vault KeePass do usuário."""
        if not self._ssh_key_id or not self._user_id:
            return None
        try:
            from backend.secrets.ssh_keys import get_ssh_key_bytes

            return await get_ssh_key_bytes(self._user_id, self._ssh_key_id)
        except Exception:
            logger.exception(
                "ssh: vault indisponível ou key_id ausente (%s)", self._ssh_key_id
            )
            return None

    # ─── Protocol ─────────────────────────────────────────────────────

    async def list_dir(self, path: str) -> list[DirEntry]:
        import json

        script = (
            "import json,os,sys; out=[]; "
            "\nfor e in os.scandir(sys.argv[1]):\n"
            " try:\n  d=e.is_dir(follow_symlinks=False); "
            "s=None if d else e.stat(follow_symlinks=False).st_size; "
            " out.append({'name':e.name,'is_dir':d,'size':s})\n"
            " except OSError: pass\n"
            "print(json.dumps(out))"
        )

        result = await self.run(
            ["python3", "-c", script, path],
            cwd=".",
            timeout=15.0,
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or "Falha ao listar diretório SSH.")
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Resposta inválida ao listar diretório SSH.") from exc
        return [
            DirEntry(
                name=item["name"],
                path=f"{path.rstrip('/')}/{item['name']}",
                is_dir=item["is_dir"],
                size=item["size"],
            )
            for item in values
            if isinstance(item, dict) and item.get("name") not in {".", ".."}
        ]

    async def read_file(self, path: str, max_bytes: int = 1_048_576) -> bytes:
        import base64

        script = (
            "import base64,sys; "
            "sys.stdout.write(base64.b64encode(open(sys.argv[1],'rb').read(int(sys.argv[2]))).decode())"
        )

        result = await self.run(
            ["python3", "-c", script, path, str(max_bytes)],
            cwd=".",
            timeout=30.0,
        )
        if result.exit_code != 0:
            if "no such file or directory" in result.stderr.lower():
                raise FileNotFoundError(path)
            raise RuntimeError(result.stderr or "Falha ao ler arquivo SSH.")
        try:
            return base64.b64decode(result.stdout, validate=True)
        except ValueError as exc:
            raise RuntimeError("Resposta inválida ao ler arquivo SSH.") from exc

    async def write_file(self, path: str, data: bytes) -> None:
        # Para arquivos pequenos, base64 + decode no destino é seguro
        # contra caracteres especiais e quebras de linha.
        import base64
        import shlex

        b64 = base64.b64encode(data).decode("ascii")
        parent = shlex.quote(posixpath.dirname(path) or ".")
        result = await self.run(
            [
                "sh",
                "-c",
                f"mkdir -p -- {parent} && echo '{b64}' | base64 -d > {shlex.quote(path)}",
            ],
            cwd=".",
            timeout=30.0,
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or "Falha ao gravar arquivo SSH.")

    async def run(
        self,
        cmd: list[str],
        cwd: str,
        timeout: float = 30.0,  # noqa: ASYNC109
    ) -> RunResult:
        conn = await self._ensure_connection()
        # asyncssh aceita lista (sem shell) ou string (com shell).
        # Para consistência com transport.local que usa lista, juntamos
        # de forma segura via shlex.quote.
        import shlex

        joined = " ".join(shlex.quote(part) for part in cmd)
        full_cmd = (
            f"cd {shlex.quote(cwd)} 2>/dev/null && {joined}"
            if cwd not in {"", "."}
            else joined
        )
        try:
            proc = await conn.run(full_cmd, timeout=timeout, check=False)
        except Exception as exc:
            logger.exception("ssh.run falhou: %s", joined[:80])
            return RunResult(exit_code=-1, stdout="", stderr=f"SSH run error: {exc}")
        return RunResult(
            exit_code=int(getattr(proc, "exit_status", 0) or 0),
            stdout=str(getattr(proc, "stdout", "") or ""),
            stderr=str(getattr(proc, "stderr", "") or ""),
        )

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
                await self._conn.wait_closed()
            except Exception:
                logger.debug("ssh: erro ao fechar conexão", exc_info=True)
            self._conn = None
