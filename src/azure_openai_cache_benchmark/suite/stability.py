from __future__ import annotations

from ..prompt_assets import PromptAsset, render_system_prompt
from ..schemas import build_tools, response_json_schema
from ..specs import cache_key, default_input
from .builder import SuiteBuilder


def add_prefix_stability(
    builder: SuiteBuilder,
    *,
    run_slug: str,
    asset: PromptAsset,
) -> None:
    for arm, volatile in (
        ("volatile-prefix", True),
        ("stable-prefix", False),
    ):
        namespace = f"{run_slug}-prefix-{arm}"
        tools = build_tools(namespace)
        schema = response_json_schema(namespace)
        for index in range(10):
            timestamp = f"2026-01-01T00:00:{index:02d}Z"
            instructions = render_system_prompt(
                asset,
                namespace=namespace,
                case_id=f"prefix-{arm}",
                timestamp_utc=timestamp,
                session_id=f"SESSION-PREFIX-{index:03d}",
                early_metadata=(
                    f"request={index};timestamp={timestamp}"
                    if volatile
                    else None
                ),
            )
            builder.add(
                experiment="prefix_stability",
                arm=arm,
                namespace=namespace,
                instructions=instructions,
                input_text=default_input(namespace),
                prompt_cache_key=cache_key(namespace),
                tools=tools,
                text_config=schema,
                expected_cache_state=(
                    "cold"
                    if index == 0
                    else ("warm" if not volatile else "observe")
                ),
                optimized_cohort=not volatile and index > 0,
                metadata={"sample_index": index},
            )


def add_tool_schema_stability(
    builder: SuiteBuilder,
    *,
    run_slug: str,
    asset: PromptAsset,
) -> None:
    for arm, shuffle in (("shuffled", True), ("canonical", False)):
        namespace = f"{run_slug}-tool-schema-{arm}"
        instructions = render_system_prompt(
            asset,
            namespace=namespace,
            case_id=f"tool-schema-{arm}",
        )
        schema = response_json_schema(namespace)
        for index in range(10):
            tools = build_tools(
                namespace,
                shuffle_seed=index if shuffle else None,
            )
            builder.add(
                experiment="tool_schema_stability",
                arm=arm,
                namespace=namespace,
                instructions=instructions,
                input_text=default_input(namespace),
                prompt_cache_key=cache_key(namespace),
                tools=tools,
                text_config=schema,
                expected_cache_state=(
                    "cold"
                    if index == 0
                    else ("warm" if not shuffle else "observe")
                ),
                optimized_cohort=not shuffle and index > 0,
                metadata={
                    "sample_index": index,
                    "tool_order": [tool["name"] for tool in tools],
                },
            )
