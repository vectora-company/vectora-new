"""A validação visual Godot respeita o escopo do workspace e a política da tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.api.handlers.workspaces import (
    GodotValidationRequest,
    validate_workspace_godot_project,
)


def _request(user_id: str = "user-1") -> Request:
    request = Request(
        {"type": "http", "method": "POST", "path": "/workspaces/ws/godot/validate"}
    )
    request.state.user = SimpleNamespace(id=user_id)
    return request


@pytest.mark.asyncio
async def test_validates_only_the_requested_workspace_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.api.handlers import workspaces
    from backend.rbac import tool_policy
    from backend.tools.registry import TOOL_REGISTRY

    workspace = SimpleNamespace(trusted=True, transport="local")
    monkeypatch.setattr(workspaces, "require_workspace_access", lambda *_: workspace)
    monkeypatch.setattr(tool_policy, "is_allowed", lambda *_: True)
    invoke = AsyncMock(
        return_value=json.dumps(
            {"status": "passed", "engine_version": "4.6", "stages": []}
        )
    )
    monkeypatch.setattr(
        TOOL_REGISTRY, "get", lambda _name: SimpleNamespace(ainvoke=invoke)
    )

    result = await validate_workspace_godot_project(
        "ws",
        _request(),
        GodotValidationRequest(
            project_path="games/platformer",
            expected_version="4.6",
            executable_path="/opt/godot/Godot",
        ),
    )

    assert result["status"] == "passed"
    assert invoke.await_args is not None
    args, context = invoke.await_args.args
    assert args == {
        "project_path": "games/platformer",
        "expected_version": "4.6",
        "timeout_seconds": 120,
    }
    assert context.workspace_id == "ws"
    assert context.user_id == "user-1"
    assert context._extra["godot_executable_path"] == "/opt/godot/Godot"


@pytest.mark.asyncio
async def test_rejects_untrusted_workspace_before_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.api.handlers import workspaces

    monkeypatch.setattr(
        workspaces,
        "require_workspace_access",
        lambda *_: SimpleNamespace(trusted=False, transport="local"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await validate_workspace_godot_project(
            "ws", _request(), GodotValidationRequest()
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_rejects_remote_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.api.handlers import workspaces

    monkeypatch.setattr(
        workspaces,
        "require_workspace_access",
        lambda *_: SimpleNamespace(trusted=True, transport="ssh"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await validate_workspace_godot_project(
            "ws", _request(), GodotValidationRequest()
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_obeys_disabled_tool_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.api.handlers import workspaces
    from backend.rbac import tool_policy

    monkeypatch.setattr(
        workspaces,
        "require_workspace_access",
        lambda *_: SimpleNamespace(trusted=True, transport="local"),
    )
    monkeypatch.setattr(tool_policy, "is_allowed", lambda *_: False)

    with pytest.raises(HTTPException) as exc_info:
        await validate_workspace_godot_project(
            "ws", _request(), GodotValidationRequest()
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_enforces_a_total_timeout_for_all_validation_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.api.handlers import workspaces
    from backend.rbac import tool_policy
    from backend.tools.registry import TOOL_REGISTRY

    monkeypatch.setattr(
        workspaces,
        "require_workspace_access",
        lambda *_: SimpleNamespace(trusted=True, transport="local"),
    )
    monkeypatch.setattr(tool_policy, "is_allowed", lambda *_: True)
    invoke = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(
        TOOL_REGISTRY, "get", lambda _name: SimpleNamespace(ainvoke=invoke)
    )

    with pytest.raises(HTTPException) as exc_info:
        await validate_workspace_godot_project(
            "ws", _request(), GodotValidationRequest(timeout_seconds=10)
        )

    assert exc_info.value.status_code == 504
