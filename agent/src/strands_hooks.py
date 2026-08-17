"""Adapters from Strands lifecycle hooks to ABCA policy and observability."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from hooks import post_tool_use_hook, pre_tool_use_hook, stop_hook
from model_pricing import estimate_cost_usd
from models import TokenUsage
from shell import log, log_error_cw, truncate
from stuck_guard import StuckGuard

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policy import PolicyEngine
    from progress_writer import _ProgressWriter
    from telemetry import _TrajectoryWriter

_POLICY_TOOL_NAMES = {
    "shell": "Bash",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "glob_files": "Glob",
    "search_text": "Grep",
    "fetch_url": "WebFetch",
}


def policy_tool_name(tool_name: str) -> str:
    """Map neutral runtime tool names onto the stable Cedar action vocabulary."""
    return _POLICY_TOOL_NAMES.get(tool_name, tool_name)


def policy_tool_input(tool_name: str, tool_input: Mapping[str, Any]) -> dict[str, Any]:
    """Map neutral tool arguments onto the stable Cedar input vocabulary."""
    normalized = dict(tool_input)
    if tool_name in {"read_file", "write_file", "edit_file"}:
        path = normalized.get("path")
        if path is not None and "file_path" not in normalized:
            normalized["file_path"] = path
    return normalized


def usage_from_metrics(metrics: Any) -> TokenUsage:
    """Normalize Strands' Bedrock-shaped accumulated usage."""
    raw = getattr(metrics, "accumulated_usage", {}) or {}
    return TokenUsage(
        input_tokens=int(raw.get("inputTokens", 0) or 0),
        output_tokens=int(raw.get("outputTokens", 0) or 0),
        cache_read_input_tokens=int(raw.get("cacheReadInputTokens", 0) or 0),
        cache_creation_input_tokens=int(raw.get("cacheWriteInputTokens", 0) or 0),
    )


def _tool_result_text(result: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for block in result.get("content", []):
        if "text" in block:
            parts.append(str(block["text"]))
        elif "json" in block:
            parts.append(str(block["json"]))
        else:
            parts.append(str(block))
    return "\n".join(parts)


def _assistant_content(message: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    text: list[str] = []
    thinking: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in message.get("content", []):
        if "text" in block:
            text.append(str(block["text"]))
        elif "reasoningContent" in block:
            reasoning = block["reasoningContent"]
            if isinstance(reasoning, dict):
                value = reasoning.get("reasoningText", {}).get("text", "")
                if value:
                    thinking.append(str(value))
        elif "toolUse" in block:
            tool_use = block["toolUse"]
            tool_calls.append(
                {
                    "name": str(tool_use.get("name", "unknown")),
                    "input": tool_use.get("input", {}),
                }
            )
    return "\n".join(text), "\n".join(thinking), tool_calls


class StrandsHooks(HookProvider):
    """Per-task Strands hook provider backed by ABCA's existing controls."""

    def __init__(
        self,
        *,
        engine: PolicyEngine,
        model_id: str,
        max_budget_usd: float | None,
        task_id: str,
        user_id: str,
        repo_url: str,
        progress: _ProgressWriter,
        trajectory: _TrajectoryWriter | None,
        clarification_state: dict[str, str],
    ) -> None:
        self.engine = engine
        self.model_id = model_id
        self.max_budget_usd = max_budget_usd
        self.task_id = task_id
        self.user_id = user_id
        self.repo_url = repo_url
        self.progress = progress
        self.trajectory = trajectory
        self.clarification_state = clarification_state
        self.stuck_guard = StuckGuard()
        self.turns = 0
        self.cancel_requested = False
        self.budget_exceeded = False
        self.last_cost_usd: float | None = None

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)
        registry.add_callback(BeforeModelCallEvent, self.before_model_call)
        registry.add_callback(AfterModelCallEvent, self.after_model_call)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    async def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        runtime_name = str(event.tool_use.get("name", "unknown"))
        runtime_input = event.tool_use.get("input", {})
        if runtime_name == "request_clarification":
            question = str(runtime_input.get("question", "")).strip()
            self.clarification_state["question"] = question or " "
        policy_name = policy_tool_name(runtime_name)
        hook_input = {
            "tool_name": policy_name,
            "tool_input": policy_tool_input(runtime_name, runtime_input),
        }
        try:
            response = await pre_tool_use_hook(
                hook_input,
                event.tool_use.get("toolUseId"),
                None,
                engine=self.engine,
                trajectory=self.trajectory,
                task_id=self.task_id or None,
                user_id=self.user_id or None,
                progress=self.progress,
                repo_url=self.repo_url or None,
            )
            specific = response.get("hookSpecificOutput", {})
            if specific.get("permissionDecision") == "deny":
                event.cancel_tool = specific.get("permissionDecisionReason") or "Denied by policy"
        except Exception as exc:
            log(
                "ERROR",
                f"BeforeToolCall adapter crashed: {type(exc).__name__}: {exc}",
            )
            log_error_cw(
                f"BeforeToolCall adapter crashed: {type(exc).__name__}: {exc}",
                task_id=self.task_id or None,
            )
            event.cancel_tool = "Hook error - fail-closed deny"

    async def after_tool_call(self, event: AfterToolCallEvent) -> None:
        runtime_name = str(event.tool_use.get("name", "unknown"))
        runtime_input = event.tool_use.get("input", {})
        content = _tool_result_text(event.result)
        try:
            response = await post_tool_use_hook(
                {
                    "tool_name": policy_tool_name(runtime_name),
                    "tool_input": policy_tool_input(runtime_name, runtime_input),
                    "tool_response": content,
                },
                event.tool_use.get("toolUseId"),
                None,
                trajectory=self.trajectory,
                progress=self.progress,
                stuck_guard=self.stuck_guard,
            )
            updated = response.get("hookSpecificOutput", {}).get("updatedMCPToolOutput")
            if updated is not None:
                event.result = {
                    "toolUseId": event.result.get("toolUseId", event.tool_use.get("toolUseId", "")),
                    "status": event.result.get("status", "success"),
                    "content": [{"text": str(updated)}],
                }
                content = str(updated)
        except Exception as exc:
            log("ERROR", f"AfterToolCall adapter crashed: {type(exc).__name__}: {exc}")
            content = "[Output redacted: hook error - fail-closed]"
            event.result = {
                "toolUseId": event.result.get("toolUseId", event.tool_use.get("toolUseId", "")),
                "status": "error",
                "content": [{"text": content}],
            }

        is_error = event.result.get("status") == "error" or event.exception is not None
        self.progress.write_agent_tool_result(
            tool_name=policy_tool_name(runtime_name),
            is_error=is_error,
            content=content,
            turn=self.turns,
        )
        log("RESULT", f"[{'ERROR' if is_error else 'ok'}] {truncate(content)}")

    async def _between_turns_guidance(self) -> str | None:
        response = await stop_hook(
            {},
            None,
            None,
            task_id=self.task_id,
            progress=self.progress,
            engine=self.engine,
            stuck_guard=self.stuck_guard,
        )
        if response.get("continue_") is False:
            self.cancel_requested = True
            return None
        if response.get("decision") == "block":
            return str(response.get("reason") or "")
        return None

    async def before_model_call(self, event: BeforeModelCallEvent) -> None:
        if self.budget_exceeded:
            event.cancel = (
                f"USD budget reached (${self.last_cost_usd or 0:.4f} >= "
                f"${self.max_budget_usd or 0:.4f})"
            )
            return
        if self.turns:
            guidance = await self._between_turns_guidance()
            if self.cancel_requested:
                event.cancel = "Task cancelled by user"
            elif guidance:
                event.agent.messages.append({"role": "user", "content": [{"text": guidance}]})

    async def after_model_call(self, event: AfterModelCallEvent) -> None:
        if event.stop_response is None or self.cancel_requested or self.budget_exceeded:
            return

        self.turns += 1
        message = event.stop_response.message
        text, thinking, tool_calls = _assistant_content(message)
        reported_tool_calls = [
            {**call, "name": policy_tool_name(str(call["name"]))} for call in tool_calls
        ]
        model_name = self.model_id
        log("TURN", f"#{self.turns} (model: {model_name})")
        if thinking:
            log("THINK", truncate(thinking, 200))
        if text:
            print(text, flush=True)

        if self.trajectory:
            self.trajectory.write_turn(
                turn=self.turns,
                model=model_name,
                thinking=thinking,
                text=text,
                tool_calls=reported_tool_calls,
                tool_results=[],
            )
        self.progress.write_agent_turn(
            turn=self.turns,
            model=model_name,
            thinking=thinking,
            text=text,
            tool_calls_count=len(reported_tool_calls),
        )
        for call in reported_tool_calls:
            self.progress.write_agent_tool_call(
                tool_name=call["name"],
                tool_input=str(call.get("input", "")),
                turn=self.turns,
            )

        message_usage = message.get("metadata", {}).get("usage", {})
        accumulated = usage_from_metrics(event.agent.event_loop_metrics)
        current = TokenUsage(
            input_tokens=accumulated.input_tokens + int(message_usage.get("inputTokens", 0) or 0),
            output_tokens=accumulated.output_tokens
            + int(message_usage.get("outputTokens", 0) or 0),
            cache_read_input_tokens=accumulated.cache_read_input_tokens
            + int(message_usage.get("cacheReadInputTokens", 0) or 0),
            cache_creation_input_tokens=accumulated.cache_creation_input_tokens
            + int(message_usage.get("cacheWriteInputTokens", 0) or 0),
        )
        self.last_cost_usd = estimate_cost_usd(self.model_id, current)
        self.progress.write_agent_cost_update(
            cost_usd=self.last_cost_usd,
            input_tokens=current.input_tokens,
            output_tokens=current.output_tokens,
            turn=self.turns,
        )
        if (
            self.max_budget_usd is not None
            and self.last_cost_usd is not None
            and self.last_cost_usd >= self.max_budget_usd
        ):
            self.budget_exceeded = True

    async def after_invocation(self, event: AfterInvocationEvent) -> None:
        if event.result is None or event.result.stop_reason != "end_turn":
            return
        if self.cancel_requested or self.budget_exceeded:
            return
        guidance = await self._between_turns_guidance()
        if guidance and not self.cancel_requested:
            event.resume = guidance
