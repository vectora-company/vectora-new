"""Valores lidos da configuração do sandbox."""

from __future__ import annotations

from datetime import date, datetime, time

type TomlValue = (
    str
    | int
    | float
    | bool
    | date
    | time
    | datetime
    | list[TomlValue]
    | dict[str, TomlValue]
)
type TomlTable = dict[str, TomlValue]
