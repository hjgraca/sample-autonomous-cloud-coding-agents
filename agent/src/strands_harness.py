"""Strands implementation of ABCA's provider-neutral agent harness."""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from coding_tools import build_coding_tools
from gateway_tools import build_gateway_tool
from model_pricing import estimate_cost_usd, require_pricing
from models import AgentResult
from repo_instructions import load_repo_instructions
from shell import log, log_error_cw
from strands_hooks import StrandsHooks, usage_from_metrics

if TYPE_CHECKING:
    from harness import HarnessRequest


def _repo_discovery(config: Any) -> tuple[bool, frozenset[str]]:
    """Resolve workflow gates for repository-owned instructions and MCP."""
    if not config.repo_url:
        return False, frozenset()
    workflow = config.resolved_workflow if isinstance(config.resolved_workflow, dict) else {}
    repo_config = workflow.get("repo_config")
    if not isinstance(repo_config, dict):
        return True, frozenset()
    ignored = frozenset(str(value) for value in repo_config.get("ignore", []))
    return repo_config.get("discover", True) is not False, ignored


def _load_mcp_clients(repo_root: str) -> list[MCPClient]:
    """Load repo MCP servers through Strands, adapting the legacy config key."""
    path = Path(repo_root) / ".mcp.json"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    servers = config.get("mcpServers", {}) if isinstance(config, dict) else {}
    if not isinstance(servers, dict):
        raise ValueError(".mcp.json mcpServers must be an object")
    normalized: dict[str, Any] = {}
    transport_names = {
        "http": "streamable-http",
        "sse": "sse",
        "stdio": "stdio",
    }
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            normalized[name] = raw
            continue
        entry = dict(raw)
        legacy_type = entry.pop("type", None)
        if legacy_type and "transport" not in entry:
            entry["transport"] = transport_names.get(str(legacy_type), legacy_type)
        normalized[name] = entry
    return MCPClient.load_servers({"mcpServers": normalized})


class StrandsHarness:
    """Execute autonomous coding sessions with Strands and Amazon Bedrock."""

    async def run(self, request: HarnessRequest) -> AgentResult:
        config = request.config
        if config.max_budget_usd is not None:
            require_pricing(config.model_id)

        from aws_session import get_session

        model = BedrockModel(
            boto_session=get_session(),
            model_id=config.model_id,
            streaming=True,
        )
        clarification_state: dict[str, str] = {}
        enabled_tools = set(request.enabled_tools)
        if request.offer_clarification:
            enabled_tools.add("request_clarification")
        tools: list[Any] = build_coding_tools(
            request.cwd,
            enabled_tools=enabled_tools,
            clarification_state=clarification_state,
        )
        gateway_tool = build_gateway_tool()
        if gateway_tool is not None:
            tools.append(gateway_tool)
        discover_repo, ignored_repo_sources = _repo_discovery(config)
        mcp_clients = (
            _load_mcp_clients(request.cwd)
            if discover_repo and "mcp" not in ignored_repo_sources
            else []
        )
        tools.extend(mcp_clients)

        instructions = (
            load_repo_instructions(request.cwd, ignored=ignored_repo_sources)
            if discover_repo
            else ""
        )
        hooks = StrandsHooks(
            engine=request.policy_engine,
            model_id=config.model_id,
            max_budget_usd=config.max_budget_usd,
            task_id=config.task_id or "",
            user_id=config.user_id or "",
            repo_url=config.repo_url or "",
            progress=request.progress,
            trajectory=request.trajectory,
            clarification_state=clarification_state,
        )
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=request.system_prompt + instructions,
            hooks=[hooks],
            callback_handler=None,
            trace_attributes={
                "task.id": config.task_id or "unknown",
                "repo.url": config.repo_url or "none",
                "agent.model": config.model_id,
            },
        )

        started = monotonic()
        strands_result = None
        try:
            async for event in agent.stream_async(
                request.prompt,
                limits={"turns": config.max_turns},
            ):
                if "result" in event:
                    strands_result = event["result"]
        except Exception as exc:
            message = f"Strands agent failed: {type(exc).__name__}: {exc}"
            log_error_cw(message, task_id=config.task_id or None)
            request.progress.write_agent_error(
                error_type=type(exc).__name__,
                message=str(exc),
            )
            return AgentResult(
                status="error",
                error=message,
                turns=hooks.turns,
                num_turns=hooks.turns,
                duration_ms=int((monotonic() - started) * 1000),
                clarification_question=clarification_state.get("question", ""),
            )
        finally:
            agent.cleanup()

        if strands_result is None:
            return AgentResult(
                status="error",
                error="Strands stream ended without an AgentResult",
                turns=hooks.turns,
                num_turns=hooks.turns,
                duration_ms=int((monotonic() - started) * 1000),
                clarification_question=clarification_state.get("question", ""),
            )

        usage = usage_from_metrics(strands_result.metrics)
        cost_usd = estimate_cost_usd(config.model_id, usage)
        status = "end_turn"
        error = None
        if hooks.cancel_requested:
            status = "cancelled"
            error = "Task cancelled by user"
        elif hooks.budget_exceeded:
            status = "error_max_budget_usd"
            error = "Agent session error (subtype='error_max_budget_usd')"
        elif strands_result.stop_reason == "limit_turns":
            status = "error_max_turns"
            error = "Agent session error (subtype='error_max_turns')"
        elif strands_result.stop_reason not in {"end_turn", "stop_sequence"}:
            status = "error"
            error = f"Strands stopped with reason {strands_result.stop_reason!r}"

        duration_ms = int((monotonic() - started) * 1000)
        duration_api_ms = int(strands_result.metrics.accumulated_metrics.get("latencyMs", 0) or 0)
        result = AgentResult(
            status=status,
            turns=hooks.turns,
            num_turns=hooks.turns,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            duration_api_ms=duration_api_ms,
            session_id=agent.agent_id,
            error=error,
            usage=usage,
            result_text=str(strands_result).strip(),
            clarification_question=clarification_state.get("question", ""),
        )
        if request.trajectory is not None:
            request.trajectory.write_result(
                subtype=result.status,
                num_turns=result.num_turns,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
                duration_api_ms=result.duration_api_ms,
                session_id=result.session_id,
                usage=result.usage,
            )
        log(
            "DONE",
            f"status={result.status} turns={result.turns} "
            f"cost=${result.cost_usd or 0:.4f} duration={result.duration_ms / 1000:.1f}s",
        )
        return result
