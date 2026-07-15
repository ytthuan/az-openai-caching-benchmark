from __future__ import annotations

import time
from typing import Any, Callable

from ..config import redact_text
from ..pricing.costs import compute_cost
from ..pricing.models import PriceBook
from ..serialization import canonical_json, sha256_text, utc_now_iso
from ..specs import RequestSpec
from ..tokens import estimate_tokens
from ..usage import (
    cache_state_is_valid,
    cacheable_prefix_efficiency,
    usage_from_response_payload,
)
from .budget import AttemptBudget
from .events import (
    REQUEST_ERRORS,
    StreamResponseError,
    event_payload,
    extract_output_text,
    http_status,
    redact_payload,
    request_id,
    response_payload,
    retry_after_seconds,
)


def request_kwargs(spec: RequestSpec, model: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": spec.instructions,
        "input": spec.input_text,
        "reasoning": {"effort": "low"},
        "max_output_tokens": spec.max_output_tokens,
        "prompt_cache_key": spec.prompt_cache_key,
        "store": False,
        "stream": True,
    }
    if spec.tools:
        kwargs["tools"] = list(spec.tools)
        kwargs["tool_choice"] = "none"
    if spec.text_config:
        kwargs["text"] = spec.text_config
    return kwargs


def base_record(
    spec: RequestSpec,
    *,
    attempt_number: int,
    retry_index: int,
    started_at: str,
    gap_seconds: float | None,
) -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "attempt_number": attempt_number,
        "retry_index": retry_index,
        "logical_request_id": spec.logical_request_id,
        "order": spec.order,
        "experiment": spec.experiment,
        "arm": spec.arm,
        "expected_cache_state": spec.expected_cache_state,
        "optimized_cohort": spec.optimized_cohort,
        "pair_id": spec.pair_id,
        "cacheable_prefix_tokens_estimate": (
            spec.cacheable_prefix_tokens_estimate
        ),
        "namespace_sha256": sha256_text(spec.namespace),
        "instructions_sha256": sha256_text(spec.instructions),
        "input_sha256": sha256_text(spec.input_text),
        "tools_sha256": sha256_text(canonical_json(spec.tools)),
        "text_config_sha256": (
            sha256_text(canonical_json(spec.text_config))
            if spec.text_config
            else None
        ),
        "prompt_cache_key_sha256": sha256_text(spec.prompt_cache_key),
        "request_payload_sha256": spec.request_payload_fingerprint(),
        "metadata": spec.metadata,
        "gap_from_previous_same_arm_seconds": gap_seconds,
        "started_at": started_at,
    }


def execute_once(
    *,
    client: Any,
    model: str,
    base_url: str,
    api_key: str,
    budget: AttemptBudget,
    price_book: PriceBook | None,
    spec: RequestSpec,
    retry_index: int,
    gap_seconds_for: Callable[[float], float | None],
) -> dict[str, Any]:
    attempt_number = budget.claim(
        logical_request_id=spec.logical_request_id,
        reason="initial" if retry_index == 0 else "retry",
    )
    started_at = utc_now_iso()
    started = time.perf_counter()
    record_base = base_record(
        spec,
        attempt_number=attempt_number,
        retry_index=retry_index,
        started_at=started_at,
        gap_seconds=gap_seconds_for(started),
    )
    first_event_at: float | None = None
    first_text_at: float | None = None
    terminal_at: float | None = None
    text_parts: list[str] = []
    terminal_payload: dict[str, Any] | None = None
    event_counts: dict[str, int] = {}
    try:
        stream = client.responses.create(**request_kwargs(spec, model))
        for event in stream:
            now = time.perf_counter()
            event_type = str(getattr(event, "type", "unknown"))
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            if first_event_at is None:
                first_event_at = now
            if event_type == "response.output_text.delta":
                delta = str(getattr(event, "delta", "") or "")
                if delta and first_text_at is None:
                    first_text_at = now
                text_parts.append(delta)
            elif event_type == "response.completed":
                terminal_payload = response_payload(
                    getattr(event, "response", None)
                )
                terminal_at = now
            elif event_type in {
                "response.failed",
                "response.incomplete",
                "error",
            }:
                raise StreamResponseError(event_type, event_payload(event))
        if terminal_payload is None:
            raise StreamResponseError(
                "missing_response.completed",
                {"event_counts": event_counts},
            )
        if terminal_payload.get("status") != "completed":
            raise StreamResponseError(
                "terminal_response_not_completed",
                {
                    "status": terminal_payload.get("status"),
                    "incomplete_details": terminal_payload.get(
                        "incomplete_details"
                    ),
                },
            )
        terminal_at = terminal_at or time.perf_counter()
        output_text = "".join(text_parts) or extract_output_text(
            terminal_payload
        )
        usage = usage_from_response_payload(terminal_payload)
        visible_tokens = estimate_tokens(output_text, model) if output_text else 0
        first_event_ms = (
            (first_event_at - started) * 1_000
            if first_event_at is not None
            else None
        )
        first_text_ms = (
            (first_text_at - started) * 1_000
            if first_text_at is not None
            else None
        )
        ttlt_ms = (terminal_at - started) * 1_000
        tbt_ms = (
            (terminal_at - first_text_at) * 1_000 / (visible_tokens - 1)
            if first_text_at is not None and visible_tokens > 1
            else None
        )
        efficiency = cacheable_prefix_efficiency(
            usage["cached_tokens"],
            spec.cacheable_prefix_tokens_estimate,
        )
        return {
            **record_base,
            "status": "completed",
            "completed_at": utc_now_iso(),
            "response_id": terminal_payload.get("id"),
            "response_status": terminal_payload.get("status"),
            "model": terminal_payload.get("model"),
            "usage": usage,
            "cost": compute_cost(usage, price_book) if price_book else None,
            "cacheable_prefix_efficiency": efficiency,
            "cache_state_valid": cache_state_is_valid(
                spec.expected_cache_state,
                cached_tokens=usage["cached_tokens"],
                cacheable_prefix_tokens_estimate=(
                    spec.cacheable_prefix_tokens_estimate
                ),
            ),
            "timing": {
                "first_event_ms": first_event_ms,
                "first_text_ms": first_text_ms,
                "ttlt_ms": ttlt_ms,
                "tbt_ms": tbt_ms,
                "visible_output_tokens_estimate": visible_tokens,
            },
            "event_counts": event_counts,
            "output_sha256": sha256_text(output_text),
        }
    except REQUEST_ERRORS as exc:
        stream_payload = (
            exc.payload if isinstance(exc, StreamResponseError) else None
        )
        return {
            **record_base,
            "status": "failed",
            "completed_at": utc_now_iso(),
            "timing": {
                "first_event_ms": (
                    (first_event_at - started) * 1_000
                    if first_event_at is not None
                    else None
                ),
                "first_text_ms": (
                    (first_text_at - started) * 1_000
                    if first_text_at is not None
                    else None
                ),
                "ttlt_ms": (time.perf_counter() - started) * 1_000,
                "tbt_ms": None,
            },
            "event_counts": event_counts,
            "error": {
                "type": type(exc).__name__,
                "message": redact_text(
                    str(exc),
                    secrets=(api_key,),
                    base_url=base_url,
                ),
                "http_status": http_status(exc),
                "request_id": request_id(exc),
                "retry_after_seconds": retry_after_seconds(exc),
                "stream_payload": redact_payload(
                    stream_payload,
                    secrets=(api_key,),
                    base_url=base_url,
                ),
            },
        }
