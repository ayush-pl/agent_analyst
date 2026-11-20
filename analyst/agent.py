"""High-level entry point that owns the MCP connection, graph and checkpointer."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from analyst.graph import build_graph
from analyst.llm import AnalystLLM, default_llm
from analyst.mcp_client import MCPToolbox


@dataclass
class AnalystRun:
    thread_id: str
    state: dict
    pending_approval: Optional[dict] = None   # interrupt payload when waiting on a human

    @property
    def done(self) -> bool:
        return self.pending_approval is None


@dataclass
class DataAnalyst:
    """``async with DataAnalyst(db_path=...) as analyst: run = await analyst.ask(q)``"""

    db_path: Optional[str] = None
    llm: Optional[AnalystLLM] = None
    require_approval: bool = True
    max_retries: int = 3
    output_dir: str = "outputs"
    mcp_command: Optional[str] = None
    mcp_args: Optional[list[str]] = None
    _tools: Any = field(default=None, init=False, repr=False)
    graph: Any = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> "DataAnalyst":
        self.llm = self.llm or default_llm()
        self._tools = MCPToolbox(db_path=self.db_path, command=self.mcp_command, args=self.mcp_args)
        await self._tools.__aenter__()
        self.graph = build_graph(self.llm, self._tools, checkpointer=InMemorySaver())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._tools.__aexit__(*exc)

    @property
    def mcp_tools(self) -> list[str]:
        return self._tools.tool_names

    async def _invoke(self, payload: Any, thread_id: str) -> AnalystRun:
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
        out = await self.graph.ainvoke(payload, config)
        interrupts = out.get("__interrupt__") if isinstance(out, dict) else None
        state = (await self.graph.aget_state(config)).values
        return AnalystRun(thread_id, state, interrupts[0].value if interrupts else None)

    async def ask(self, question: str, thread_id: str | None = None) -> AnalystRun:
        """Start a run. Returns early with ``pending_approval`` if a human must review the SQL."""
        return await self._invoke({
            "question": question, "require_approval": self.require_approval,
            "max_retries": self.max_retries, "output_dir": self.output_dir, "events": [],
        }, thread_id or uuid.uuid4().hex)

    async def resume(self, run: AnalystRun, action: str = "approve", sql: str | None = None,
                     reason: str | None = None) -> AnalystRun:
        """Answer a pending approval: action = approve | edit (with sql) | reject (with reason)."""
        decision = {"action": action}
        if sql is not None:
            decision["sql"] = sql
        if reason is not None:
            decision["reason"] = reason
        return await self._invoke(Command(resume=decision), run.thread_id)
