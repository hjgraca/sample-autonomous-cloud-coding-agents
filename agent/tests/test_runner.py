"""Unit tests for the provider-neutral runner entry point."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from models import AgentResult, TaskConfig
from runner import (
    _FULL_TOOL_SURFACE,
    _initialize_policy_engine,
    _resolve_allowed_tools,
    _setup_agent_env,
    run_agent,
)


def _config(**overrides: Any) -> TaskConfig:
    values: dict[str, Any] = {
        "repo_url": "owner/repo",
        "github_token": "ghp_test",
        "aws_region": "us-east-1",
        "task_id": "t-runner-1",
    }
    values.update(overrides)
    return TaskConfig(**values)


class TestInitializePolicyEngine:
    @patch("policy.PolicyEngine")
    def test_threads_task_policy_configuration(self, policy_engine):
        config = _config(
            initial_approvals=["tool_type:Read"],
            approval_timeout_s=600,
            initial_approval_gate_count=17,
            approval_gate_cap=200,
            read_only=True,
            user_id="user-1",
        )
        progress = MagicMock()

        engine = _initialize_policy_engine(config=config, progress=progress)

        assert engine is policy_engine.return_value
        assert policy_engine.call_args.kwargs["read_only"] is True
        assert policy_engine.call_args.kwargs["initial_approvals"] == ["tool_type:Read"]
        assert policy_engine.call_args.kwargs["task_default_timeout_s"] == 600
        assert policy_engine.call_args.kwargs["initial_approval_gate_count"] == 17
        assert policy_engine.call_args.kwargs["approval_gate_cap"] == 200
        progress.write_approval_pre_approvals_loaded.assert_called_once_with(
            count=1,
            scopes=["tool_type:Read"],
        )

    @patch("policy.PolicyEngine")
    def test_omits_optional_engine_defaults(self, policy_engine):
        _initialize_policy_engine(config=_config(), progress=MagicMock())
        kwargs = policy_engine.call_args.kwargs
        assert "initial_approvals" not in kwargs
        assert "task_default_timeout_s" not in kwargs
        assert "initial_approval_gate_count" not in kwargs
        assert "approval_gate_cap" not in kwargs


class TestResolveAllowedTools:
    def test_full_surface_maps_to_neutral_names(self):
        resolved = _resolve_allowed_tools(_config(allowed_tools=[]))
        assert _FULL_TOOL_SURFACE == ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"]
        assert resolved == [
            "shell",
            "read_file",
            "write_file",
            "edit_file",
            "glob_files",
            "search_text",
            "fetch_url",
        ]

    def test_read_only_removes_mutating_tools(self):
        config = _config(
            allowed_tools=["Bash", "Read", "Write", "Edit", "Glob"],
            read_only=True,
        )
        assert _resolve_allowed_tools(config) == ["shell", "read_file", "glob_files"]

    def test_restricted_workflow_is_not_widened(self):
        config = _config(allowed_tools=["Read", "Glob", "Grep", "WebFetch"])
        assert _resolve_allowed_tools(config) == [
            "read_file",
            "glob_files",
            "search_text",
            "fetch_url",
        ]


def test_setup_agent_env_sets_shared_env(monkeypatch):
    _setup_agent_env(_config(model_id="us.anthropic.claude-opus-4-8"))

    assert os.environ["AWS_REGION"] == "us-east-1"
    assert os.environ["GH_TOKEN"] == _config().github_token


def test_run_agent_delegates_through_harness_boundary():
    expected = AgentResult(status="end_turn", turns=1)
    harness = MagicMock()
    harness.run = AsyncMock(return_value=expected)

    with (
        patch("runner._build_harness", return_value=harness),
        patch("runner._initialize_policy_engine", return_value=MagicMock()),
        patch("runner._ProgressWriter"),
        patch("runner._TrajectoryWriter"),
    ):
        result = asyncio.run(
            run_agent(
                "do work",
                "system",
                _config(allowed_tools=["Read"]),
                cwd="/tmp/repo",
            )
        )

    assert result is expected
    awaited = harness.run.await_args
    assert awaited is not None
    request = awaited.args[0]
    assert request.prompt == "do work"
    assert request.enabled_tools == frozenset({"read_file"})
    assert request.config.model_id == "us.anthropic.claude-opus-4-8"
