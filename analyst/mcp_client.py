"""Thin async client for the analyst MCP server (stdio transport)."""

from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPToolbox:
    """Spawns an MCP server as a subprocess and calls its tools.

    By default it launches this project's ``analyst.mcp_server``; pass a
    different ``command``/``args`` to point the agent at any MCP server that
    exposes the same tool names (list_tables, describe_schema, run_query,
    render_chart).
    """

    def __init__(self, db_path: str | None = None, command: str | None = None,
                 args: list[str] | None = None, env: dict[str, str] | None = None):
        server_env = dict(os.environ)
        if db_path:
            server_env["ANALYST_DB"] = str(db_path)
        server_env.update(env or {})
        self._params = StdioServerParameters(
            command=command or sys.executable,
            args=args if args is not None else ["-m", "analyst.mcp_server"],
            env=server_env,
        )
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tool_names: list[str] = []

    async def __aenter__(self) -> "MCPToolbox":
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(self._params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        self.tool_names = [t.name for t in (await self.session.list_tools()).tools]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack:
            await self._stack.aclose()
        self._stack = self.session = None

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call an MCP tool and return its JSON payload as a dict."""
        if self.session is None:
            raise RuntimeError("MCPToolbox is not connected; use 'async with'.")
        res = await self.session.call_tool(name, arguments or {})
        structured = getattr(res, "structured_content", None)
        if isinstance(structured, dict) and "ok" in structured:
            return structured
        text = "".join(getattr(c, "text", "") for c in (res.content or []))
        if getattr(res, "is_error", False):
            return {"ok": False, "error": text or f"MCP tool {name} failed"}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"ok": True, "text": text}
