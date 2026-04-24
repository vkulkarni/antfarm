"""Model pricing and cost computation for Antfarm mission budgets.

Prices are expressed in USD per 1M tokens. Claude Anthropic APIs report four
token buckets in a Stop-hook usage payload:

- ``input_tokens``: ordinary (non-cached) input. Note that Claude reports this
  DISJOINT from the cache fields — ``input_tokens`` does NOT include cache
  read/creation tokens. Do NOT subtract cache tokens from input tokens.
- ``output_tokens``: tokens generated in the assistant response.
- ``cache_read_tokens``: tokens served from prompt cache (cheaper than input).
- ``cache_creation_tokens``: tokens written to prompt cache (more expensive
  than input, one-time cost).

``resolve_model`` picks the longest-prefix match among the canonical keys in
``PRICES`` so that specific minor versions (e.g. ``claude-sonnet-4-7``) take
precedence over generic prefixes (e.g. ``claude-sonnet``). Unknown model IDs
fall back to ``FALLBACK_RATES`` and emit a single WARNING per model ID.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price table (USD per 1M tokens)
# ---------------------------------------------------------------------------

# Canonical model prefix → {input, output, cache_read, cache_creation} USD/MTok.
PRICES: dict[str, dict[str, float]] = {
    # Sonnet tier
    "claude-sonnet-4-5": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_creation": 3.75,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_creation": 3.75,
    },
    "claude-sonnet-4-7": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_creation": 3.75,
    },
    # Opus tier
    "claude-opus-4": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
    "claude-opus-4-5": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
    "claude-opus-4-6": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
    # Haiku tier
    "claude-haiku-4": {
        "input": 0.80,
        "output": 4.00,
        "cache_read": 0.08,
        "cache_creation": 1.00,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.00,
        "cache_read": 0.08,
        "cache_creation": 1.00,
    },
}


DEFAULT_MODEL = "claude-sonnet-4-6"


# Fallback rates used when a model ID does not match any canonical prefix.
# Chosen conservatively at the Sonnet tier so unknown models cannot silently
# under-report cost.
FALLBACK_RATES: dict[str, float] = {
    "input": 3.00,
    "output": 15.00,
    "cache_read": 0.30,
    "cache_creation": 3.75,
}


# Models that have already been warned about — prevents log spam.
_WARNED_MODELS: set[str] = set()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def resolve_model(raw: str) -> str:
    """Return the canonical price-table key for a raw model string.

    Matching is case-insensitive and uses longest-prefix semantics so that
    ``"claude-sonnet-4-7-1m"`` resolves to ``"claude-sonnet-4-7"`` rather than
    a shorter prefix.

    Returns the empty string when no prefix matches — the caller can then
    decide to use ``FALLBACK_RATES``.
    """
    if not raw:
        return ""
    needle = raw.lower()
    best = ""
    for key in PRICES:
        if needle.startswith(key) and len(key) > len(best):
            best = key
    return best


def compute_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Compute USD cost for a single usage event.

    Claude reports ``input_tokens`` disjoint from the cache fields — do NOT
    subtract ``cache_read_tokens`` or ``cache_creation_tokens`` from
    ``input_tokens``.

    Unknown models log a single WARNING and fall back to ``FALLBACK_RATES``.

    Args:
        model: Raw model identifier (as reported by the Claude transcript).
        input_tokens: Non-cached input tokens.
        output_tokens: Generated output tokens.
        cache_read_tokens: Tokens served from prompt cache.
        cache_creation_tokens: Tokens written into prompt cache.

    Returns:
        Total cost in USD.
    """
    key = resolve_model(model)
    if key:
        rates = PRICES[key]
    else:
        if model and model not in _WARNED_MODELS:
            logger.warning("pricing: unknown model '%s'; falling back to Sonnet rates", model)
            _WARNED_MODELS.add(model)
        rates = FALLBACK_RATES

    # Prices are USD per 1M tokens.
    scale = 1_000_000.0
    cost = (
        (input_tokens * rates["input"])
        + (output_tokens * rates["output"])
        + (cache_read_tokens * rates["cache_read"])
        + (cache_creation_tokens * rates["cache_creation"])
    ) / scale
    return cost
