"""Resolução confinada de caminhos para operações remotas de workspace."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.transport import TransportBackend
    from backend.workspace.workspace import Workspace


async def resolve_remote_workspace_path(
    transport: TransportBackend, workspace: Workspace, requested: str
) -> tuple[str | None, str | None]:
    """Resolve um caminho no host remoto e o confina à raiz do workspace."""
    raw = PurePosixPath(requested)
    if ".." in raw.parts:
        return None, "Path escapes the remote workspace."
    root = getattr(workspace, "remote_path", None)
    if not root:
        result = await transport.run(["pwd"], cwd=".", timeout=15.0)
        if result.exit_code != 0 or not result.stdout.strip():
            return None, "Unable to resolve the remote workspace root."
        root = result.stdout.strip().splitlines()[-1]
    root = str(PurePosixPath(root))
    candidate = str(raw) if raw.is_absolute() else str(PurePosixPath(root) / raw)
    script = (
        "import os,sys; r=os.path.realpath(sys.argv[1]); "
        "p=os.path.realpath(sys.argv[2]); "
        "sys.exit(2) if os.path.commonpath((r,p)) != r else print(p)"
    )
    result = await transport.run(
        ["python3", "-c", script, root, candidate], cwd=root, timeout=15.0
    )
    if result.exit_code != 0:
        return None, "Path escapes the remote workspace or cannot be resolved."
    resolved = result.stdout.strip().splitlines()
    if not resolved:
        return None, "Unable to resolve the remote path."
    return resolved[-1], None
