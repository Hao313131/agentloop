"""Expose AgentLoop's read-only tools over MCP (Model Context Protocol, stdio).

Any MCP client (Claude Desktop, Cursor, an internal agent) can then search the
knowledge base and look up orders with the same permission checks as the web agent.
Write tools (refunds, tickets) are deliberately not exposed.

    AGENTLOOP_MCP_USER=agent@harbour.demo python -m app.mcp_server

Client config example (Claude Desktop ``mcpServers``):
    "agentloop": {"command": "/path/to/backend/.venv/bin/python", "args": ["-m", "app.mcp_server"],
                 "cwd": "/path/to/backend", "env": {"AGENTLOOP_MCP_USER": "agent@harbour.demo"}}
"""

from __future__ import annotations

import json
import os
import uuid

from sqlalchemy import select

from .agent.business_tools import get_order, search_knowledge, track_shipment
from .agent.tools import ToolContext, ToolError
from .config import get_settings
from .db import init_engine, new_session, prepare_database
from .models import Tenant, User
from .services import Services


def _call(services: Services, handler, args: dict) -> str:
    email = os.environ.get("AGENTLOOP_MCP_USER", "agent@harbour.demo")
    with new_session() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            return json.dumps({"error": f"the MCP identity {email} does not exist"}, ensure_ascii=False)
        ctx = ToolContext(db=db, settings=services.settings, tenant=db.get(Tenant, user.tenant_id), user=user,
                          embedder=services.embedder, run_id=uuid.uuid4().hex, conversation_id=None, dry_run=True)
        try:
            output = handler(ctx, args)
        except ToolError as exc:
            output = {"error": str(exc)}
        output.pop("_debug", None)
        return json.dumps(output, ensure_ascii=False)


def build_server():
    from mcp.server.mcpserver import MCPServer

    settings = get_settings()
    init_engine(settings.database_url)
    prepare_database(settings.database_url, settings.auto_migrate)
    services = Services(settings)
    server = MCPServer("agentloop")

    @server.tool()
    def agentloop_search_knowledge(query: str, top_k: int = 4) -> str:
        """Search the AgentLoop business knowledge base. Returns numbered excerpts with relevance, filtered
        by what the MCP identity is allowed to see."""
        return _call(services, search_knowledge, {"query": query, "top_k": top_k})

    @server.tool()
    def agentloop_get_order(order_id: str) -> str:
        """Look up an order by number: status, total and whether it can be refunded."""
        return _call(services, get_order, {"order_id": order_id})

    @server.tool()
    def agentloop_track_shipment(order_id: str) -> str:
        """Look up an order's delivery progress."""
        return _call(services, track_shipment, {"order_id": order_id})

    return server


if __name__ == "__main__":  # pragma: no cover
    build_server().run("stdio")
