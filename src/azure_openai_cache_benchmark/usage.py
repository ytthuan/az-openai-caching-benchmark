from __future__ import annotations

from typing import Any, Mapping

from .constants import (
    MIN_CACHEABLE_TOKENS,
    WARM_PREFIX_EFFICIENCY_FLOOR,
)
from .errors import ValidationError


def usage_from_response_payload(
    response_payload: Mapping[str, Any],
) -> dict[str, int]:
    usage = response_payload.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)
    total_tokens = int(
        usage.get("total_tokens") or input_tokens + output_tokens
    )
    if min(
        input_tokens,
        cached_tokens,
        output_tokens,
        reasoning_tokens,
        total_tokens,
    ) < 0:
        raise ValidationError("Token usage cannot be negative.")
    if cached_tokens > input_tokens:
        raise ValidationError("cached_tokens exceeds input_tokens.")
    if reasoning_tokens > output_tokens:
        raise ValidationError("reasoning_tokens exceeds output_tokens.")
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "uncached_input_tokens": input_tokens - cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def cacheable_prefix_efficiency(
    cached_tokens: int,
    cacheable_prefix_tokens_estimate: int,
) -> float | None:
    if cacheable_prefix_tokens_estimate <= 0:
        return None
    return cached_tokens / cacheable_prefix_tokens_estimate


def cache_state_is_valid(
    expected_state: str,
    *,
    cached_tokens: int,
    cacheable_prefix_tokens_estimate: int,
) -> bool:
    if expected_state in {"cold", "under_threshold"}:
        return cached_tokens == 0
    if expected_state == "warm":
        efficiency = cacheable_prefix_efficiency(
            cached_tokens,
            cacheable_prefix_tokens_estimate,
        )
        return (
            cached_tokens >= MIN_CACHEABLE_TOKENS
            and efficiency is not None
            and efficiency >= WARM_PREFIX_EFFICIENCY_FLOOR
        )
    return True
