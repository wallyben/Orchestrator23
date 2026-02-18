"""
Tool registry — uniform interface for orchestrator verification tools.

Every tool implements the Tool protocol (a `name` property and a `run()` method
that returns a ToolResult). The ToolRegistry collects tools, runs them in
registration order, and aggregates results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from logger import Logger


@dataclass(frozen=True)
class ToolResult:
    tool_name: str
    passed: bool
    output: str
    return_code: int


@runtime_checkable
class Tool(Protocol):
    @property
    def name(self) -> str: ...

    def run(self) -> ToolResult: ...


class ToolRegistry:
    def __init__(self, logger: Logger):
        self._tools: dict[str, Tool] = {}
        self._logger = logger
        self._on_run_hook: callable | None = None

    def set_on_run_hook(self, hook: callable) -> None:
        self._on_run_hook = hook

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError(
                f"{type(tool).__name__} does not implement the Tool protocol"
            )
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        self._logger.info("tool_registered", {"name": tool.name})

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"No tool registered with name: {name}")
        return self._tools[name]

    def run_all(self) -> list[ToolResult]:
        if self._on_run_hook is not None:
            try:
                self._on_run_hook(list(self._tools.keys()))
            except Exception:
                pass
        results: list[ToolResult] = []
        for tool in self._tools.values():
            self._logger.info("tool_run_start", {"name": tool.name})
            try:
                result = tool.run()
            except Exception as e:
                self._logger.error(
                    "tool_run_exception",
                    {"name": tool.name, "error": str(e), "type": type(e).__name__},
                )
                result = ToolResult(
                    tool_name=tool.name,
                    passed=False,
                    output=f"Tool {tool.name!r} raised {type(e).__name__}: {e}",
                    return_code=-1,
                )
            self._logger.info(
                "tool_run_done",
                {"name": tool.name, "passed": result.passed, "return_code": result.return_code},
            )
            results.append(result)
        return results

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
