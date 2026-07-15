from __future__ import annotations

from ..constants import MODEL_DEFAULT
from ..prompt_assets import PromptAsset
from ..serialization import safe_slug
from ..specs import RequestSpec
from .builder import SuiteBuilder
from .cache_behavior import (
    add_cache_key_cardinality,
    add_idle_retention,
    add_traffic_shape,
)
from .latency import add_matched_latency
from .qualification import add_qualification
from .stability import add_prefix_stability, add_tool_schema_stability
from .validation import validate_suite


def build_suite(
    *,
    run_id: str,
    asset: PromptAsset,
    model: str = MODEL_DEFAULT,
    idle_gap_seconds: float = 660.0,
    paced_interval_seconds: float = 4.2,
) -> list[RequestSpec]:
    run_slug = safe_slug(run_id)
    builder = SuiteBuilder(model=model)
    add_qualification(builder, run_slug=run_slug, asset=asset)
    add_prefix_stability(builder, run_slug=run_slug, asset=asset)
    add_tool_schema_stability(builder, run_slug=run_slug, asset=asset)
    add_cache_key_cardinality(builder, run_slug=run_slug, asset=asset)
    add_traffic_shape(
        builder,
        run_slug=run_slug,
        asset=asset,
        paced_interval_seconds=paced_interval_seconds,
    )
    add_idle_retention(
        builder,
        run_slug=run_slug,
        asset=asset,
        idle_gap_seconds=idle_gap_seconds,
    )
    add_matched_latency(builder, run_slug=run_slug, asset=asset)
    specs = builder.snapshot()
    validate_suite(specs)
    return specs
