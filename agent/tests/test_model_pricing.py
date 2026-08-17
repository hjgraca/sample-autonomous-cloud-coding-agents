from model_pricing import (
    UnknownModelPricingError,
    estimate_cost_usd,
    normalize_model_id,
    require_pricing,
)
from models import TokenUsage


def test_normalizes_cross_region_profile():
    assert normalize_model_id("us.anthropic.claude-opus-4-8") == "anthropic.claude-opus-4-8"


def test_estimates_uncached_and_cached_tokens():
    usage = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_input_tokens=200_000,
        cache_creation_input_tokens=100_000,
    )
    # Opus 4.8: 700K*$5 + 100K*$25 + 200K*$0.50 + 100K*$6.25.
    assert estimate_cost_usd("us.anthropic.claude-opus-4-8", usage) == 6.725


def test_unknown_model_has_no_unbudgeted_estimate():
    assert estimate_cost_usd("example.unknown", TokenUsage(input_tokens=100)) is None


def test_unknown_model_is_rejected_for_budget_enforcement():
    try:
        require_pricing("example.unknown")
    except UnknownModelPricingError as exc:
        assert "example.unknown" in str(exc)
    else:
        raise AssertionError("unknown model pricing should fail")
