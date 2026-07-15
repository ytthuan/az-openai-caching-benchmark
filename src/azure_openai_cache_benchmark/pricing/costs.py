from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..constants import PROBE_ATTEMPTS
from ..specs import RequestSpec
from ..tokens import estimate_tokens
from .models import PriceBook


def compute_cost(
    usage: Mapping[str, int],
    price_book: PriceBook,
) -> dict[str, float]:
    uncached_input = int(usage["uncached_input_tokens"])
    cached_input = int(usage["cached_tokens"])
    output_tokens = int(usage["output_tokens"])
    uncached_input_usd = (
        uncached_input
        * price_book.input_usd_per_million
        / 1_000_000
    )
    cached_input_usd = (
        cached_input
        * price_book.cached_input_usd_per_million
        / 1_000_000
    )
    output_usd = (
        output_tokens
        * price_book.output_usd_per_million
        / 1_000_000
    )
    no_cache_input_usd = (
        int(usage["input_tokens"])
        * price_book.input_usd_per_million
        / 1_000_000
    )
    actual_input_usd = uncached_input_usd + cached_input_usd
    actual_usd = actual_input_usd + output_usd
    no_cache_usd = no_cache_input_usd + output_usd
    input_savings_usd = no_cache_input_usd - actual_input_usd
    savings_usd = no_cache_usd - actual_usd
    return {
        "uncached_input_usd": uncached_input_usd,
        "cached_input_usd": cached_input_usd,
        "actual_input_usd": actual_input_usd,
        "no_cache_input_usd": no_cache_input_usd,
        "input_savings_usd": input_savings_usd,
        "input_savings_rate": (
            input_savings_usd / no_cache_input_usd
            if no_cache_input_usd > 0
            else 0.0
        ),
        "output_usd": output_usd,
        "actual_usd": actual_usd,
        "no_cache_usd": no_cache_usd,
        "savings_usd": savings_usd,
        "total_savings_rate": (
            savings_usd / no_cache_usd if no_cache_usd > 0 else 0.0
        ),
    }


def expected_cost_envelope(
    specs: Sequence[RequestSpec],
    price_book: PriceBook | None,
    model: str,
    max_attempts: int,
) -> dict[str, Any] | None:
    if price_book is None:
        return None
    planned_input = sum(
        spec.cacheable_prefix_tokens_estimate
        + estimate_tokens(spec.input_text, model)
        for spec in specs
    )
    planned_output = sum(spec.max_output_tokens for spec in specs)
    reserve_attempts = max(0, max_attempts - len(specs))
    maximum_input = max(
        (
            spec.cacheable_prefix_tokens_estimate
            + estimate_tokens(spec.input_text, model)
            for spec in specs
        ),
        default=0,
    )
    maximum_output = max(
        (spec.max_output_tokens for spec in specs),
        default=0,
    )
    reserve_input = reserve_attempts * maximum_input
    reserve_output = reserve_attempts * maximum_output
    estimated_input = planned_input + reserve_input
    output_ceiling = planned_output + reserve_output
    no_cache_input_usd = (
        estimated_input
        * price_book.input_usd_per_million
        / 1_000_000
    )
    maximum_output_usd = (
        output_ceiling
        * price_book.output_usd_per_million
        / 1_000_000
    )
    return {
        "planned_attempts": len(specs),
        "reserve_attempts": reserve_attempts,
        "capability_probe_attempts": PROBE_ATTEMPTS,
        "retry_reserve_attempts": max(
            0,
            reserve_attempts - PROBE_ATTEMPTS,
        ),
        "planned_estimated_input_tokens": planned_input,
        "reserve_input_tokens_ceiling": reserve_input,
        "estimated_input_tokens": estimated_input,
        "planned_maximum_output_tokens": planned_output,
        "reserve_output_tokens_ceiling": reserve_output,
        "maximum_output_tokens": output_ceiling,
        "no_cache_input_usd": no_cache_input_usd,
        "maximum_output_usd": maximum_output_usd,
        "conservative_total_usd": (
            no_cache_input_usd + maximum_output_usd
        ),
        "note": (
            "Hard-cap-aware no-cache ceiling. Reserve attempts use the "
            "largest planned request and output cap."
        ),
    }
