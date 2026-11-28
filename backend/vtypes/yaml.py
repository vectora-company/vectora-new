"""Valores produzidos pelo leitor de YAML."""

from __future__ import annotations

from datetime import date, datetime

type YamlKey = str | int | float | bool | date | datetime | bytes | None
type YamlValue = YamlKey | list[YamlValue] | dict[YamlKey, YamlValue]
