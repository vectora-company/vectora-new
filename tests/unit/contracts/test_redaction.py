"""Testes unitários para sanitização e mascaramento de segredos em contratos."""

from backend.contracts.redaction import redact_payload, redact_string
from backend.vtypes.contracts import JsonObject, JsonValue
from tests.json_values import as_object, dig


def test_redact_sensitive_keys() -> None:
    """Verifica o comportamento de redact sensitive keys."""
    payload: JsonObject = {
        "api_key": "sk-secret123",
        "nested": {
            "token": "ghp_12345",
            "password": "pass",
            "safe": "visible",
            "credentials": {"nested_user": "admin", "nested_key": "secret"},
            "x-api-key": ["key1", "key2"],
        },
    }
    redacted = redact_payload(payload)
    assert dig(redacted, "api_key") == "[REDACTED]"
    assert dig(redacted, "nested", "token") == "[REDACTED]"
    assert dig(redacted, "nested", "password") == "[REDACTED]"
    assert dig(redacted, "nested", "safe") == "visible"
    assert dig(redacted, "nested", "credentials") == "[REDACTED]"
    assert dig(redacted, "nested", "x-api-key") == "[REDACTED]"


def test_redact_list_items() -> None:
    """Verifica o comportamento de redact list items."""
    payload: list[JsonValue] = [{"token": "sec1"}, {"safe": 123}]
    redacted = redact_payload(payload)
    assert dig(redacted, 0, "token") == "[REDACTED]"
    assert dig(redacted, 1, "safe") == 123


def test_redact_string_patterns() -> None:
    # sk- pattern
    """Verifica o comportamento de redact string patterns."""
    assert (
        redact_string("Authorization: sk-abc1234567890abcdef1234")
        == "Authorization: [REDACTED_API_KEY]"
    )
    # ghp_ pattern
    assert (
        redact_string("Token: ghp_1234567890abcdef1234567890abcdef")
        == "Token: [REDACTED_GH_TOKEN]"
    )
    # Bearer pattern
    assert (
        redact_string("Header: Bearer abcdef1234567890abcdef1234567890")
        == "Header: Bearer [REDACTED_TOKEN]"
    )
    # JWT pattern
    jwt_sample = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    assert redact_string(f"JWT: {jwt_sample}") == "JWT: [REDACTED_JWT]"


def test_redaction_distinguishes_schema_properties_from_payload() -> None:
    """Verifica o comportamento de redaction distinguishes schema properties from payload."""
    payload: JsonObject = {"type": "x", "description": "d", "password": "secret"}
    assert dig(redact_payload(payload), "password") == "[REDACTED]"

    schema: JsonObject = {
        "type": "object",
        "properties": {
            "token": {"type": "string", "enum": ["safe-example"]},
            "password": {"enum": ["secret-example"]},
        },
    }
    redacted = redact_payload(schema)
    assert dig(redacted, "properties", "token", "enum") == ["safe-example"]
    assert dig(redacted, "properties", "password", "enum") == ["secret-example"]


def test_redaction_covers_camel_case_sensitive_keys() -> None:
    """Verifica o comportamento de redaction covers camel case sensitive keys."""
    redacted = redact_payload({"apiToken": "secret", "session-token": 123})
    assert dig(redacted, "apiToken") == "[REDACTED]"
    assert dig(redacted, "session-token") == "[REDACTED]"


def test_redaction_preserves_paths_and_schema_map_keys() -> None:
    """Verifica o comportamento de redaction preserves paths and schema map keys."""
    payload: JsonObject = {
        "paths": {"/auth/token": {"post": {"description": "safe"}}},
        "schemas": {"TokenResponse": {"properties": {"token": {"type": "string"}}}},
    }
    redacted = redact_payload(payload)
    assert "/auth/token" in as_object(dig(redacted, "paths"))
    assert "TokenResponse" in as_object(dig(redacted, "schemas"))
    assert dig(redacted, "schemas", "TokenResponse", "properties", "token") == {
        "type": "string"
    }


def test_redaction_preserves_map_keys_but_redacts_nested_values() -> None:
    """Verifica o comportamento de redaction preserves map keys but redacts nested values."""
    payload: JsonObject = {
        "paths": {
            "/auth/token": {
                "post": {"security": {"api_key": "sk-123456789012345678901234"}}
            }
        },
        "schemas": {
            "TokenResponse": {
                "example": {"access_token": "Bearer abcdefghijklmnopqrstuv"}
            }
        },
    }

    redacted = redact_payload(payload)

    assert "/auth/token" in as_object(dig(redacted, "paths"))
    assert "TokenResponse" in as_object(dig(redacted, "schemas"))
    assert (
        dig(redacted, "paths", "/auth/token", "post", "security", "api_key")
        == "[REDACTED]"
    )
    assert (
        dig(redacted, "schemas", "TokenResponse", "example", "access_token")
        == "[REDACTED]"
    )


def test_redaction_preserves_sensitive_route_and_schema_names() -> None:
    """Garante que sanitização não elimina nomes legítimos do OpenAPI."""
    openapi: JsonObject = {
        "paths": {"/settings/api-token": {"post": {"operationId": "rotate_api_token"}}},
        "components": {
            "schemas": {
                "ApiTokenResponse": {
                    "type": "object",
                    "properties": {"token": {"type": "string"}},
                }
            }
        },
    }

    redacted = redact_payload(openapi)

    assert "/settings/api-token" in as_object(dig(redacted, "paths"))
    assert "ApiTokenResponse" in as_object(dig(redacted, "components", "schemas"))
    assert (
        dig(redacted, "paths", "/settings/api-token", "post", "operationId")
        == "rotate_api_token"
    )
