"""Persistência do registro de extensões e da trilha de auditoria.

O registro é um arquivo JSON em ``<vectora_home>/extensions``, gravado de forma
atômica. A trilha de auditoria é um arquivo de linhas JSON só de acréscimo.
Nenhum segredo entra em nenhum dos dois.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from backend.extensions.models import ExtensionRecord

if TYPE_CHECKING:
    from backend.vtypes.contracts import JsonObject

_MAX_AUDIT_READ = 500


class ExtensionStore:
    """Lê e grava o registro de extensões de um diretório."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._registry = root / "registry.json"
        self._audit = root / "audit.jsonl"

    @property
    def root(self) -> Path:
        return self._root

    def load(self) -> dict[str, ExtensionRecord]:
        if not self._registry.exists():
            return {}
        try:
            raw = json.loads(self._registry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        records: dict[str, ExtensionRecord] = {}
        for item in raw.get("extensions", []):
            try:
                record = ExtensionRecord.model_validate(item)
            except ValueError:
                continue
            records[record.id] = record
        return records

    def save(self, records: dict[str, ExtensionRecord]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "extensions": [
                record.model_dump(mode="json") for record in records.values()
            ],
        }
        descriptor, tmp_name = tempfile.mkstemp(dir=self._root, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            Path(tmp_name).replace(self._registry)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def record_audit(
        self, *, actor: str, action: str, extension_id: str, detail: str = ""
    ) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "actor": actor,
            "action": action,
            "extension_id": extension_id,
            "detail": detail[:500],
        }
        with self._audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_audit(
        self, extension_id: str | None = None, limit: int = 100
    ) -> list[JsonObject]:
        if not self._audit.exists():
            return []
        entries: list[JsonObject] = []
        for line in self._audit.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if extension_id is None or entry.get("extension_id") == extension_id:
                entries.append(entry)
        return entries[-min(limit, _MAX_AUDIT_READ) :]
