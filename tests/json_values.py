"""Leitura conferida de valores JSON em testes."""

from __future__ import annotations

from backend.vtypes.contracts import JsonObject, JsonValue


def as_object(value: JsonValue) -> JsonObject:
    """O valor como objeto JSON; falha o teste se for outro formato."""
    if not isinstance(value, dict):
        raise AssertionError(value)
    return value


def as_list(value: JsonValue) -> list[JsonValue]:
    """O valor como lista JSON; falha o teste se for outro formato."""
    if not isinstance(value, list):
        raise AssertionError(value)
    return value


def as_str(value: JsonValue) -> str:
    """O valor como texto; falha o teste se for outro formato."""
    if not isinstance(value, str):
        raise AssertionError(value)
    return value


def dig(value: JsonValue, *path: str | int) -> JsonValue:
    """Segue chaves e índices; falha o teste se o formato não corresponder."""
    for key in path:
        value = as_object(value)[key] if isinstance(key, str) else as_list(value)[key]
    return value
