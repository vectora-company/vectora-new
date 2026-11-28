"""Host de extensões: ciclo de vida, integridade, grants e broker de tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.extensions.host import (
    ExtensionError,
    ExtensionHost,
    GrantsUpdate,
    InstallRequest,
    compute_bundle_digest,
)
from backend.extensions.models import (
    Contribution,
    ExtensionRelease,
    ExtensionState,
    Grants,
)
from backend.extensions.store import ExtensionStore
from backend.tools.context import ToolContext
from backend.vtypes.contracts import JsonObject, JsonValue


class FakeClient:
    """Cliente MCP falso: registra exatamente o que o broker entrega."""

    instances: list[FakeClient] = []
    fail_connect = False
    call_error: Exception | None = None
    call_delay = 0.0

    def __init__(self) -> None:
        self.connections: dict[str, JsonObject] = {}
        self.calls: list[tuple[str, JsonObject, ToolContext | None]] = []
        self.closed = False
        FakeClient.instances.append(self)

    async def connect(
        self, connections: dict[str, JsonObject], *, strict: bool = False
    ) -> None:
        if FakeClient.fail_connect:
            raise RuntimeError("sem jail")
        self.connections = connections

    def tools(self) -> list[str]:
        return ["echo", "secret"]

    async def call_tool(
        self,
        name: str,
        /,
        arguments: JsonObject,
        ctx: ToolContext | None = None,
    ) -> str:
        if FakeClient.call_delay:
            await asyncio.sleep(FakeClient.call_delay)
        if FakeClient.call_error is not None:
            raise FakeClient.call_error
        self.calls.append((name, arguments, ctx))
        return f"ok:{name}"

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances = []
    FakeClient.fail_connect = False
    FakeClient.call_error = None
    FakeClient.call_delay = 0.0


@pytest.fixture
def host(tmp_path: Path) -> ExtensionHost:
    return ExtensionHost(
        ExtensionStore(tmp_path / "extensions"),
        client_factory=FakeClient,
        call_timeout=0.2,
    )


def _bundle(host: ExtensionHost, name: str = "demo-1", content: str = "x") -> Path:
    path = host.bundles_root / name
    path.mkdir(parents=True)
    (path / "main.js").write_text(content, encoding="utf-8")
    return path


def _request(
    host: ExtensionHost,
    *,
    version: str = "1.0.0",
    content: str = "x",
    **overrides: JsonValue | list[Contribution],
) -> InstallRequest:
    path = _bundle(host, f"demo-{version}-{content}", content)
    release: dict[str, JsonValue | list[Contribution]] = {
        "version": version,
        "digest": compute_bundle_digest(path),
        "install_path": str(path),
        "command": "node",
        "args": ["./main.js"],
        "declared_tools": ["echo"],
        "contributions": [
            Contribution(kind="mode", id="studio", label="Studio", entry="studio.html"),
            Contribution(
                kind="settings_page", id="config", label="Config", entry="config.html"
            ),
        ],
    }
    release.update(overrides)
    return InstallRequest(
        id="demo",
        name="Demo",
        publisher="acme",
        release=ExtensionRelease.model_validate(release),
    )


async def _active_host(host: ExtensionHost) -> ExtensionHost:
    await host.install(_request(host), actor="admin")
    await host.set_grants(
        "demo",
        GrantsUpdate(
            tools=["echo"], allow_hosts=["api.exemplo.com"], workspaces=["ws1"]
        ),
        actor="admin",
    )
    await host.activate("demo", actor="admin")
    return host


# ---------------------------------------------------------------- instalação


async def test_install_registers_inactive_extension_with_no_grants(
    host: ExtensionHost,
) -> None:
    record = await host.install(_request(host), actor="admin")

    assert record.state is ExtensionState.INACTIVE
    assert record.grants == Grants()
    assert [item.id for item in host.installed()] == ["demo"]


async def test_install_rejects_digest_mismatch(host: ExtensionHost) -> None:
    request = _request(host)
    request.release.digest = "0" * 64

    with pytest.raises(ExtensionError) as excinfo:
        await host.install(request, actor="admin")

    assert excinfo.value.code == "integrity"


async def test_install_rejects_bundle_outside_host_directory(
    host: ExtensionHost, tmp_path: Path
) -> None:
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "main.js").write_text("x", encoding="utf-8")
    request = _request(host)
    request.release.install_path = str(outside)
    request.release.digest = compute_bundle_digest(outside)

    with pytest.raises(ExtensionError) as excinfo:
        await host.install(request, actor="admin")

    assert excinfo.value.code == "invalid"


@pytest.mark.parametrize("command", ["/bin/sh", "../fora", "C:\\x.exe"])
async def test_install_rejects_unsafe_commands(
    host: ExtensionHost, command: str
) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await host.install(_request(host, command=command), actor="admin")

    assert excinfo.value.code == "invalid"


async def test_install_rejects_incompatible_host_api(host: ExtensionHost) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await host.install(_request(host, host_api=99), actor="admin")

    assert excinfo.value.code == "incompatible"


async def test_install_rejects_duplicate_contribution(host: ExtensionHost) -> None:
    duplicated = [
        Contribution(kind="mode", id="studio", label="A", entry="a.html"),
        Contribution(kind="mode", id="studio", label="B", entry="b.html"),
    ]

    with pytest.raises(ExtensionError) as excinfo:
        await host.install(_request(host, contributions=duplicated), actor="admin")

    assert excinfo.value.code == "conflict"


def test_bundle_digest_refuses_symlinks(tmp_path: Path) -> None:
    bundle = tmp_path / "pkg"
    bundle.mkdir()
    (bundle / "real.txt").write_text("a", encoding="utf-8")
    (bundle / "link.txt").symlink_to(bundle / "real.txt")

    with pytest.raises(ExtensionError):
        compute_bundle_digest(bundle)


def test_models_reject_reserved_ids_and_wildcard_hosts() -> None:
    with pytest.raises(ValidationError):
        Contribution(kind="mode", id="vectora-assistant", label="x", entry="x.html")
    with pytest.raises(ValidationError):
        Grants(allow_hosts=["*"])


# ------------------------------------------------------------------ ativação


async def test_activate_starts_the_extension_in_the_jail_with_granted_hosts(
    host: ExtensionHost,
) -> None:
    await _active_host(host)

    connection = FakeClient.instances[-1].connections["demo"]
    assert host.get("demo").state is ExtensionState.ACTIVE
    assert connection["require_sandbox"] is True
    assert connection["allow_hosts"] == ["api.exemplo.com"]
    assert connection["env"] == {}


async def test_activate_failure_marks_failed_and_closes_the_client(
    host: ExtensionHost,
) -> None:
    await host.install(_request(host), actor="admin")
    FakeClient.fail_connect = True

    with pytest.raises(ExtensionError) as excinfo:
        await host.activate("demo", actor="admin")

    assert excinfo.value.code == "failed"
    assert host.get("demo").state is ExtensionState.FAILED
    assert FakeClient.instances[-1].closed is True


async def test_activate_rechecks_integrity_of_the_installed_bytes(
    host: ExtensionHost,
) -> None:
    request = _request(host)
    await host.install(request, actor="admin")
    (Path(request.release.install_path) / "main.js").write_text(
        "adulterado", encoding="utf-8"
    )

    with pytest.raises(ExtensionError) as excinfo:
        await host.activate("demo", actor="admin")

    assert excinfo.value.code == "integrity"
    assert host.get("demo").state is ExtensionState.FAILED
    assert FakeClient.instances == []


# -------------------------------------------------------------------- broker


async def test_call_tool_forwards_only_the_explicit_arguments(
    host: ExtensionHost,
) -> None:
    await _active_host(host)

    result = await host.call_tool(
        "demo", "echo", {"text": "oi"}, workspace_id="ws1", actor="u1"
    )

    assert result == "ok:echo"
    assert FakeClient.instances[-1].calls == [("echo", {"text": "oi"}, None)]


async def test_call_tool_is_denied_without_a_grant(host: ExtensionHost) -> None:
    await host.install(_request(host), actor="admin")
    await host.activate("demo", actor="admin")

    with pytest.raises(ExtensionError) as excinfo:
        await host.call_tool("demo", "echo", {}, workspace_id="ws1", actor="u1")

    assert excinfo.value.code == "denied"
    assert FakeClient.instances[-1].calls == []


async def test_call_tool_is_denied_in_another_workspace(host: ExtensionHost) -> None:
    await _active_host(host)

    with pytest.raises(ExtensionError) as excinfo:
        await host.call_tool("demo", "echo", {}, workspace_id="ws2", actor="u1")

    assert excinfo.value.code == "denied"


async def test_call_tool_is_denied_for_a_tool_the_extension_did_not_declare(
    host: ExtensionHost,
) -> None:
    await _active_host(host)

    with pytest.raises(ExtensionError) as excinfo:
        await host.call_tool("demo", "secret", {}, workspace_id="ws1", actor="u1")

    assert excinfo.value.code == "denied"


async def test_call_tool_rejects_unserializable_and_oversized_arguments(
    host: ExtensionHost,
) -> None:
    await _active_host(host)

    # Referência circular: um objeto que o JSON não consegue serializar.
    circular: JsonObject = {}
    circular["x"] = circular

    with pytest.raises(ExtensionError) as bad:
        await host.call_tool("demo", "echo", circular, workspace_id="ws1", actor="u")
    with pytest.raises(ExtensionError) as big:
        await host.call_tool(
            "demo", "echo", {"x": "a" * 300_000}, workspace_id="ws1", actor="u"
        )

    assert bad.value.code == "invalid"
    assert big.value.code == "invalid"


async def test_call_tool_requires_an_active_extension(host: ExtensionHost) -> None:
    await host.install(_request(host), actor="admin")

    with pytest.raises(ExtensionError) as excinfo:
        await host.call_tool("demo", "echo", {}, workspace_id="ws1", actor="u1")

    assert excinfo.value.code == "not_active"


async def test_timeout_fails_the_extension_without_hanging_the_caller(
    host: ExtensionHost,
) -> None:
    await _active_host(host)
    FakeClient.call_delay = 1.0

    with pytest.raises(ExtensionError) as excinfo:
        await host.call_tool("demo", "echo", {}, workspace_id="ws1", actor="u1")

    assert excinfo.value.code == "failed"
    assert host.get("demo").state is ExtensionState.FAILED
    assert FakeClient.instances[-1].closed is True


async def test_crash_hides_the_internal_error_and_stops_the_process(
    host: ExtensionHost,
) -> None:
    await _active_host(host)
    FakeClient.call_error = RuntimeError("token=segredo-interno")

    with pytest.raises(ExtensionError) as excinfo:
        await host.call_tool("demo", "echo", {}, workspace_id="ws1", actor="u1")

    assert "segredo-interno" not in excinfo.value.message
    assert host.get("demo").state is ExtensionState.FAILED


# ------------------------------------------------------------ outras operações


async def test_revoked_extension_cannot_be_activated_or_reinstalled_with_same_bytes(
    host: ExtensionHost,
) -> None:
    request = _request(host)
    await host.install(request, actor="admin")
    await host.revoke("demo", actor="admin", reason="vulnerabilidade")

    with pytest.raises(ExtensionError) as activate:
        await host.activate("demo", actor="admin")
    with pytest.raises(ExtensionError) as same:
        await host.install(request, actor="admin")
    fixed = await host.install(
        _request(host, version="1.0.1", content="y"), actor="admin"
    )

    assert activate.value.code == "revoked"
    assert same.value.code == "revoked"
    assert fixed.state is ExtensionState.INACTIVE


async def test_rollback_swaps_to_the_previous_release(host: ExtensionHost) -> None:
    await host.install(_request(host, version="1.0.0"), actor="admin")
    await host.install(_request(host, version="1.1.0", content="y"), actor="admin")

    record = await host.rollback("demo", actor="admin")

    assert record.current.version == "1.0.0"
    assert record.previous is not None and record.previous.version == "1.1.0"


async def test_rollback_without_previous_release_fails(host: ExtensionHost) -> None:
    await host.install(_request(host), actor="admin")

    with pytest.raises(ExtensionError):
        await host.rollback("demo", actor="admin")


async def test_remove_stops_the_process_and_deletes_the_bundle(
    host: ExtensionHost,
) -> None:
    await _active_host(host)
    bundle = Path(host.get("demo").current.install_path)

    await host.remove("demo", actor="admin")

    assert FakeClient.instances[-1].closed is True
    assert not bundle.exists()
    with pytest.raises(ExtensionError):
        host.get("demo")


async def test_grants_must_reference_declared_tools(host: ExtensionHost) -> None:
    await host.install(_request(host), actor="admin")

    with pytest.raises(ExtensionError) as excinfo:
        await host.set_grants("demo", GrantsUpdate(tools=["inventada"]), actor="admin")

    assert excinfo.value.code == "invalid"


async def test_changing_granted_hosts_restarts_an_active_extension(
    host: ExtensionHost,
) -> None:
    await _active_host(host)
    first = FakeClient.instances[-1]

    await host.set_grants(
        "demo",
        GrantsUpdate(
            tools=["echo"], allow_hosts=["outro.exemplo.com"], workspaces=["ws1"]
        ),
        actor="admin",
    )

    assert first.closed is True
    assert FakeClient.instances[-1].connections["demo"]["allow_hosts"] == [
        "outro.exemplo.com"
    ]


async def test_only_active_extensions_contribute_to_the_interface(
    host: ExtensionHost,
) -> None:
    await host.install(_request(host), actor="admin")
    assert host.contributions() == []

    await host.activate("demo", actor="admin")
    ids = {item["id"] for item in host.contributions()}

    assert ids == {"demo.mode.studio", "demo.settings_page.config"}


async def test_registry_and_audit_survive_a_restart(
    host: ExtensionHost, tmp_path: Path
) -> None:
    await _active_host(host)

    reopened = ExtensionHost(
        ExtensionStore(tmp_path / "extensions"), client_factory=FakeClient
    )
    assert reopened.get("demo").state is ExtensionState.ACTIVE
    await reopened.restore()

    assert FakeClient.instances[-1].connections["demo"]
    actions = [entry["action"] for entry in reopened.audit("demo")]
    assert actions[:3] == ["install", "grants", "activate"]


async def test_shutdown_stops_every_running_extension(host: ExtensionHost) -> None:
    await _active_host(host)

    await host.shutdown()

    assert FakeClient.instances[-1].closed is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "mode", "entry": None},
        {"kind": "file_icon", "entry": "x.html", "file_extensions": ["md"]},
        {"kind": "file_icon", "file_extensions": []},
        {"kind": "settings_page", "entry": "../fora.html"},
        {"kind": "settings_page", "entry": "/etc/passwd.html"},
        {"kind": "settings_page", "entry": "pagina.js"},
    ],
)
def test_contribution_entry_rules(overrides: JsonObject) -> None:
    data: JsonObject = {"id": "ok", "label": "Ok", **overrides}

    with pytest.raises(ValidationError):
        Contribution.model_validate(data)
