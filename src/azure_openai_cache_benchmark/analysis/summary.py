from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from ..constants import (
    DEGRADED_ARMS,
    MATCHED_PAIR_COUNT,
    MIN_CACHEABLE_TOKENS,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    OPTIMIZED_PREFIX_P50_FLOOR,
    OPTIMIZED_REQUEST_HIT_FLOOR,
    OPTIMIZED_TOKEN_CACHE_FLOOR,
    PLANNED_ATTEMPTS,
    PROBE_ATTEMPTS,
    WARM_PREFIX_EFFICIENCY_FLOOR,
)
from ..pricing.models import PriceBook
from .diagnostics import matched_latency_summary, root_cause_summary
from .metrics import latest_records, metric_summary


def build_summary(
    *,
    run_id: str,
    model: str,
    records: Sequence[Mapping[str, Any]],
    price_book: PriceBook | None,
    attempt_budget: Mapping[str, Any],
    run_status: str,
    completed_at: str,
    prompt_metadata: Mapping[str, Any] | None = None,
    fatal_error: str | None = None,
) -> dict[str, Any]:
    final_records = latest_records(records)
    planned_final = [
        record
        for record in final_records
        if record.get("experiment") != "capability_probe"
    ]
    optimized_records = [
        record
        for record in planned_final
        if bool(record.get("optimized_cohort"))
    ]
    degraded_records = [
        record
        for record in planned_final
        if (str(record.get("experiment")), str(record.get("arm")))
        in DEGRADED_ARMS
    ]
    arms: dict[str, Any] = {}
    for key in sorted(
        {
            f"{record.get('experiment')}/{record.get('arm')}"
            for record in planned_final
        }
    ):
        experiment, arm = key.split("/", maxsplit=1)
        arms[key] = metric_summary(
            [
                record
                for record in planned_final
                if record.get("experiment") == experiment
                and record.get("arm") == arm
            ],
            price_book,
        )
    overall = metric_summary(final_records, price_book)
    optimized = metric_summary(optimized_records, price_book)
    degraded = metric_summary(degraded_records, price_book)
    reliability_pass = (
        optimized["logical_requests"]
        == OPTIMIZED_COHORT_EXPECTED_REQUESTS
        and optimized["completed"] == OPTIMIZED_COHORT_EXPECTED_REQUESTS
        and optimized["failed"] == 0
    )
    hit_rate = optimized["substantive_request_hit_rate"]
    token_rate = optimized["token_weighted_cache_rate"]
    prefix_p50 = optimized["prefix_efficiency_p50"]
    cache_pass = bool(
        hit_rate is not None
        and hit_rate >= OPTIMIZED_REQUEST_HIT_FLOOR
        and (
            (
                token_rate is not None
                and token_rate >= OPTIMIZED_TOKEN_CACHE_FLOOR
            )
            or (
                prefix_p50 is not None
                and prefix_p50 >= OPTIMIZED_PREFIX_P50_FLOOR
            )
        )
    )
    acceptance = {
        "status": (
            "pass"
            if reliability_pass and cache_pass
            else ("fail" if run_status != "incomplete" else "incomplete")
        ),
        "optimized_reliability_pass": reliability_pass,
        "optimized_cache_pass": cache_pass,
        "optimized_expected_requests": OPTIMIZED_COHORT_EXPECTED_REQUESTS,
        "optimized_completed_requests": optimized["completed"],
        "optimized_failed_requests": optimized["failed"],
        "substantive_request_hit_rate": hit_rate,
        "token_weighted_cache_rate": token_rate,
        "prefix_efficiency_p50": prefix_p50,
        "thresholds": {
            "substantive_request_hit_rate": OPTIMIZED_REQUEST_HIT_FLOOR,
            "token_weighted_cache_rate": OPTIMIZED_TOKEN_CACHE_FLOOR,
            "prefix_efficiency_p50": OPTIMIZED_PREFIX_P50_FLOOR,
            "minimum_cached_tokens": MIN_CACHEABLE_TOKENS,
            "warm_prefix_efficiency": WARM_PREFIX_EFFICIENCY_FLOOR,
            "maximum_optimized_failures": 0,
        },
    }
    allocation = Counter(
        str(record.get("experiment")) for record in planned_final
    )
    return {
        "schema_version": "2.0.0",
        "run_id": run_id,
        "model": model,
        "generated_at": completed_at,
        "run_status": run_status,
        "fatal_error": fatal_error,
        "suite": {
            "planned_requests": PLANNED_ATTEMPTS,
            "probe_requests": PROBE_ATTEMPTS,
            "hard_cap": int(attempt_budget.get("maximum") or 0),
            "optimized_expected_requests": (
                OPTIMIZED_COHORT_EXPECTED_REQUESTS
            ),
            "matched_pair_count": MATCHED_PAIR_COUNT,
            "allocation": dict(allocation),
        },
        "records": {
            "attempt_records": len(records),
            "final_logical_requests": len(final_records),
            "planned_final_logical_requests": len(planned_final),
            "probe_final_logical_requests": (
                len(final_records) - len(planned_final)
            ),
            "attempt_budget": dict(attempt_budget),
        },
        "overall": overall,
        "optimized": optimized,
        "degraded": degraded,
        "arms": arms,
        "root_causes": root_cause_summary(planned_final, price_book),
        "matched_latency": matched_latency_summary(planned_final),
        "acceptance": acceptance,
        "pricing": price_book.to_dict() if price_book else None,
        "prompt": dict(prompt_metadata or {}),
        "provenance": {
            "source_kind": "live_run",
            "source_run_id": run_id,
            "derived_from": ["manifest.json", "requests.jsonl"],
            "generated_at": completed_at,
        },
    }
