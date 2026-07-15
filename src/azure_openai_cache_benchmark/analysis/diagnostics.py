from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from ..constants import (
    MATCHED_PAIR_COUNT,
    ROOT_CAUSE_DELTA_FLOOR,
    ROOT_CAUSE_MIN_COMPLETED,
)
from ..pricing.models import PriceBook
from ..usage import cache_state_is_valid
from .metrics import metric_summary, percentile


def root_cause_summary(
    final_records: Sequence[Mapping[str, Any]],
    price_book: PriceBook | None,
) -> list[dict[str, Any]]:
    definitions = (
        (
            "prefix_volatility",
            ("prefix_stability", "stable-prefix"),
            ("prefix_stability", "volatile-prefix"),
        ),
        (
            "tool_schema_churn",
            ("tool_schema_stability", "canonical"),
            ("tool_schema_stability", "shuffled"),
        ),
        (
            "cache_key_cardinality",
            ("cache_key_cardinality", "stable-key"),
            ("cache_key_cardinality", "per-request-key"),
        ),
        (
            "traffic_shape",
            ("traffic_shape", "paced"),
            ("traffic_shape", "burst"),
        ),
    )
    rows: list[dict[str, Any]] = []
    for factor, baseline_key, degraded_key in definitions:
        baseline_records = [
            record
            for record in final_records
            if (record.get("experiment"), record.get("arm")) == baseline_key
            and int((record.get("metadata") or {}).get("sample_index") or 0)
            > 0
        ]
        degraded_records = [
            record
            for record in final_records
            if (record.get("experiment"), record.get("arm")) == degraded_key
            and int((record.get("metadata") or {}).get("sample_index") or 0)
            > 0
        ]
        baseline = metric_summary(baseline_records, price_book)
        degraded = metric_summary(degraded_records, price_book)
        baseline_rate = baseline["token_weighted_cache_rate"]
        degraded_rate = degraded["token_weighted_cache_rate"]
        delta = (
            baseline_rate - degraded_rate
            if baseline_rate is not None and degraded_rate is not None
            else None
        )
        enough = (
            baseline["completed"] >= ROOT_CAUSE_MIN_COMPLETED
            and degraded["completed"] >= ROOT_CAUSE_MIN_COMPLETED
        )
        rows.append(
            {
                "factor": factor,
                "baseline_arm": "/".join(baseline_key),
                "degraded_arm": "/".join(degraded_key),
                "baseline_completed": baseline["completed"],
                "degraded_completed": degraded["completed"],
                "baseline_token_weighted_cache_rate": baseline_rate,
                "degraded_token_weighted_cache_rate": degraded_rate,
                "delta": delta,
                "status": (
                    "supported"
                    if enough
                    and delta is not None
                    and delta >= ROOT_CAUSE_DELTA_FLOOR
                    else ("not_observed" if enough else "insufficient_data")
                ),
            }
        )
    return rows


def matched_latency_summary(
    final_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for record in final_records:
        pair_id = record.get("pair_id")
        if pair_id:
            member = str((record.get("metadata") or {}).get("member") or "")
            grouped[str(pair_id)][member] = record
    valid_deltas: list[float] = []
    first_text_deltas: list[float] = []
    warm_faster = 0
    cold_faster = 0
    equal = 0
    invalid_reasons: Counter[str] = Counter()
    for pair_id in sorted(grouped):
        cold = grouped[pair_id].get("cold")
        warm = grouped[pair_id].get("warm")
        if not cold or not warm:
            invalid_reasons["missing_member"] += 1
            continue
        if cold.get("status") != "completed" or warm.get("status") != "completed":
            invalid_reasons["incomplete_member"] += 1
            continue
        if int(cold.get("retry_index") or 0) != 0 or int(
            warm.get("retry_index") or 0
        ) != 0:
            invalid_reasons["retried_member"] += 1
            continue
        cold_usage = cold.get("usage") or {}
        warm_usage = warm.get("usage") or {}
        if not cache_state_is_valid(
            "cold",
            cached_tokens=int(cold_usage.get("cached_tokens") or 0),
            cacheable_prefix_tokens_estimate=int(
                cold.get("cacheable_prefix_tokens_estimate") or 0
            ),
        ):
            invalid_reasons["cold_not_miss"] += 1
            continue
        if not cache_state_is_valid(
            "warm",
            cached_tokens=int(warm_usage.get("cached_tokens") or 0),
            cacheable_prefix_tokens_estimate=int(
                warm.get("cacheable_prefix_tokens_estimate") or 0
            ),
        ):
            invalid_reasons["warm_not_substantive_hit"] += 1
            continue
        cold_ttlt = (cold.get("timing") or {}).get("ttlt_ms")
        warm_ttlt = (warm.get("timing") or {}).get("ttlt_ms")
        if cold_ttlt is None or warm_ttlt is None:
            invalid_reasons["missing_ttlt"] += 1
            continue
        delta = float(warm_ttlt) - float(cold_ttlt)
        valid_deltas.append(delta)
        if delta < 0:
            warm_faster += 1
        elif delta > 0:
            cold_faster += 1
        else:
            equal += 1
        cold_first = (cold.get("timing") or {}).get("first_text_ms")
        warm_first = (warm.get("timing") or {}).get("first_text_ms")
        if cold_first is not None and warm_first is not None:
            first_text_deltas.append(float(warm_first) - float(cold_first))
    return {
        "planned_pairs": MATCHED_PAIR_COUNT,
        "observed_pairs": len(grouped),
        "valid_pairs": len(valid_deltas),
        "invalid_pairs": MATCHED_PAIR_COUNT - len(valid_deltas),
        "invalid_reasons": dict(invalid_reasons),
        "warm_faster_ttlt_pairs": warm_faster,
        "cold_faster_ttlt_pairs": cold_faster,
        "equal_ttlt_pairs": equal,
        "warm_minus_cold_ttlt_ms_p50": percentile(valid_deltas, 0.50),
        "warm_minus_cold_first_text_ms_p50": percentile(
            first_text_deltas,
            0.50,
        ),
        "causal_latency_claimed": False,
        "interpretation": (
            "directional_only"
            if valid_deltas
            else "insufficient_valid_pairs"
        ),
    }
