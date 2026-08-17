"""Bedrock model pricing used for task cost estimates and USD budget limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import TokenUsage

_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")
_TOKENS_PER_MILLION = 1_000_000


class UnknownModelPricingError(ValueError):
    """Raised when a budgeted task selects a model without known pricing."""


@dataclass(frozen=True)
class ModelPricing:
    """USD rates per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


_PRICING: dict[str, ModelPricing] = {
    "anthropic.claude-sonnet-4-6": ModelPricing(3.0, 15.0, 0.30, 3.75),
    "anthropic.claude-opus-4-20250514-v1:0": ModelPricing(15.0, 75.0, 1.50, 18.75),
    "anthropic.claude-opus-4-8": ModelPricing(5.0, 25.0, 0.50, 6.25),
    "anthropic.claude-opus-5": ModelPricing(5.0, 25.0, 0.50, 6.25),
    "anthropic.claude-haiku-4-5-20251001-v1:0": ModelPricing(1.0, 5.0, 0.10, 1.25),
}


def normalize_model_id(model_id: str) -> str:
    """Strip a cross-Region inference-profile prefix from a model id."""
    normalized = model_id.strip()
    for prefix in _PROFILE_PREFIXES:
        if normalized.startswith(prefix):
            return normalized.removeprefix(prefix)
    return normalized


def pricing_for(model_id: str) -> ModelPricing | None:
    """Return pricing for a foundation model or inference profile id."""
    return _PRICING.get(normalize_model_id(model_id))


def require_pricing(model_id: str) -> ModelPricing:
    """Return known pricing or reject budget enforcement for this model."""
    pricing = pricing_for(model_id)
    if pricing is None:
        raise UnknownModelPricingError(
            f"max_budget_usd requires known pricing for model {model_id!r}"
        )
    return pricing


def estimate_cost_usd(model_id: str, usage: TokenUsage) -> float | None:
    """Estimate list-price cost from normalized Strands/Bedrock token usage."""
    pricing = pricing_for(model_id)
    if pricing is None:
        return None

    cache_read = max(usage.cache_read_input_tokens, 0)
    cache_write = max(usage.cache_creation_input_tokens, 0)
    uncached_input = max(usage.input_tokens - cache_read - cache_write, 0)
    total = (
        uncached_input * pricing.input
        + max(usage.output_tokens, 0) * pricing.output
        + cache_read * pricing.cache_read
        + cache_write * pricing.cache_write
    )
    return total / _TOKENS_PER_MILLION
