"""Valores e linhas lidos de um banco SQLite."""

from __future__ import annotations

from typing import overload

type SqlValue = str | int | float | bytes | None
type SqlRow = dict[str, SqlValue]


@overload
def optional_text(value: None) -> None: ...


@overload
def optional_text(value: str | float | bytes) -> str: ...


@overload
def optional_text(value: SqlValue) -> str | None: ...


def optional_text(value: SqlValue) -> str | None:
    """Texto de uma coluna anulável."""
    return None if value is None else str(value)
