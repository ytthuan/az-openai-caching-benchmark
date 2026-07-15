from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from ..pricing.costs import compute_cost
from ..pricing.models import PriceBook
from ..usage import cache_state_is_valid


def percentile(
    values: Sequence[float],
    percentile_value: float,
) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def latest_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = dict(raw)
        logical_id = str(record.get("logical_request_id") or "")
        if not logical_id:
            continue
        previous = latest.get(logical_id)
        current_attempt = int(record.get("attempt_number") or 0)
        previous_attempt = int((previous or {}).get("attempt_number") or 0)
        if previous is None or current_attempt >= previous_attempt:
            latest[logical_id] = record
    return sorted(
        latest.values(),
        key=lambda record: (
            int(record.get("order") or 0),
            str(record.get("logical_request_id") or ""),
        ),
    )


def metric_summary(
    records: Sequence[Mapping[str, Any]],
    price_book: PriceBook | None,
) -> dict[str, Any]:
    completed = [
        record for record in records if record.get("status") == "completed"
    ]
    failed = [
        record for record in records if record.get("status") == "failed"
    ]
    usage = {
        key: sum(
            int((record.get("usage") or {}).get(key) or 0)
            for record in completed
        )
        for key in (
            "input_tokens",
            "cached_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
    }
    efficiencies = [
        float(record["cacheable_prefix_efficiency"])
        for record in completed
        if record.get("cacheable_prefix_efficiency") is not None
        and int((record.get("usage") or {}).get("cached_tokens") or 0) > 0
    ]
    substantive_hits = sum(
        1
        for record in completed
        if cache_state_is_valid(
            "warm",
            cached_tokens=int(
                (record.get("usage") or {}).get("cached_tokens") or 0
            ),
            cacheable_prefix_tokens_estimate=int(
                record.get("cacheable_prefix_tokens_estimate") or 0
            ),
        )
    )
    timing = {}
    for name in ("first_event_ms", "first_text_ms", "ttlt_ms", "tbt_ms"):
        values = [
            float((record.get("timing") or {})[name])
            for record in completed
            if (record.get("timing") or {}).get(name) is not None
        ]
        timing[name] = {
            "count": len(values),
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
        }
    return {
        "logical_requests": len(records),
        "completed": len(completed),
        "failed": len(failed),
        "request_hit_rate": (
            sum(
                int((record.get("usage") or {}).get("cached_tokens") or 0)
                > 0
                for record in completed
            )
            / len(completed)
            if completed
            else None
        ),
        "substantive_request_hit_rate": (
            substantive_hits / len(completed) if completed else None
        ),
        "substantive_hits": substantive_hits,
        "token_weighted_cache_rate": (
            usage["cached_tokens"] / usage["input_tokens"]
            if usage["input_tokens"] > 0
            else None
        ),
        "prefix_efficiency_p50": percentile(efficiencies, 0.50),
        "usage": usage,
        "timing": timing,
        "cost": compute_cost(usage, price_book) if price_book else None,
    }
