from __future__ import annotations

from ..constants import MATCHED_MAX_OUTPUT_TOKENS, MATCHED_PAIR_COUNT
from ..prompt_assets import PromptAsset, render_system_prompt
from ..schemas import build_tools, response_json_schema
from ..specs import cache_key
from .builder import SuiteBuilder


def add_matched_latency(
    builder: SuiteBuilder,
    *,
    run_slug: str,
    asset: PromptAsset,
) -> None:
    for pair_index in range(1, MATCHED_PAIR_COUNT + 1):
        pair_id = f"latency-pair-{pair_index:02d}"
        namespace = f"{run_slug}-{pair_id}"
        instructions = render_system_prompt(
            asset,
            namespace=namespace,
            case_id=pair_id,
        )
        common = {
            "experiment": "matched_latency",
            "arm": "cold-warm-pair",
            "namespace": namespace,
            "instructions": instructions,
            "input_text": (
                "Phép đo latency synthetic. Chỉ trả đúng chuỗi "
                "MATCHED_LATENCY_OK."
            ),
            "prompt_cache_key": cache_key(namespace),
            "tools": build_tools(namespace),
            "text_config": response_json_schema(namespace),
            "max_output_tokens": MATCHED_MAX_OUTPUT_TOKENS,
            "pair_id": pair_id,
        }
        builder.add(
            expected_cache_state="cold",
            metadata={"pair_index": pair_index, "member": "cold"},
            **common,
        )
        builder.add(
            expected_cache_state="warm",
            optimized_cohort=True,
            metadata={"pair_index": pair_index, "member": "warm"},
            **common,
        )
