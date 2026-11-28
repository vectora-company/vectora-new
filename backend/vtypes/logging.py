"""Valores aceitos como contexto estruturado nos logs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path

type LogValue = (
    str
    | int
    | float
    | bool
    | Path
    | BaseException
    | Sequence[LogValue]
    | AbstractSet[LogValue]
    | Mapping[str, LogValue]
    | None
)
