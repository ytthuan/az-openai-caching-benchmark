from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Mapping, Sequence

from ..constants import MIN_CACHEABLE_TOKENS, PROBE_ATTEMPTS
from ..errors import ValidationError
from ..specs import RequestSpec
from ..usage import cache_state_is_valid
from .runner import ResponseRunner


def _run_burst(
    runner: ResponseRunner,
    specs: Sequence[RequestSpec],
) -> None:
    with ThreadPoolExecutor(max_workers=len(specs)) as executor:
        futures = [
            executor.submit(
                runner.run_spec,
                spec,
                allow_retry=False,
            )
            for spec in specs
        ]
        for future in as_completed(futures):
            future.result()


def validate_capability_probe(
    specs: Sequence[RequestSpec],
    records: Sequence[Mapping[str, Any]],
) -> None:
    if len(records) != PROBE_ATTEMPTS:
        raise ValidationError(
            "Capability/isolation probe did not complete three requests."
        )
    for spec, record in zip(specs, records):
        if record.get("status") != "completed":
            raise ValidationError(
                f"Capability/isolation probe failed for {spec.arm}."
            )
        usage = record.get("usage") or {}
        if not cache_state_is_valid(
            spec.expected_cache_state,
            cached_tokens=int(usage.get("cached_tokens") or 0),
            cacheable_prefix_tokens_estimate=(
                spec.cacheable_prefix_tokens_estimate
            ),
        ):
            raise ValidationError(
                "Capability/isolation probe cache state invalid for "
                f"{spec.arm}: cached_tokens={usage.get('cached_tokens')}."
            )


def validate_under_threshold(
    records: Sequence[Mapping[str, Any]],
) -> None:
    for record in records:
        if (
            record.get("experiment") == "qualification"
            and record.get("arm") == "under-threshold"
            and record.get("status") == "completed"
        ):
            input_tokens = int(
                (record.get("usage") or {}).get("input_tokens") or 0
            )
            if input_tokens >= MIN_CACHEABLE_TOKENS:
                raise ValidationError(
                    "Qualification control reached 1,024 input tokens."
                )


def execute_plan(
    *,
    runner: ResponseRunner,
    specs: Sequence[RequestSpec],
) -> None:
    index = 0
    while index < len(specs):
        spec = specs[index]
        if spec.delay_before_seconds > 0:
            time.sleep(spec.delay_before_seconds)
        if spec.batch_id:
            batch_id = spec.batch_id
            batch: list[RequestSpec] = []
            while index < len(specs) and specs[index].batch_id == batch_id:
                batch.append(specs[index])
                index += 1
            _run_burst(runner, batch)
            continue
        allow_retry = spec.experiment != "matched_latency"
        record = runner.run_spec(spec, allow_retry=allow_retry)
        if (
            spec.experiment == "qualification"
            and spec.arm == "under-threshold"
            and record.get("status") == "completed"
        ):
            validate_under_threshold(runner.records)
        index += 1
