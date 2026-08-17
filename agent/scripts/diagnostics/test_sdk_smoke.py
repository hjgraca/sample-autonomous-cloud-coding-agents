#!/usr/bin/env python3
"""Live Strands-to-Bedrock smoke test.

Run from ``agent/`` with AWS credentials and an optional ``MODEL_ID``:

    MODEL_ID=us.anthropic.claude-sonnet-4-6 uv run \
      python scripts/diagnostics/test_sdk_smoke.py
"""

from __future__ import annotations

import asyncio
import os

import boto3
from strands import Agent
from strands.models import BedrockModel


async def main() -> None:
    model_id = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    model = BedrockModel(
        boto_session=boto3.Session(region_name=os.environ.get("AWS_REGION", "us-east-1")),
        model_id=model_id,
    )
    agent = Agent(model=model, tools=[], callback_handler=None)
    result = None
    async for event in agent.stream_async("Reply with exactly: STRANDS_OK", limits={"turns": 1}):
        if "result" in event:
            result = event["result"]
    if result is None:
        raise RuntimeError("Strands stream returned no AgentResult")
    print(str(result).strip())
    print(result.metrics.get_summary()["accumulated_usage"])


if __name__ == "__main__":
    asyncio.run(main())
