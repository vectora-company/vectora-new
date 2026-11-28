"""Ponte entre o harness e as tools das extensões Vext.

A extensão oferece tools com a semântica das tools MCP. O harness chama esta
tool, e o host entrega à extensão só o nome da tool e os argumentos explícitos
da chamada: nunca transcript, memória, contexto do modelo ou credenciais.
"""

from __future__ import annotations

import json
import logging

from backend.extensions.host import ExtensionError, get_extension_host
from backend.tools.context import ToolContext
from backend.tools.registry import ToolExtras, vtool
from backend.vtypes.contracts import JsonValue

logger = logging.getLogger(__name__)


@vtool(
    extras=ToolExtras(
        render_hint="json",
        category="mcp",
        destructive=False,
        icon="puzzle",
    )
)
async def call_extension_tool(
    extension_id: str,
    tool_name: str,
    arguments: str,
    ctx: ToolContext | None = None,
) -> str:
    """Invoca uma tool oferecida por uma extensão Vext instalada e ativa.

    Use só com extensões e tools que o usuário concedeu a este workspace; sem
    concessão a chamada é recusada.

    Args:
        extension_id: Identificador da extensão
        tool_name: Nome da tool oferecida pela extensão
        arguments: Argumentos em formato JSON string (ex: '{"query": "x"}')

    Returns:
        Resposta da tool, ou mensagem de erro descritiva
    """
    try:
        parsed: JsonValue = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return f"Erro: 'arguments' deve ser JSON válido — {exc}"
    if not isinstance(parsed, dict):
        return "Erro: 'arguments' deve ser um objeto JSON."
    host = get_extension_host()
    try:
        return await host.call_tool(
            extension_id,
            tool_name,
            parsed,
            workspace_id=ctx.workspace_id if ctx is not None else "",
            actor=ctx.user_id if ctx is not None else "local",
            ctx=None,
        )
    except ExtensionError as exc:
        return f"Erro: {exc.message}"
