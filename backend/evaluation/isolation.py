"""Guardas determinísticas para sessões e efeitos de cenários de avaliação."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

RunTerminalStatus = Literal["passed", "failed", "blocked", "cancelled"]
EffectStatus = Literal["prepared", "applied", "failed", "uncertain"]


@dataclass
class ScenarioPolicy:
    """Snapshot mutável de permissões consultado em cada operação."""

    grants: set[str] = field(default_factory=set)
    revision: int = 1
    revoked: set[str] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def authorize(self, capability: str) -> bool:
        """Revalida o grant atual para uma ação protegida."""
        with self._lock:
            return capability in self.grants and capability not in self.revoked

    def revoke(self, capability: str) -> None:
        """Revoga uma capability e avança a revisão de policy."""
        with self._lock:
            self.revoked.add(capability)
            self.revision += 1

    def run_if_authorized(
        self, capability: str, action: Callable[[], bool]
    ) -> tuple[bool, bool]:
        """Serializa uma ação já preparada com revogações concorrentes.

        O lock permanece adquirido durante a reserva e o dispatch: quando
        ``revoke`` retorna, nenhum dispatch autorizado por uma revisão anterior
        ainda está em andamento.
        """
        with self._lock:
            if capability not in self.grants or capability in self.revoked:
                return False, False
            return True, action()


class ScenarioIsolationGuard:
    """Vincula uma sessão ao workspace que existia no início da execução."""

    def __init__(
        self,
        run_id: str,
        workspace: Path | str,
        policy: ScenarioPolicy,
    ) -> None:
        if not run_id:
            raise ValueError("run_id obrigatório")
        self.run_id = run_id
        self.workspace = Path(workspace).resolve()
        self.policy = policy

    def resolve_workspace_path(self, relative_path: str | Path) -> Path:
        """Resolve dentro do workspace capturado e rejeita escapes/symlinks."""
        target = (self.workspace / relative_path).resolve()
        if not target.is_relative_to(self.workspace):
            raise PermissionError("caminho fora do workspace capturado")
        return target

    def authorize_effect(self, capability: str, relative_path: str | Path) -> Path:
        """Confere caminho e policy no momento em que o efeito será executado."""
        target = self.resolve_workspace_path(relative_path)
        if not self.policy.authorize(capability):
            raise PermissionError(f"policy não autoriza {capability}")
        return target


class EffectJournal:
    """Journal SQLite que bloqueia replay ambíguo de efeitos externos."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        """Abre o journal; um caminho em disco preserva estados entre processos."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path), timeout=5, check_same_thread=False
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS evaluation_effect_journal (
                run_id TEXT NOT NULL,
                effect_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('prepared', 'applied', 'failed', 'uncertain')
                ),
                PRIMARY KEY (run_id, effect_id)
            )"""
        )
        self._connection.commit()

    def prepare(self, run_id: str, effect_id: str) -> bool:
        """Reserva um efeito uma única vez antes do dispatch."""
        if not run_id or not effect_id:
            raise ValueError("run_id e effect_id são obrigatórios")
        with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO evaluation_effect_journal "
                "(run_id, effect_id, status) VALUES (?, ?, 'prepared')",
                (run_id, effect_id),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def finish(self, run_id: str, effect_id: str, *, applied: bool) -> None:
        """Registra o desfecho de um efeito preparado."""
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE evaluation_effect_journal SET status = ? "
                "WHERE run_id = ? AND effect_id = ? AND status = 'prepared'",
                ("applied" if applied else "failed", run_id, effect_id),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                raise ValueError("efeito não está preparado")

    def mark_uncertain(self, run_id: str, effect_id: str) -> None:
        """Marca dispatch interrompido como ambíguo; não será repetido."""
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE evaluation_effect_journal SET status = 'uncertain' "
                "WHERE run_id = ? AND effect_id = ? AND status = 'prepared'",
                (run_id, effect_id),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                raise ValueError("efeito não está preparado")

    def status(self, run_id: str, effect_id: str) -> EffectStatus | None:
        """Retorna o estado isolado do efeito daquela execução."""
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM evaluation_effect_journal "
                "WHERE run_id = ? AND effect_id = ?",
                (run_id, effect_id),
            ).fetchone()
            return row[0] if row is not None else None

    def mark_run_cancelled(self, run_id: str) -> None:
        """Marca efeitos em voo como incertos para bloquear retomada duplicada."""
        with self._lock:
            self._connection.execute(
                "UPDATE evaluation_effect_journal SET status = 'uncertain' "
                "WHERE run_id = ? AND status = 'prepared'",
                (run_id,),
            )
            self._connection.commit()

    def close(self) -> None:
        """Fecha a conexão para permitir retomada em outro executor."""
        with self._lock:
            self._connection.close()


@dataclass(frozen=True)
class ScenarioTerminalResult:
    """Estado terminal e diagnóstico persistíveis da execução de cenário."""

    run_id: str
    status: RunTerminalStatus
    diagnostic: str

    def save(self, target_path: Path | str) -> None:
        """Persiste o diagnóstico terminal por substituição atômica local."""
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(self.__dict__, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def sidecar_failure_result(
    run_id: str, sidecar: str, detail: str
) -> ScenarioTerminalResult:
    """Converte falha parcial de dependência em resultado explícito."""
    if not sidecar.strip() or not detail.strip():
        raise ValueError("sidecar e diagnóstico são obrigatórios")
    return ScenarioTerminalResult(
        run_id=run_id,
        status="blocked",
        diagnostic=f"sidecar {sidecar} indisponível: {detail}",
    )


def run_guarded_effect(
    guard: ScenarioIsolationGuard,
    journal: EffectJournal,
    *,
    effect_id: str,
    capability: str,
    relative_path: str | Path,
    dispatch: Callable[[Path], None],
) -> bool:
    """Despacha uma única ação após validar isolamento, policy e idempotência."""

    def dispatch_authorized_effect() -> bool:
        target = guard.authorize_effect(capability, relative_path)
        if not journal.prepare(guard.run_id, effect_id):
            return False
        try:
            dispatch(target)
        except KeyboardInterrupt:
            journal.mark_uncertain(guard.run_id, effect_id)
            raise
        except Exception:
            journal.finish(guard.run_id, effect_id, applied=False)
            raise
        journal.finish(guard.run_id, effect_id, applied=True)
        return True

    authorized, dispatched = guard.policy.run_if_authorized(
        capability, dispatch_authorized_effect
    )
    if not authorized:
        raise PermissionError(f"policy não autoriza {capability}")
    return dispatched


def cancel_guarded_run(journal: EffectJournal, run_id: str) -> ScenarioTerminalResult:
    """Marca efeitos pendentes como incertos e retorna status terminal."""
    journal.mark_run_cancelled(run_id)
    return ScenarioTerminalResult(
        run_id=run_id,
        status="cancelled",
        diagnostic="execução cancelada; efeitos pendentes não serão repetidos",
    )
