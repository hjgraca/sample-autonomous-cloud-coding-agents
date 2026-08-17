from typing import ClassVar

from strands_hooks import policy_tool_input, policy_tool_name, usage_from_metrics


class _Metrics:
    accumulated_usage: ClassVar = {
        "inputTokens": 12,
        "outputTokens": 3,
        "cacheReadInputTokens": 4,
        "cacheWriteInputTokens": 2,
    }


def test_neutral_tool_names_map_to_existing_cedar_vocabulary():
    assert policy_tool_name("shell") == "Bash"
    assert policy_tool_name("write_file") == "Write"
    assert policy_tool_name("custom_mcp_tool") == "custom_mcp_tool"


def test_neutral_file_path_maps_to_existing_cedar_input_vocabulary():
    assert policy_tool_input(
        "write_file",
        {"path": ".github/workflows/deploy.yml", "content": "jobs: {}"},
    ) == {
        "path": ".github/workflows/deploy.yml",
        "file_path": ".github/workflows/deploy.yml",
        "content": "jobs: {}",
    }


def test_non_file_tool_input_is_unchanged():
    assert policy_tool_input("shell", {"command": "git status"}) == {"command": "git status"}


def test_strands_usage_is_normalized_to_public_result_shape():
    usage = usage_from_metrics(_Metrics())
    assert usage.input_tokens == 12
    assert usage.output_tokens == 3
    assert usage.cache_read_input_tokens == 4
    assert usage.cache_creation_input_tokens == 2
