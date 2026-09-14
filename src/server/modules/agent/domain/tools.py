"""Tool contract (§8): formal `ToolDefinition` + `ToolRegistry`.

NOT `dict[str, Callable]`. The registry is injected into the AgentService and
bridges tools to the LLM via `ToolSpec`. Formal protocol = MCP-ready seam.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

from server.modules.agent.domain.funnel_fsm import FunnelStage
from server.modules.agent.domain.llm_port import ToolSpec


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool may read at execution time. Tools never persist — they return
    a result dict and the AgentService applies any side effect.
    """

    funnel_stage: FunnelStage
    config: Mapping[str, object]


ToolHandler = Callable[["ToolContext", dict[str, object]], Awaitable[dict[str, object]]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A callable tool. `description` includes when NOT to use it (§8)."""

    name: str
    description: str
    input_schema: dict[str, object]
    handler: ToolHandler


class ToolNotFoundError(KeyError):
    """Raised when a requested tool name is not registered."""


class ToolRegistry:
    """Holds the tool catalogue; resolves names to definitions and to specs."""

    def __init__(self, tools: Iterable[ToolDefinition]) -> None:
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in tools}

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def specs(self) -> list[ToolSpec]:
        """The catalogue as the LLM sees it (name/description/input_schema)."""
        return [
            ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
            for t in self._tools.values()
        ]

    async def execute(
        self, name: str, ctx: ToolContext, tool_input: dict[str, object]
    ) -> dict[str, object]:
        return await self.get(name).handler(ctx, tool_input)
