"""Ciclo de vida, supervisão e broker de tools das extensões.

Cada extensão ativa é um servidor MCP ``stdio`` dentro do jail do Vectora, com
ambiente mínimo e rede só para os destinos concedidos. O host é a autoridade:
declarações da extensão não ampliam permissões, e uma chamada de tool leva só o
nome da tool e os argumentos explícitos, nunca transcript, memória, contexto ou
credenciais.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, Field, field_validator

from backend.extensions.models import (
    HOST_API_VERSION,
    Contribution,
    ExtensionRecord,
    ExtensionRelease,
    ExtensionState,
    Grants,
    _validate_extension_id,
    effective_contribution_id,
)
from backend.extensions.store import ExtensionStore

if TYPE_CHECKING:
    from backend.tools.context import ToolContext
    from backend.vtypes.contracts import JsonObject
    from backend.vtypes.extensions import ExtensionClient, ExtensionHealth

logger = logging.getLogger(__name__)

_MAX_BUNDLE_FILES: Final = 5000
_MAX_BUNDLE_BYTES: Final = 200 * 1024 * 1024
_MAX_ARGUMENT_BYTES: Final = 256 * 1024
_STARTUP_TIMEOUT_S: Final = 30.0
_CALL_TIMEOUT_S: Final = 60.0
_MAX_UI_FILE_BYTES: Final = 5 * 1024 * 1024


class ExtensionError(Exception):
    """Erro de domínio do host, com um código estável para a API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InstallRequest(BaseModel):
    """O que o host precisa para instalar uma versão já verificada pelo Vext."""

    id: str
    name: str = Field(min_length=1, max_length=120)
    publisher: str = Field(min_length=1, max_length=120)
    release: ExtensionRelease

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_extension_id(value)


class GrantsUpdate(BaseModel):
    tools: list[str] = Field(default_factory=list)
    allow_hosts: list[str] = Field(default_factory=list)
    workspaces: list[str] = Field(default_factory=list)


def compute_bundle_digest(path: Path) -> str:
    """SHA-256 determinístico do conteúdo de um pacote extraído.

    Recusa links simbólicos e pacotes acima dos limites, para que o digest
    cubra exatamente os bytes que serão executados.
    """
    root = path.resolve()
    if not root.is_dir():
        raise ExtensionError("invalid", "O pacote da extensão não é um diretório.")
    digest = hashlib.sha256()
    count = 0
    total = 0
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            raise ExtensionError(
                "invalid", "O pacote não pode conter links simbólicos."
            )
        if not file.is_file():
            continue
        count += 1
        size = file.stat().st_size
        total += size
        if count > _MAX_BUNDLE_FILES or total > _MAX_BUNDLE_BYTES:
            raise ExtensionError("invalid", "O pacote excede os limites do host.")
        relative = file.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


class ExtensionHost:
    """Registro e supervisão das extensões instaladas."""

    def __init__(
        self,
        store: ExtensionStore,
        *,
        client_factory: Callable[[], ExtensionClient] | None = None,
        startup_timeout: float = _STARTUP_TIMEOUT_S,
        call_timeout: float = _CALL_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._client_factory = client_factory or _default_client
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._records = store.load()
        self._clients: dict[str, ExtensionClient] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------------

    @property
    def bundles_root(self) -> Path:
        return self._store.root / "bundles"

    def installed(self) -> list[ExtensionRecord]:
        return sorted(self._records.values(), key=lambda record: record.id)

    def get(self, extension_id: str) -> ExtensionRecord:
        record = self._records.get(extension_id)
        if record is None:
            raise ExtensionError("not_found", "Extensão não encontrada.")
        return record

    def audit(self, extension_id: str | None = None) -> list[JsonObject]:
        return self._store.read_audit(extension_id)

    def contributions(self) -> list[JsonObject]:
        """Contribuições das extensões ativas, com id efetivo sem colisão."""
        return [
            {
                "id": effective_contribution_id(record.id, contribution),
                "extension_id": record.id,
                **contribution.model_dump(mode="json", exclude={"id"}),
            }
            for record in self.installed()
            if record.state is ExtensionState.ACTIVE
            for contribution in record.current.contributions
        ]

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def install(self, request: InstallRequest, *, actor: str) -> ExtensionRecord:
        release = request.release
        self._validate_release(release)
        async with self._lock:
            existing = self._records.get(request.id)
            if (
                existing is not None
                and existing.state is ExtensionState.REVOKED
                and existing.current.digest == release.digest
            ):
                raise ExtensionError(
                    "revoked", "Esta versão foi revogada e não pode ser instalada."
                )
            if existing is not None:
                await self._stop(existing.id)
            record = ExtensionRecord(
                id=request.id,
                name=request.name,
                publisher=request.publisher,
                current=release,
                previous=existing.current if existing is not None else None,
                grants=existing.grants if existing is not None else Grants(),
            )
            self._records[record.id] = record
            self._persist()
            self._store.record_audit(
                actor=actor,
                action="install",
                extension_id=record.id,
                detail=f"{release.version} {release.digest[:12]}",
            )
            return record

    async def activate(self, extension_id: str, *, actor: str) -> ExtensionRecord:
        async with self._lock:
            return await self._activate(extension_id, actor=actor)

    async def deactivate(self, extension_id: str, *, actor: str) -> ExtensionRecord:
        async with self._lock:
            record = self.get(extension_id)
            await self._stop(extension_id)
            if record.state is not ExtensionState.REVOKED:
                self._set_state(record, ExtensionState.INACTIVE)
            self._store.record_audit(
                actor=actor, action="deactivate", extension_id=extension_id
            )
            return record

    async def revoke(
        self, extension_id: str, *, actor: str, reason: str
    ) -> ExtensionRecord:
        async with self._lock:
            record = self.get(extension_id)
            await self._stop(extension_id)
            self._set_state(record, ExtensionState.REVOKED, reason or "revogada")
            self._store.record_audit(
                actor=actor, action="revoke", extension_id=extension_id, detail=reason
            )
            return record

    async def rollback(self, extension_id: str, *, actor: str) -> ExtensionRecord:
        async with self._lock:
            record = self.get(extension_id)
            if record.previous is None:
                raise ExtensionError("invalid", "Não há versão anterior para voltar.")
            await self._stop(extension_id)
            record.current, record.previous = record.previous, record.current
            self._set_state(record, ExtensionState.INACTIVE)
            self._store.record_audit(
                actor=actor,
                action="rollback",
                extension_id=extension_id,
                detail=record.current.version,
            )
            return record

    async def remove(self, extension_id: str, *, actor: str) -> None:
        async with self._lock:
            record = self.get(extension_id)
            await self._stop(extension_id)
            for release in (record.current, record.previous):
                if release is not None:
                    self._remove_bundle(release)
            del self._records[extension_id]
            self._persist()
            self._store.record_audit(
                actor=actor, action="remove", extension_id=extension_id
            )

    async def set_grants(
        self, extension_id: str, update: GrantsUpdate, *, actor: str
    ) -> ExtensionRecord:
        async with self._lock:
            record = self.get(extension_id)
            declared = set(record.current.declared_tools)
            unknown = sorted(set(update.tools) - declared)
            if unknown:
                raise ExtensionError(
                    "invalid", f"Tools não declaradas pela extensão: {unknown}"
                )
            try:
                grants = Grants.model_validate(update.model_dump())
            except ValueError as exc:
                raise ExtensionError("invalid", str(exc)) from exc
            hosts_changed = grants.allow_hosts != record.grants.allow_hosts
            record.grants = grants
            self._persist()
            self._store.record_audit(
                actor=actor,
                action="grants",
                extension_id=extension_id,
                detail=(
                    f"tools={len(grants.tools)} hosts={len(grants.allow_hosts)} "
                    f"workspaces={len(grants.workspaces)}"
                ),
            )
            if hosts_changed and record.state is ExtensionState.ACTIVE:
                await self._stop(extension_id)
                await self._activate(extension_id, actor=actor)
            return record

    async def restore(self) -> None:
        """Reativa, ao iniciar, as extensões que estavam ativas."""
        for record in self.installed():
            if record.state is not ExtensionState.ACTIVE:
                continue
            try:
                async with self._lock:
                    await self._activate(record.id, actor="host")
            except ExtensionError:
                logger.warning("extensão não reativada", extra={"extension": record.id})

    async def shutdown(self) -> None:
        async with self._lock:
            for extension_id in list(self._clients):
                await self._stop(extension_id)

    # ------------------------------------------------------------------
    # Supervisão e broker
    # ------------------------------------------------------------------

    def ui_file(self, extension_id: str, relative: str) -> Path:
        """Arquivo da pasta ``ui/`` de uma extensão ativa, sem sair dela."""
        record = self.get(extension_id)
        if record.state is not ExtensionState.ACTIVE:
            raise ExtensionError("not_active", "A extensão não está ativa.")
        root = (Path(record.current.install_path) / "ui").resolve()
        target = (root / relative).resolve()
        if root not in target.parents or target.is_symlink() or not target.is_file():
            raise ExtensionError("not_found", "Arquivo não encontrado.")
        if target.stat().st_size > _MAX_UI_FILE_BYTES:
            raise ExtensionError("invalid", "Arquivo acima do limite.")
        return target

    async def health(self, extension_id: str) -> ExtensionHealth:
        record = self.get(extension_id)
        client = self._clients.get(extension_id)
        result: ExtensionHealth = {
            "state": record.state.value,
            "running": client is not None,
            "tools": sorted(client.tools()) if client is not None else [],
        }
        return result

    async def call_tool(
        self,
        extension_id: str,
        tool_name: str,
        arguments: JsonObject,
        *,
        workspace_id: str,
        actor: str,
        ctx: ToolContext | None = None,
    ) -> str:
        """Despacha uma tool de extensão com os argumentos explícitos da chamada."""
        record = self.get(extension_id)
        if record.state is not ExtensionState.ACTIVE:
            raise ExtensionError("not_active", "A extensão não está ativa.")
        if tool_name not in record.grants.tools:
            raise ExtensionError("denied", "Esta tool não foi concedida à extensão.")
        if tool_name not in record.current.declared_tools:
            raise ExtensionError("denied", "Esta tool não foi declarada pela extensão.")
        if not workspace_id or workspace_id not in record.grants.workspaces:
            raise ExtensionError(
                "denied", "A extensão não tem acesso a este workspace."
            )
        try:
            payload = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ExtensionError("invalid", "Argumentos não serializáveis.") from exc
        if len(payload.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            raise ExtensionError("invalid", "Argumentos acima do limite.")
        client = self._clients.get(extension_id)
        if client is None or tool_name not in client.tools():
            raise ExtensionError("not_active", "A tool não está disponível agora.")
        try:
            async with asyncio.timeout(self._call_timeout):
                result = await client.call_tool(tool_name, arguments, ctx)
        except TimeoutError as exc:
            await self._fail(extension_id, "tempo esgotado numa chamada")
            raise ExtensionError(
                "failed", "A extensão excedeu o tempo limite."
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(extension_id, "erro numa chamada")
            raise ExtensionError("failed", "A extensão falhou na chamada.") from exc
        self._store.record_audit(
            actor=actor,
            action="call_tool",
            extension_id=extension_id,
            detail=f"{tool_name} workspace={workspace_id}",
        )
        return str(result)

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _validate_release(self, release: ExtensionRelease) -> None:
        if release.host_api != HOST_API_VERSION:
            raise ExtensionError(
                "incompatible",
                f"A extensão exige a API {release.host_api} do host; "
                f"esta versão oferece a {HOST_API_VERSION}.",
            )
        _check_command(release.command)
        self._contained_path(release)
        seen: set[tuple[str, str]] = set()
        for contribution in release.contributions:
            key = (contribution.kind, contribution.id)
            if key in seen:
                raise ExtensionError(
                    "conflict", f"Contribuição duplicada: {contribution.id}"
                )
            seen.add(key)
        if compute_bundle_digest(Path(release.install_path)) != release.digest:
            raise ExtensionError("integrity", "O digest do pacote não confere.")

    def _contained_path(self, release: ExtensionRelease) -> Path:
        path = Path(release.install_path).resolve()
        root = self.bundles_root.resolve()
        if path == root or root not in path.parents:
            raise ExtensionError(
                "invalid", "O pacote precisa estar no diretório de pacotes do host."
            )
        return path

    def _remove_bundle(self, release: ExtensionRelease) -> None:
        try:
            path = self._contained_path(release)
        except ExtensionError:
            return
        shutil.rmtree(path, ignore_errors=True)

    async def _activate(self, extension_id: str, *, actor: str) -> ExtensionRecord:
        record = self.get(extension_id)
        if record.state is ExtensionState.REVOKED:
            raise ExtensionError("revoked", "A extensão foi revogada.")
        release = record.current
        try:
            self._validate_release(release)
        except ExtensionError as exc:
            self._set_state(record, ExtensionState.FAILED, exc.message)
            self._store.record_audit(
                actor=actor,
                action="activate_refused",
                extension_id=extension_id,
                detail=exc.code,
            )
            raise
        await self._stop(extension_id)
        command = _resolve_command(release)
        connection: JsonObject = {
            "transport": "stdio",
            "command": command,
            "args": [*release.args],
            "require_sandbox": True,
            "allow_hosts": [*record.grants.allow_hosts],
            "env": {},
        }
        client = self._client_factory()
        try:
            async with asyncio.timeout(self._startup_timeout):
                await client.connect({extension_id: connection}, strict=True)
        except Exception as exc:
            await _close(client)
            self._set_state(record, ExtensionState.FAILED, "não foi possível iniciar")
            self._store.record_audit(
                actor=actor,
                action="activate_failed",
                extension_id=extension_id,
                detail=type(exc).__name__,
            )
            raise ExtensionError(
                "failed", "Não foi possível iniciar a extensão."
            ) from exc
        self._clients[extension_id] = client
        self._set_state(record, ExtensionState.ACTIVE)
        self._store.record_audit(
            actor=actor, action="activate", extension_id=extension_id
        )
        return record

    async def _stop(self, extension_id: str) -> None:
        client = self._clients.pop(extension_id, None)
        if client is not None:
            await _close(client)

    async def _fail(self, extension_id: str, reason: str) -> None:
        await self._stop(extension_id)
        record = self._records.get(extension_id)
        if record is not None:
            self._set_state(record, ExtensionState.FAILED, reason)

    def _set_state(
        self, record: ExtensionRecord, state: ExtensionState, reason: str = ""
    ) -> None:
        record.state = state
        record.state_reason = reason
        self._persist()

    def _persist(self) -> None:
        self._store.save(self._records)


def _check_command(command: str) -> None:
    """Comando é um nome simples do PATH ou um caminho relativo dentro do pacote."""
    windows = PureWindowsPath(command)
    posix = PurePosixPath(command)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise ExtensionError("invalid", "Comando da extensão inválido.")


def _resolve_command(release: ExtensionRelease) -> str:
    if "/" in release.command or "\\" in release.command:
        return str((Path(release.install_path) / release.command).resolve())
    return release.command


async def _close(client: ExtensionClient) -> None:
    try:
        await client.aclose()
    except Exception:
        logger.debug("extensão: erro ao encerrar o cliente (ignorado)")


def _default_client() -> ExtensionClient:
    from backend.tools.mcp import VectoraMCPClient

    return VectoraMCPClient()


_host: ExtensionHost | None = None


def get_extension_host() -> ExtensionHost:
    """Host único do processo, com o registro em ``<vectora_home>/extensions``."""
    global _host
    if _host is None:
        from backend.settings import settings

        _host = ExtensionHost(
            ExtensionStore(Path(settings.vectora_home) / "extensions")
        )
    return _host


__all__ = [
    "Contribution",
    "ExtensionError",
    "ExtensionHost",
    "GrantsUpdate",
    "InstallRequest",
    "compute_bundle_digest",
    "get_extension_host",
]
