"""Descrição segura de DSNs de banco para log e terminal."""

from __future__ import annotations

from urllib.parse import urlsplit

_UNKNOWN = "<dsn não reconhecido>"


def describe_dsn(dsn: str) -> str:
    """Devolve ``host:porta/banco``, sem usuário, senha ou parâmetros.

    Uma senha com ``/`` ou ``#`` sem codificação desloca o ``@`` para fora do
    endereço; nesse caso nada do texto original é devolvido.
    """
    try:
        parts = urlsplit(dsn)
        port = parts.port
    except ValueError:
        return _UNKNOWN
    if not parts.hostname or "@" in parts.path + parts.query + parts.fragment:
        return _UNKNOWN
    address = parts.hostname if port is None else f"{parts.hostname}:{port}"
    return f"{address}{parts.path}"
