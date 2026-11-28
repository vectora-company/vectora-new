"""State e PKCE dos fluxos OAuth locais."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import _CachedRequest

from backend.api.handlers.oauth import router
from backend.services import oauth_state


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    oauth_state._pending.clear()


def test_state_is_random_and_not_derived_from_the_user() -> None:
    first, _ = oauth_state.begin("user-1", "github")
    second, _ = oauth_state.begin("user-1", "github")

    assert first != second
    assert "user-1" not in first
    assert len(first) >= 32


def test_challenge_is_the_s256_of_the_stored_verifier() -> None:
    state, challenge = oauth_state.begin("user-1", "gitlab")

    verifier = oauth_state._pending[state].code_verifier
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert "=" not in challenge


def test_consume_returns_the_owner_once() -> None:
    state, _ = oauth_state.begin("user-1", "github")

    transaction = oauth_state.consume(state, "github")

    assert transaction.user_id == "user-1"
    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.consume(state, "github")


def test_consume_rejects_unknown_state_and_other_provider() -> None:
    state, _ = oauth_state.begin("user-1", "github")

    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.consume("inventado", "github")
    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.consume(state, "gitlab")


def test_consume_rejects_expired_state() -> None:
    state, _ = oauth_state.begin("user-1", "github", now=1000.0)

    with pytest.raises(oauth_state.OAuthStateError):
        oauth_state.consume(
            state, "github", now=1000.0 + oauth_state.STATE_TTL_SECONDS + 1
        )


def test_pending_transactions_are_bounded() -> None:
    for _ in range(oauth_state._MAX_PENDING + 20):
        oauth_state.begin("user-1", "github")

    assert len(oauth_state._pending) == oauth_state._MAX_PENDING


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("VECTORA_ALLOW_LOCAL_OAUTH_FALLBACK", "1")
    app = FastAPI()
    app.include_router(router)

    def attach_user(request: _CachedRequest) -> None:
        request.state.user = SimpleNamespace(id="user-123")

    app.middleware("http")(lambda req, call_next: (attach_user(req), call_next(req))[1])
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


_GITHUB_ENV = {
    "GITHUB_OAUTH_CLIENT_ID": "gh-id",
    "GITHUB_OAUTH_CLIENT_SECRET": "gh-secret",
}


def test_callback_with_the_user_id_as_state_is_refused(client: TestClient) -> None:
    """O ataque antigo: state igual ao id da vítima com o code do atacante."""
    with (
        patch.dict("os.environ", _GITHUB_ENV),
        patch("backend.rbac.auth.set_env_override", new_callable=AsyncMock) as save,
    ):
        response = client.get("/auth/github/callback?code=atacante&state=user-123")

    assert "oauth_error=github_invalid_state" in response.headers["location"]
    save.assert_not_awaited()


def test_state_cannot_be_replayed(client: TestClient) -> None:
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"access_token": "tok"}
    with (
        patch.dict("os.environ", _GITHUB_ENV),
        patch("backend.rbac.auth.set_env_override", new_callable=AsyncMock) as save,
        patch("httpx.AsyncClient") as http,
    ):
        context = AsyncMock()
        context.__aenter__ = AsyncMock(return_value=context)
        context.__aexit__ = AsyncMock(return_value=False)
        context.post = AsyncMock(return_value=ok)
        http.return_value = context
        state, _ = oauth_state.begin("user-123", "github")

        first = client.get(f"/auth/github/callback?code=a&state={state}")
        second = client.get(f"/auth/github/callback?code=a&state={state}")

    assert "oauth_success=github" in first.headers["location"]
    assert "oauth_error=github_invalid_state" in second.headers["location"]
    assert save.await_count == 1


def test_token_exchange_sends_the_pkce_verifier(client: TestClient) -> None:
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"access_token": "tok"}
    with (
        patch.dict("os.environ", _GITHUB_ENV),
        patch("backend.rbac.auth.set_env_override", new_callable=AsyncMock) as save,
        patch("httpx.AsyncClient") as http,
    ):
        context = AsyncMock()
        context.__aenter__ = AsyncMock(return_value=context)
        context.__aexit__ = AsyncMock(return_value=False)
        context.post = AsyncMock(return_value=ok)
        http.return_value = context
        state, _ = oauth_state.begin("user-123", "github")
        verifier = oauth_state._pending[state].code_verifier

        client.get(f"/auth/github/callback?code=a&state={state}")

    assert context.post.await_args is not None
    assert save.await_args is not None
    assert context.post.await_args.kwargs["json"]["code_verifier"] == verifier
    assert save.await_args.args[0] == "user-123"


def test_start_uses_an_opaque_state_and_s256(client: TestClient) -> None:
    with patch.dict("os.environ", _GITHUB_ENV):
        response = client.get("/auth/github")

    location = response.headers["location"]
    assert "state=user-123" not in location
    assert "code_challenge_method=S256" in location
