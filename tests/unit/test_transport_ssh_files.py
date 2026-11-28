from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from backend.transport.base import RunResult
from backend.transport.ssh import SshTransport


@pytest.fixture
def transport() -> SshTransport:
    return SshTransport(remote_host="user@example", ssh_key_id=None, user_id="user")


async def test_ssh_read_surfaces_missing_file(
    transport: SshTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        transport,
        "run",
        AsyncMock(return_value=RunResult(1, "", "No such file or directory")),
    )

    with pytest.raises(FileNotFoundError):
        await transport.read_file("/workspaces/project/missing.txt")


async def test_ssh_read_preserves_binary_bytes(
    transport: SshTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"\x00\xffbinary"
    monkeypatch.setattr(
        transport,
        "run",
        AsyncMock(
            return_value=RunResult(0, base64.b64encode(content).decode("ascii"), "")
        ),
    )

    assert await transport.read_file("/workspaces/project/image.bin") == content


@pytest.mark.parametrize("operation", ["list_dir", "read_file", "write_file"])
async def test_ssh_filesystem_operations_surface_failure(
    transport: SshTransport, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(
        transport,
        "run",
        AsyncMock(return_value=RunResult(255, "", "connection lost")),
    )
    call = (
        transport.list_dir("/workspaces/project")
        if operation == "list_dir"
        else transport.read_file("/workspaces/project/a.txt")
        if operation == "read_file"
        else transport.write_file("/workspaces/project/a.txt", b"content")
    )

    with pytest.raises(RuntimeError, match="connection lost"):
        await call
