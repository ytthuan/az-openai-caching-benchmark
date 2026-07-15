from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from ..constants import (
    DYNAMIC_START,
    MATCHED_PAIR_COUNT,
    MIN_REQUIRED_ATTEMPTS,
    OFFICIAL_SOURCES,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    PLANNED_ATTEMPTS,
    PROBE_ATTEMPTS,
)
from ..pricing.models import PriceBook
from ..prompt_assets import PromptAsset
from ..serialization import utc_now_iso
from ..specs import RequestSpec


def manifest_payload(
    *,
    run_id: str,
    model: str,
    asset: PromptAsset,
    specs: Sequence[RequestSpec],
    max_attempts: int,
    price_book: PriceBook | None,
    endpoint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "run_id": run_id,
        "model": model,
        "created_at": utc_now_iso(),
        "prompt": {
            "path": asset.path,
            "sha256": asset.sha256,
            "word_count": asset.word_count,
            "estimated_tokens": asset.estimated_tokens,
            "encoding": asset.encoding,
            "dynamic_start_marker": DYNAMIC_START,
        },
        "suite": {
            "planned_requests": PLANNED_ATTEMPTS,
            "probe_requests": PROBE_ATTEMPTS,
            "minimum_required_attempts": MIN_REQUIRED_ATTEMPTS,
            "hard_cap": max_attempts,
            "optimized_expected_requests": (
                OPTIMIZED_COHORT_EXPECTED_REQUESTS
            ),
            "matched_pair_count": MATCHED_PAIR_COUNT,
            "allocation": dict(
                Counter(spec.experiment for spec in specs)
            ),
        },
        "request_plan": [spec.to_manifest() for spec in specs],
        "pricing": price_book.to_dict() if price_book else None,
        "endpoint": dict(endpoint) if endpoint else None,
        "official_sources": list(OFFICIAL_SOURCES),
    }
