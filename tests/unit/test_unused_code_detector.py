"""Regressões do detector de símbolos Python sem uso."""

from __future__ import annotations

from pathlib import Path

import pytest

from utils.unused_code import VULTURE_IGNORE_DECORATORS, VULTURE_IGNORE_NAMES


def test_protocol_override_is_ignored_without_hiding_same_named_method(
    tmp_path: Path,
) -> None:
    vulture = pytest.importorskip("vulture")
    source = tmp_path / "protocols.py"
    source.write_text(
        """from typing import override

class Base:
    def data_received(self, data: bytes) -> None:
        pass

class Adapter(Base):
    @override
    def data_received(self, data: bytes) -> None:
        pass

class Unrelated:
    def data_received(self, data: bytes) -> None:
        pass
""",
        encoding="utf-8",
    )

    detector = vulture.Vulture(
        ignore_names=VULTURE_IGNORE_NAMES.split(","),
        ignore_decorators=VULTURE_IGNORE_DECORATORS.split(","),
    )
    detector.scavenge([str(source)])
    unused = {
        (item.typ, item.name) for item in detector.get_unused_code(min_confidence=60)
    }

    assert ("method", "data_received") in unused


def test_ipc_pipe_protocol_callbacks_are_explicit_overrides() -> None:
    from backend.services import ipc_pipe_win

    assert "connection_made" not in VULTURE_IGNORE_NAMES
    assert "data_received" not in VULTURE_IGNORE_NAMES
    assert "connection_lost" not in VULTURE_IGNORE_NAMES
    assert getattr(ipc_pipe_win._PipeSide.connection_made, "__override__", False)
    assert getattr(ipc_pipe_win._PipeSide.data_received, "__override__", False)
    assert getattr(ipc_pipe_win._PipeSide.connection_lost, "__override__", False)
