"""Trusted per-user Decision Model preferences copied into an execution context."""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, cast

from backend.llm.systemone.types import DecisionProviderType
from backend.vtypes.contracts import JsonObject

if TYPE_CHECKING:
    from backend.vtypes.context import VectoraContext


def decision_model_preferences(ctx: VectoraContext) -> JsonObject:
    """Return the validated preferences attached by ``ctx_from_config``."""
    extra = getattr(ctx, "_extra", None)
    value = extra.get("decision_model_preferences") if isinstance(extra, dict) else None
    return value if isinstance(value, dict) else {}


def decision_model_enabled_for(
    ctx: VectoraContext, surface: str | tuple[str, ...]
) -> bool:
    """Require every applicable surface in the durable preference snapshot."""
    preferences = decision_model_preferences(ctx)
    surfaces = preferences.get("surfaces")
    required = (surface,) if isinstance(surface, str) else surface
    return bool(
        required
        and preferences.get("enabled") is True
        and isinstance(surfaces, dict)
        and all(surfaces.get(name) is True for name in required)
    )


_DECISION_PROVIDERS = frozenset(
    {"clef-local", "laya-local", "clef-api", "jev-api", "openrouter-api"}
)


def selected_decision_model_provider(
    ctx: VectoraContext,
) -> DecisionProviderType | None:
    """Return a supported provider id from the trusted context snapshot."""
    provider = decision_model_preferences(ctx).get("provider")
    if provider in _DECISION_PROVIDERS:
        return cast("DecisionProviderType", provider)
    return None


def decision_model_runtime_fingerprint(provider: str | None) -> str | None:
    """Fingerprint provider routing and credentials without persisting secrets."""
    if provider not in _DECISION_PROVIDERS:
        return None

    from backend.settings import settings

    if provider == "clef-local":
        config = {
            "base_url": settings.systemone_local_base_url,
            "model": settings.systemone_local_model,
            "credential": os.getenv("LLAMACPP_API_KEY", ""),
        }
    elif provider == "laya-local":
        config = {
            "base_url": settings.systemone_laya_base_url,
            "model": settings.systemone_laya_model,
            "credential": settings.systemone_laya_api_key
            or os.getenv("LAYA_API_KEY", ""),
        }
    elif provider == "openrouter-api":
        config = {
            "model": settings.systemone_openrouter_model,
            "credential": settings.openrouter_api_key
            or os.getenv("OPENROUTER_API_KEY", ""),
        }
    elif provider == "clef-api":
        config = {
            "base_url": settings.systemone_clef_api_base_url,
            "account_id": settings.systemone_clef_account_id
            or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
            or os.getenv("CLEF_ACCOUNT_ID", ""),
            "model": settings.systemone_clef_api_model,
            "credential": settings.clef_api_key or os.getenv("CLEF_API_KEY", ""),
        }
    else:
        config = {
            "base_url": settings.systemone_jev_api_base_url,
            "model": settings.systemone_jev_api_model,
            "credential": settings.jev_api_key or os.getenv("JEV_API_KEY", ""),
        }

    serialized = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def decision_model_surfaces_for_tool(
    ctx: VectoraContext, tool_name: str
) -> tuple[str, ...]:
    """Require provenance, execution mode and capability flags for a tool call."""
    extra = getattr(ctx, "_extra", None)
    extra = extra if isinstance(extra, dict) else {}
    name = tool_name.lower()
    if name in {"call_mcp_tool", "call_extension_tool"} or name.startswith("mcp_"):
        surface = "mcp"
    elif name in {
        "bash",
        "terminal",
        "terminal_tool",
        "open_terminal",
        "list_terminals",
        "attach_terminal",
        "close_terminal",
        "read_terminal",
        "write_terminal",
    } or name.startswith("terminal_"):
        surface = "terminal"
    elif name == "http_request" or name in {"web_crawl", "web_map", "fetch_url"}:
        surface = "network"
    elif name.startswith("browser") or name in {"web_search", "browser_search"}:
        surface = "browser"
    elif name.startswith("kanban_"):
        surface = "kanban"
    else:
        surface = "hitl"
    provenance = []
    if extra.get("connector_execution") is True:
        provenance.append("connectors")
    if extra.get("subagent_execution") is True:
        provenance.append("subagents")
    if getattr(ctx, "background_task_id", ""):
        provenance.append("background")
    if getattr(ctx, "permission_mode", None) == "plan":
        return (*provenance, "plan_mode", *((surface,) if surface != "hitl" else ()))
    return (*provenance, surface)
