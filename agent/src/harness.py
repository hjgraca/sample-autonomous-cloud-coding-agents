"""Provider-neutral agent harness contract owned by ABCA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from models import AgentResult, TaskConfig
    from progress_writer import _ProgressWriter
    from telemetry import _TrajectoryWriter


@dataclass(frozen=True)
class HarnessRequest:
    """Inputs required to execute one autonomous agent session."""

    prompt: str
    system_prompt: str
    config: TaskConfig
    cwd: str
    enabled_tools: frozenset[str]
    offer_clarification: bool
    policy_engine: Any
    progress: _ProgressWriter
    trajectory: _TrajectoryWriter | None = None


class AgentHarness(Protocol):
    """Runtime boundary that keeps vendor SDK types out of the pipeline."""

    async def run(self, request: HarnessRequest) -> AgentResult:
        """Execute the request and return ABCA's stable result model."""
        ...
