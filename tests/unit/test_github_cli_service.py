from __future__ import annotations

import logging
from collections.abc import Awaitable
from unittest.mock import AsyncMock

import pytest

from backend.services import github_cli
from backend.vtypes.contracts import JsonObject


class _Process:
    def __init__(
        self, *, returncode: int = 0, stdout: bytes = b"ok", stderr: bytes = b""
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.killed = False
        self.input: bytes | None = None

    async def communicate(self, input_data: bytes | None = None) -> tuple[bytes, bytes]:
        self.input = input_data
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_run_gh_uses_safe_environment_and_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    process = _Process(
        stdout=b"https://user:ghp_12345678901234567890@github.com/a/b",
        stderr=b"github_pat_12345678901234567890",
    )
    calls: list[tuple[tuple[str, ...], dict[str, int | str | None]]] = []

    async def create_process(*args: str, **kwargs: int | str | None) -> _Process:
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(
        github_cli.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/gh"
    )
    from backend.rbac import auth

    monkeypatch.setattr(
        auth,
        "get_env_overrides",
        AsyncMock(return_value={"GITHUB_TOKEN": "user-token"}),
    )
    monkeypatch.setattr(github_cli.asyncio, "create_subprocess_exec", create_process)
    caplog.set_level(logging.INFO, logger=github_cli.__name__)

    result = await github_cli.run_gh_command(["pr", "list"], user_id="user-1")

    assert result == {
        "status": "ok",
        "output": "https://***@github.com/a/b",
    }
    _, kwargs = calls[0]
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["GH_TOKEN"] == "user-token"
    assert env["GH_PAGER"] == ""
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert env["NO_COLOR"] == "1"
    assert "ghp_12345678901234567890" not in caplog.text
    assert "github_pat_12345678901234567890" not in caplog.text
    assert "user-token" not in caplog.text


@pytest.mark.asyncio
async def test_run_gh_sends_write_body_only_over_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process()
    monkeypatch.setattr(
        github_cli.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/gh"
    )

    async def create_process(*_args: str, **_kwargs: int | str | None) -> _Process:
        return process

    monkeypatch.setattr(github_cli.asyncio, "create_subprocess_exec", create_process)

    result = await github_cli.run_gh_command(
        ["pr", "create", "--title", "titulo", "--body-file", "-"],
        input_data="descrição privada",
    )

    assert result["status"] == "ok"
    assert process.input == "descrição privada".encode()


@pytest.mark.asyncio
async def test_run_gh_rejects_body_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_cli.shutil, "which", lambda _: "/usr/bin/gh")
    create_process = AsyncMock()
    monkeypatch.setattr(github_cli.asyncio, "create_subprocess_exec", create_process)

    result = await github_cli.run_gh_command(
        ["issue", "create", "--body", "texto sensível"]
    )

    assert result["status"] == "error"
    assert result["code"] == "gh_body_in_argv"
    create_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_gh_classifies_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_cli.shutil, "which", lambda *_args, **_kwargs: None)

    result = await github_cli.run_gh_command(["pr", "list"])

    assert result["status"] == "error"
    assert result["code"] == "gh_unavailable"


@pytest.mark.asyncio
async def test_run_gh_classifies_unauthenticated_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(returncode=1, stderr=b"You are not logged into any GitHub hosts")
    monkeypatch.setattr(
        github_cli.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/gh"
    )

    async def create_process(*_args: str, **_kwargs: int | str | None) -> _Process:
        return process

    monkeypatch.setattr(github_cli.asyncio, "create_subprocess_exec", create_process)

    result = await github_cli.run_gh_command(["pr", "list"])

    assert result["status"] == "error"
    assert result["code"] == "gh_unauthenticated"


@pytest.mark.asyncio
async def test_run_gh_redacts_credentials_from_process_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(
        returncode=1,
        stderr=(
            b"https://user:token123@github.com/o/r; "
            b"ghp_12345678901234567890; github_pat_12345678901234567890"
        ),
    )
    monkeypatch.setattr(
        github_cli.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/gh"
    )
    monkeypatch.setattr(
        github_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )

    result = await github_cli.run_gh_command(["pr", "view", "1"])

    assert result["status"] == "error"
    assert "https://***@github.com/o/r" in result["message"]
    assert "token123" not in result["message"]
    assert "ghp_12345678901234567890" not in result["message"]
    assert "github_pat_12345678901234567890" not in result["message"]


@pytest.mark.asyncio
async def test_run_gh_uses_separate_read_and_write_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    async def wait_for(
        _awaitable: Awaitable[tuple[bytes, bytes]], **kwargs: float
    ) -> tuple[bytes, bytes]:
        seen.append(kwargs["timeout"])
        return b"", b""

    async def create_process(*_args: str, **_kwargs: int | str | None) -> _Process:
        return _Process()

    monkeypatch.setattr(
        github_cli.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/gh"
    )
    monkeypatch.setattr(github_cli.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(github_cli.asyncio, "wait_for", wait_for)

    await github_cli.run_gh_command(["pr", "list"])
    await github_cli.run_gh_command(
        ["issue", "comment", "1", "--body-file", "-"], input_data="x"
    )

    assert seen == [30.0, 120.0]


@pytest.mark.asyncio
async def test_run_gh_kills_process_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process()

    async def create_process(*_args: str, **_kwargs: int | str | None) -> _Process:
        return process

    async def timeout(
        _awaitable: Awaitable[tuple[bytes, bytes]], **_kwargs: float
    ) -> tuple[bytes, bytes]:
        raise TimeoutError

    monkeypatch.setattr(
        github_cli.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/gh"
    )
    monkeypatch.setattr(github_cli.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(github_cli.asyncio, "wait_for", timeout)

    result = await github_cli.run_gh_command(["pr", "list"])

    assert result["code"] == "gh_timeout"
    assert process.killed


@pytest.mark.asyncio
async def test_run_gh_exposes_only_etag_and_rate_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(
        stdout=(
            b'HTTP/2.0 200 OK\r\nETag: "etag-1"\r\n'
            b"X-RateLimit-Remaining: 9\r\nAuthorization: secret\r\n\r\n[]"
        )
    )
    monkeypatch.setattr(
        github_cli.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/gh"
    )
    monkeypatch.setattr(
        github_cli.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    result = await github_cli.run_gh_command(["api", "--include", "repos/o/r/issues"])

    assert result["http_status"] == 200
    assert result["etag"] == '"etag-1"'
    assert result["x-ratelimit-remaining"] == "9"
    assert "authorization" not in result


@pytest.mark.asyncio
async def test_run_gh_returns_rate_limit_reset_from_http_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_at = 1_900_000_000
    process = _Process(
        returncode=1,
        stdout=(
            f"HTTP/2.0 403 Forbidden\r\n"
            f"X-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: {reset_at}\r\n\r\n"
        ).encode(),
        stderr=b"API rate limit exceeded",
    )
    monkeypatch.setattr(
        github_cli.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/gh"
    )
    monkeypatch.setattr(
        github_cli.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    result = await github_cli.run_gh_command(["api", "--include", "repos/o/r/issues"])

    assert result["code"] == "rate_limited"
    assert result["reset_at"] == reset_at
    assert result["http_status"] == 403


@pytest.mark.asyncio
async def test_github_auth_epoch_is_a_non_reversible_token_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.rbac import auth

    monkeypatch.setattr(
        auth,
        "get_env_overrides",
        AsyncMock(return_value={"GITHUB_TOKEN": "private-token"}),
    )

    epoch = await github_cli.github_auth_epoch("user-1")

    assert epoch == github_cli.hashlib.sha256(b"private-token").hexdigest()
    assert "private-token" not in epoch
