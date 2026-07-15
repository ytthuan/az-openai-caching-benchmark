from __future__ import annotations

from typing import Any, Mapping

from ..constants import SAMPLE_SUMMARY_ALLOWLIST
from ..errors import ValidationError


def _safe_metric(metric: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "logical_requests",
        "completed",
        "failed",
        "request_hit_rate",
        "substantive_request_hit_rate",
        "substantive_hits",
        "token_weighted_cache_rate",
        "prefix_efficiency_p50",
        "usage",
        "timing",
        "cost",
    }
    return {
        key: metric.get(key)
        for key in allowed
        if key in metric
    }


def sanitize_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    records = summary.get("records") or {}
    safe = {
        "schema_version": summary.get("schema_version"),
        "sample": {
            "sanitized": True,
            "description": (
                "Allowlisted aggregate view of a real live benchmark run."
            ),
        },
        "run_id": summary.get("run_id"),
        "model": summary.get("model"),
        "generated_at": summary.get("generated_at"),
        "run_status": summary.get("run_status"),
        "suite": dict(summary.get("suite") or {}),
        "records": {
            key: records.get(key)
            for key in (
                "attempt_records",
                "final_logical_requests",
                "planned_final_logical_requests",
                "probe_final_logical_requests",
            )
        },
        "overall": _safe_metric(summary.get("overall") or {}),
        "optimized": _safe_metric(summary.get("optimized") or {}),
        "degraded": _safe_metric(summary.get("degraded") or {}),
        "arms": {
            str(name): _safe_metric(value)
            for name, value in (summary.get("arms") or {}).items()
            if isinstance(value, Mapping)
        },
        "root_causes": [
            {
                key: row.get(key)
                for key in (
                    "factor",
                    "baseline_arm",
                    "degraded_arm",
                    "baseline_completed",
                    "degraded_completed",
                    "baseline_token_weighted_cache_rate",
                    "degraded_token_weighted_cache_rate",
                    "delta",
                    "status",
                )
            }
            for row in (summary.get("root_causes") or [])
            if isinstance(row, Mapping)
        ],
        "matched_latency": {
            key: (summary.get("matched_latency") or {}).get(key)
            for key in (
                "planned_pairs",
                "observed_pairs",
                "valid_pairs",
                "invalid_pairs",
                "invalid_reasons",
                "warm_faster_ttlt_pairs",
                "cold_faster_ttlt_pairs",
                "equal_ttlt_pairs",
                "warm_minus_cold_ttlt_ms_p50",
                "warm_minus_cold_first_text_ms_p50",
                "causal_latency_claimed",
                "interpretation",
            )
        },
        "acceptance": dict(summary.get("acceptance") or {}),
        "pricing": (
            dict(summary["pricing"])
            if isinstance(summary.get("pricing"), Mapping)
            else None
        ),
        "prompt": {
            key: (summary.get("prompt") or {}).get(key)
            for key in (
                "path",
                "sha256",
                "word_count",
                "estimated_tokens",
                "encoding",
                "dynamic_start_marker",
            )
        },
        "provenance": {
            "source_kind": "sanitized_live_run",
            "source_run_id": summary.get("run_id"),
            "derived_from": ["live summary aggregate"],
            "generated_at": summary.get("generated_at"),
            "sanitized": True,
        },
    }
    if set(safe) != SAMPLE_SUMMARY_ALLOWLIST:
        raise ValidationError("Sanitized summary allowlist drifted.")
    return safe
