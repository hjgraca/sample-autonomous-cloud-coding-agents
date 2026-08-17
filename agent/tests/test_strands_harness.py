from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness import HarnessRequest
from model_pricing import UnknownModelPricingError
from models import TaskConfig
from strands_harness import StrandsHarness, _load_mcp_clients, _repo_discovery


class _FakeHooks:
    def __init__(self, **_kwargs):
        self.turns = 3
        self.cancel_requested = False
        self.budget_exceeded = False


class _FakeResult:
    def __init__(self, stop_reason: str = "end_turn"):
        self.stop_reason = stop_reason
        self.metrics = SimpleNamespace(
            accumulated_usage={"inputTokens": 1000, "outputTokens": 100},
            accumulated_metrics={"latencyMs": 250},
        )

    def __str__(self) -> str:
        return "completed"


class _FakeAgent:
    agent_id = "strands-agent-1"

    def __init__(self, result: _FakeResult | None):
        self.result = result
        self.cleaned = False

    async def stream_async(self, _prompt: str, **_kwargs):
        if self.result is not None:
            yield {"result": self.result}

    def cleanup(self) -> None:
        self.cleaned = True


def _request(**config_overrides) -> HarnessRequest:
    config_values = {
        "repo_url": "owner/repo",
        "github_token": "ghp_test",
        "aws_region": "us-east-1",
        "task_id": "task-1",
        "user_id": "user-1",
    }
    config_values.update(config_overrides)
    return HarnessRequest(
        prompt="implement the task",
        system_prompt="system",
        config=TaskConfig.model_validate(config_values),
        cwd="/tmp/repo",
        enabled_tools=frozenset({"read_file"}),
        offer_clarification=False,
        policy_engine=MagicMock(),
        progress=MagicMock(),
        trajectory=MagicMock(),
    )


@pytest.mark.parametrize(
    ("stop_reason", "cancelled", "budget_exceeded", "expected_status"),
    [
        ("end_turn", False, False, "end_turn"),
        ("limit_turns", False, False, "error_max_turns"),
        ("end_turn", True, False, "cancelled"),
        ("end_turn", False, True, "error_max_budget_usd"),
        ("content_filtered", False, False, "error"),
    ],
)
def test_maps_strands_stop_reasons_and_cleans_up(
    stop_reason, cancelled, budget_exceeded, expected_status
):
    hooks = _FakeHooks()
    hooks.cancel_requested = cancelled
    hooks.budget_exceeded = budget_exceeded
    agent = _FakeAgent(_FakeResult(stop_reason))
    scoped_session = object()

    with (
        patch("aws_session.get_session", return_value=scoped_session),
        patch("strands_harness.BedrockModel") as bedrock_model,
        patch("strands_harness.StrandsHooks", return_value=hooks),
        patch("strands_harness.Agent", return_value=agent),
        patch("strands_harness.build_coding_tools", return_value=[]),
        patch("strands_harness.build_gateway_tool", return_value=None),
        patch("strands_harness._load_mcp_clients", return_value=[]),
        patch("strands_harness.load_repo_instructions", return_value=""),
    ):
        result = asyncio.run(StrandsHarness().run(_request()))

    assert result.status == expected_status
    assert result.turns == 3
    assert result.session_id == "strands-agent-1"
    assert result.result_text == "completed"
    assert agent.cleaned is True
    assert bedrock_model.call_args.kwargs["boto_session"] is scoped_session


def test_stream_without_result_is_an_error_and_cleans_up():
    agent = _FakeAgent(None)
    with (
        patch("aws_session.get_session", return_value=object()),
        patch("strands_harness.BedrockModel"),
        patch("strands_harness.StrandsHooks", return_value=_FakeHooks()),
        patch("strands_harness.Agent", return_value=agent),
        patch("strands_harness.build_coding_tools", return_value=[]),
        patch("strands_harness.build_gateway_tool", return_value=None),
        patch("strands_harness._load_mcp_clients", return_value=[]),
        patch("strands_harness.load_repo_instructions", return_value=""),
    ):
        result = asyncio.run(StrandsHarness().run(_request()))

    assert result.status == "error"
    assert "without an AgentResult" in (result.error or "")
    assert agent.cleaned is True


def test_budgeted_unknown_model_is_rejected_before_model_construction():
    with (
        patch("strands_harness.BedrockModel") as bedrock_model,
        pytest.raises(UnknownModelPricingError),
    ):
        asyncio.run(StrandsHarness().run(_request(model_id="example.unknown", max_budget_usd=1.0)))
    bedrock_model.assert_not_called()


def test_mcp_loader_normalizes_legacy_transport_names(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        """
        {
          "mcpServers": {
            "local": {"type": "stdio", "command": "example"},
            "remote": {"type": "http", "url": "https://example.com/mcp"}
          }
        }
        """,
        encoding="utf-8",
    )
    loaded = [MagicMock(), MagicMock()]

    with patch("strands_harness.MCPClient.load_servers", return_value=loaded) as load:
        assert _load_mcp_clients(str(tmp_path)) == loaded

    servers = load.call_args.args[0]["mcpServers"]
    assert servers["local"]["transport"] == "stdio"
    assert servers["remote"]["transport"] == "streamable-http"
    assert "type" not in servers["remote"]


def test_mcp_loader_rejects_non_object_server_map(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        _load_mcp_clients(str(tmp_path))


def test_repo_discovery_honors_workflow_gates():
    config = TaskConfig.model_validate(
        {
            "repo_url": "owner/repo",
            "github_token": "ghp_test",
            "aws_region": "us-east-1",
            "resolved_workflow": {
                "repo_config": {
                    "discover": True,
                    "ignore": ["mcp", "claude_md"],
                }
            },
        }
    )

    assert _repo_discovery(config) == (
        True,
        frozenset({"mcp", "claude_md"}),
    )


def test_repo_discovery_is_disabled_for_repoless_tasks():
    config = TaskConfig.model_validate(
        {
            "repo_url": "",
            "github_token": "",
            "aws_region": "us-east-1",
            "requires_repo": False,
            "resolved_workflow": {"repo_config": {"discover": True}},
        }
    )

    assert _repo_discovery(config) == (False, frozenset())
