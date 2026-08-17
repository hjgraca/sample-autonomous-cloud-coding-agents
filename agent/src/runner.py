"""Provider-neutral agent invocation entry point."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from config import AGENT_WORKSPACE
from harness import HarnessRequest
from progress_writer import _ProgressWriter
from shell import log
from strands_harness import StrandsHarness
from telemetry import _TrajectoryWriter

if TYPE_CHECKING:
    from harness import AgentHarness
    from models import AgentResult, TaskConfig


def _setup_agent_env(config: TaskConfig) -> tuple[str | None, str | None]:
    """Configure environment inherited by repository subprocesses."""
    os.environ["AWS_REGION"] = config.aws_region
    os.environ["GITHUB_TOKEN"] = config.github_token
    os.environ["GH_TOKEN"] = config.github_token

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    otlp_protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
    for key in [key for key in os.environ if key.startswith("OTEL_")]:
        del os.environ[key]
    pythonpath = os.environ.get("PYTHONPATH", "")
    if pythonpath:
        cleaned = os.pathsep.join(
            path for path in pythonpath.split(os.pathsep) if "opentelemetry" not in path
        )
        if cleaned:
            os.environ["PYTHONPATH"] = cleaned
        else:
            os.environ.pop("PYTHONPATH", None)

    return otlp_endpoint, otlp_protocol


def _initialize_policy_engine(
    *,
    config: TaskConfig,
    progress: _ProgressWriter,
) -> Any:
    """Construct the task-scoped Cedar policy engine."""
    from policy import PolicyEngine

    engine_kwargs: dict[str, Any] = {}
    if config.initial_approvals:
        engine_kwargs["initial_approvals"] = list(config.initial_approvals)
    if config.approval_timeout_s is not None:
        engine_kwargs["task_default_timeout_s"] = config.approval_timeout_s
    if config.initial_approval_gate_count:
        engine_kwargs["initial_approval_gate_count"] = config.initial_approval_gate_count
    if config.approval_gate_cap is not None:
        engine_kwargs["approval_gate_cap"] = config.approval_gate_cap

    cedar_policies = config.cedar_policies
    engine = PolicyEngine(
        task_type=config.policy_principal,
        repo=config.repo_url,
        read_only=config.read_only,
        extra_policies=cedar_policies if cedar_policies else None,
        **engine_kwargs,
    )
    cap_log = (
        f" approval_gate_cap={config.approval_gate_cap} approval_gate_cap_source=threaded"
        if config.approval_gate_cap is not None
        else " approval_gate_cap=unset approval_gate_cap_source=engine_default"
    )
    log(
        "AGENT",
        f"Cedar policy engine initialized for task_type={config.policy_principal}"
        + (f" with {len(cedar_policies)} extra policies" if cedar_policies else "")
        + cap_log,
    )
    progress.write_approval_pre_approvals_loaded(
        count=len(config.initial_approvals),
        scopes=list(config.initial_approvals),
    )
    return engine


_FULL_TOOL_SURFACE = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"]
_WRITE_TOOLS = frozenset(("Write", "Edit"))
_TOOL_NAMES = {
    "Bash": "shell",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Glob": "glob_files",
    "Grep": "search_text",
    "WebFetch": "fetch_url",
}
_NO_CLARIFICATION_WORKFLOW_IDS = frozenset(
    (
        "coding/pr-iteration-v1",
        "coding/pr-review-v1",
        "coding/restack-v1",
        "default/agent-v1",
        "web/research-v1",
    )
)


def _resolve_allowed_tools(config: TaskConfig) -> list[str]:
    """Resolve workflow tool names into the provider-neutral harness vocabulary."""
    configured = list(config.allowed_tools) if config.allowed_tools else list(_FULL_TOOL_SURFACE)
    if config.read_only:
        configured = [name for name in configured if name not in _WRITE_TOOLS]
    return [_TOOL_NAMES[name] for name in configured if name in _TOOL_NAMES]


def _build_harness() -> AgentHarness:
    return StrandsHarness()


async def run_agent(
    prompt: str,
    system_prompt: str,
    config: TaskConfig,
    cwd: str = AGENT_WORKSPACE,
    trajectory: _TrajectoryWriter | None = None,
) -> AgentResult:
    """Execute one agent session through ABCA's configured harness."""
    _setup_agent_env(config)
    if trajectory is None:
        trajectory = _TrajectoryWriter(config.task_id or "unknown")
    progress = _ProgressWriter(
        config.task_id or "unknown",
        trace=config.trace,
        user_id=config.user_id,
        repo=config.repo_url,
    )
    policy_engine = _initialize_policy_engine(config=config, progress=progress)
    workflow_id = (config.resolved_workflow or {}).get("id", "")
    offer_clarification = not config.read_only and workflow_id not in _NO_CLARIFICATION_WORKFLOW_IDS
    request = HarnessRequest(
        prompt=prompt,
        system_prompt=system_prompt,
        config=config,
        cwd=cwd,
        enabled_tools=frozenset(_resolve_allowed_tools(config)),
        offer_clarification=offer_clarification,
        policy_engine=policy_engine,
        progress=progress,
        trajectory=trajectory,
    )
    return await _build_harness().run(request)
