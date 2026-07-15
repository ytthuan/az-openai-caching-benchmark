from __future__ import annotations

from collections import Counter, defaultdict
from typing import Sequence

from ..constants import (
    EXPECTED_ALLOCATION,
    MATCHED_PAIR_COUNT,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    PLANNED_ATTEMPTS,
)
from ..errors import ValidationError
from ..specs import RequestSpec


def validate_suite(specs: Sequence[RequestSpec]) -> None:
    if len(specs) != PLANNED_ATTEMPTS:
        raise ValidationError(
            f"Planned suite must contain {PLANNED_ATTEMPTS} requests; "
            f"got {len(specs)}."
        )
    allocation = Counter(spec.experiment for spec in specs)
    if dict(allocation) != EXPECTED_ALLOCATION:
        raise ValidationError(
            f"Unexpected scenario allocation: {dict(allocation)}"
        )
    if [spec.order for spec in specs] != list(
        range(1, PLANNED_ATTEMPTS + 1)
    ):
        raise ValidationError("Request order is not contiguous.")
    optimized = [spec for spec in specs if spec.optimized_cohort]
    if len(optimized) != OPTIMIZED_COHORT_EXPECTED_REQUESTS:
        raise ValidationError(
            "Optimized cohort must contain "
            f"{OPTIMIZED_COHORT_EXPECTED_REQUESTS} warm requests; "
            f"got {len(optimized)}."
        )
    if any(spec.expected_cache_state != "warm" for spec in optimized):
        raise ValidationError("Optimized cohort may contain only warm requests.")

    pairs: dict[str, list[RequestSpec]] = defaultdict(list)
    for spec in specs:
        if spec.pair_id:
            pairs[spec.pair_id].append(spec)
    if len(pairs) != MATCHED_PAIR_COUNT:
        raise ValidationError(
            f"Expected {MATCHED_PAIR_COUNT} matched latency pairs."
        )
    namespaces: set[str] = set()
    keys: set[str] = set()
    previous_order = 0
    for pair_id in sorted(pairs):
        members = sorted(pairs[pair_id], key=lambda spec: spec.order)
        if len(members) != 2:
            raise ValidationError(f"{pair_id} must contain two members.")
        cold, warm = members
        if (
            cold.expected_cache_state,
            warm.expected_cache_state,
        ) != ("cold", "warm"):
            raise ValidationError(f"{pair_id} must be cold then warm.")
        if warm.order != cold.order + 1 or cold.order <= previous_order:
            raise ValidationError(f"{pair_id} is not an adjacent ordered pair.")
        if (
            cold.request_payload_fingerprint()
            != warm.request_payload_fingerprint()
        ):
            raise ValidationError(
                f"{pair_id} payload members are not byte-identical."
            )
        if cold.batch_id or warm.batch_id:
            raise ValidationError(f"{pair_id} cannot execute concurrently.")
        namespaces.add(cold.namespace)
        keys.add(cold.prompt_cache_key)
        previous_order = warm.order
    if len(namespaces) != MATCHED_PAIR_COUNT:
        raise ValidationError("Matched pair namespaces are not unique.")
    if len(keys) != MATCHED_PAIR_COUNT:
        raise ValidationError("Matched pair cache keys are not unique.")
