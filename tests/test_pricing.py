"""Tests for antfarm.core.pricing."""

from __future__ import annotations

import logging

from antfarm.core import pricing
from antfarm.core.pricing import (
    DEFAULT_MODEL,
    FALLBACK_RATES,
    PRICES,
    compute_cost,
    resolve_model,
)

# ---------------------------------------------------------------------------
# Price table sanity
# ---------------------------------------------------------------------------


def test_default_model_is_in_price_table():
    assert DEFAULT_MODEL in PRICES


def test_every_price_entry_has_all_rate_keys():
    expected_keys = {"input", "output", "cache_read", "cache_creation"}
    for key, rates in PRICES.items():
        assert set(rates.keys()) == expected_keys, f"{key} missing rate keys"


# ---------------------------------------------------------------------------
# compute_cost
# ---------------------------------------------------------------------------


def test_sonnet_input_only_cost():
    """1M input tokens on Sonnet should cost $3.00 exactly."""
    cost = compute_cost(
        model="claude-sonnet-4-7",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost == 3.00


def test_cache_read_cheaper_than_input():
    sonnet = PRICES["claude-sonnet-4-7"]
    assert sonnet["cache_read"] < sonnet["input"]

    input_cost = compute_cost(
        model="claude-sonnet-4-7",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    cache_read_cost = compute_cost(
        model="claude-sonnet-4-7",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cache_read_cost < input_cost


def test_cache_creation_more_expensive_than_input():
    sonnet = PRICES["claude-sonnet-4-7"]
    assert sonnet["cache_creation"] > sonnet["input"]

    input_cost = compute_cost(
        model="claude-sonnet-4-7",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    cache_creation_cost = compute_cost(
        model="claude-sonnet-4-7",
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=1_000_000,
    )
    assert cache_creation_cost > input_cost


def test_output_rate():
    """1M output tokens on Sonnet should cost $15.00 exactly."""
    cost = compute_cost(
        model="claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=1_000_000,
    )
    assert cost == 15.00


def test_opus_costs_more_than_sonnet_for_same_tokens():
    opus = compute_cost(model="claude-opus-4-7", input_tokens=1000, output_tokens=1000)
    sonnet = compute_cost(model="claude-sonnet-4-7", input_tokens=1000, output_tokens=1000)
    assert opus > sonnet


def test_haiku_cheaper_than_sonnet():
    sonnet = compute_cost(model="claude-sonnet-4-7", input_tokens=1000, output_tokens=1000)
    haiku = compute_cost(model="claude-haiku-4-5", input_tokens=1000, output_tokens=1000)
    assert haiku < sonnet


def test_zero_tokens_zero_cost():
    assert compute_cost(model="claude-sonnet-4-7", input_tokens=0, output_tokens=0) == 0.0


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


def test_resolve_model_prefix_match():
    # Exact match
    assert resolve_model("claude-sonnet-4-7") == "claude-sonnet-4-7"
    # Longer suffix matches longest prefix (4-7 wins over 4-6)
    assert resolve_model("claude-sonnet-4-7-1m") == "claude-sonnet-4-7"
    # Case-insensitive
    assert resolve_model("CLAUDE-SONNET-4-7") == "claude-sonnet-4-7"


def test_resolve_model_distinguishes_families():
    assert resolve_model("claude-opus-4-7") == "claude-opus-4-7"
    assert resolve_model("claude-haiku-4-5") == "claude-haiku-4-5"


def test_resolve_model_fallback_for_unknown_returns_empty():
    assert resolve_model("gpt-4") == ""
    assert resolve_model("") == ""


def test_compute_cost_fallback_for_unknown_warns(caplog):
    """Unknown model uses FALLBACK_RATES and logs a WARNING (once)."""
    # Reset warned set to ensure this test sees the warning.
    pricing._WARNED_MODELS.discard("mystery-model-v9")

    with caplog.at_level(logging.WARNING, logger="antfarm.core.pricing"):
        cost = compute_cost(
            model="mystery-model-v9",
            input_tokens=1_000_000,
            output_tokens=0,
        )
    assert cost == FALLBACK_RATES["input"]
    assert "mystery-model-v9" in caplog.text


def test_compute_cost_unknown_warns_only_once(caplog):
    pricing._WARNED_MODELS.discard("double-warn-test")
    with caplog.at_level(logging.WARNING, logger="antfarm.core.pricing"):
        compute_cost(model="double-warn-test", input_tokens=10, output_tokens=0)
        compute_cost(model="double-warn-test", input_tokens=10, output_tokens=0)
    # Count number of warnings referencing our model string
    count = sum(1 for rec in caplog.records if "double-warn-test" in rec.getMessage())
    assert count == 1
