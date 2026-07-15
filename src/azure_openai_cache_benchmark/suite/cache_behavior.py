from __future__ import annotations

from ..prompt_assets import PromptAsset, render_system_prompt
from ..schemas import build_tools, response_json_schema
from ..specs import cache_key, default_input
from .builder import SuiteBuilder


def add_cache_key_cardinality(
    builder: SuiteBuilder,
    *,
    run_slug: str,
    asset: PromptAsset,
) -> None:
    for arm, unique_key in (
        ("per-request-key", True),
        ("stable-key", False),
    ):
        namespace = f"{run_slug}-cache-key-{arm}"
        instructions = render_system_prompt(
            asset,
            namespace=namespace,
            case_id=f"cache-key-{arm}",
        )
        tools = build_tools(namespace)
        schema = response_json_schema(namespace)
        for index in range(10):
            key_namespace = f"{namespace}-{index}" if unique_key else namespace
            builder.add(
                experiment="cache_key_cardinality",
                arm=arm,
                namespace=namespace,
                instructions=instructions,
                input_text=default_input(namespace),
                prompt_cache_key=cache_key(key_namespace),
                tools=tools,
                text_config=schema,
                expected_cache_state=(
                    "cold"
                    if index == 0
                    else ("warm" if not unique_key else "observe")
                ),
                optimized_cohort=not unique_key and index > 0,
                metadata={"sample_index": index},
            )


def add_traffic_shape(
    builder: SuiteBuilder,
    *,
    run_slug: str,
    asset: PromptAsset,
    paced_interval_seconds: float,
) -> None:
    for arm in ("burst", "paced"):
        namespace = f"{run_slug}-traffic-{arm}"
        instructions = render_system_prompt(
            asset,
            namespace=namespace,
            case_id=f"traffic-{arm}",
        )
        tools = build_tools(namespace)
        schema = response_json_schema(namespace)
        for index in range(6):
            builder.add(
                experiment="traffic_shape",
                arm=arm,
                namespace=namespace,
                instructions=instructions,
                input_text=default_input(namespace),
                prompt_cache_key=cache_key(namespace),
                tools=tools,
                text_config=schema,
                expected_cache_state="cold" if index == 0 else "warm",
                delay_before_seconds=(
                    paced_interval_seconds
                    if arm == "paced" and index > 0
                    else 0.0
                ),
                batch_id=(
                    "traffic-burst-steady"
                    if arm == "burst" and index > 0
                    else None
                ),
                optimized_cohort=arm == "paced" and index > 0,
                metadata={"sample_index": index},
            )


def add_idle_retention(
    builder: SuiteBuilder,
    *,
    run_slug: str,
    asset: PromptAsset,
    idle_gap_seconds: float,
) -> None:
    namespace = f"{run_slug}-idle-retention"
    instructions = render_system_prompt(
        asset,
        namespace=namespace,
        case_id="idle-retention",
    )
    schema = response_json_schema(namespace)
    for index, stage in enumerate(
        ("seed", "immediate_warm", "post_idle", "rewarm")
    ):
        builder.add(
            experiment="idle_retention",
            arm="observational",
            namespace=namespace,
            instructions=instructions,
            input_text=default_input(namespace),
            prompt_cache_key=cache_key(namespace),
            text_config=schema,
            expected_cache_state=(
                "cold"
                if index == 0
                else (
                    "warm"
                    if stage in {"immediate_warm", "rewarm"}
                    else "observe"
                )
            ),
            delay_before_seconds=(
                idle_gap_seconds if stage == "post_idle" else 0.0
            ),
            metadata={"sample_index": index, "probe_stage": stage},
        )
