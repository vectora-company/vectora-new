"""Requests mínimos para chamar handlers sem montar um servidor."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from starlette.requests import Request


def fake_request(*, state: SimpleNamespace) -> MagicMock:
    """Mock com a especificação de ``Request`` e o ``state`` informado."""
    request = MagicMock(spec=Request)
    request.state = state
    return request
