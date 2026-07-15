from __future__ import annotations

from ..constants import (
    MATCHED_MAX_OUTPUT_TOKENS,
    MODEL_DEFAULT,
    PROBE_ATTEMPTS,
)
from ..errors import ValidationError
from ..prompt_assets import PromptAsset, render_system_prompt
from ..schemas import response_json_schema
from ..serialization import safe_slug
from ..specs import RequestSpec, cache_key, default_input
from ..tokens import truncate_to_tokens
from .builder import SuiteBuilder


def add_qualification(
    builder: SuiteBuilder,
    *,
    run_slug: str,
    asset: PromptAsset,
) -> None:
    short_namespace = f"{run_slug}-qualification-short"
    short_instructions = truncate_to_tokens(
        asset.template,
        700,
        builder.model,
    )
    for index in range(3):
        builder.add(
            experiment="qualification",
            arm="under-threshold",
            namespace=short_namespace,
            instructions=short_instructions,
            input_text="Trả đúng chuỗi CACHE_QUALIFICATION_OK.",
            prompt_cache_key=cache_key(short_namespace),
            max_output_tokens=64,
            expected_cache_state="under_threshold",
            metadata={"sample_index": index},
        )

    full_namespace = f"{run_slug}-qualification-full"
    full_instructions = render_system_prompt(
        asset,
        namespace=full_namespace,
        case_id="qualification-full",
    )
    full_schema = response_json_schema(full_namespace)
    for index in range(3):
        builder.add(
            experiment="qualification",
            arm="full-prompt",
            namespace=full_namespace,
            instructions=full_instructions,
            input_text=default_input(full_namespace),
            prompt_cache_key=cache_key(full_namespace),
            text_config=full_schema,
            expected_cache_state="cold" if index == 0 else "warm",
            optimized_cohort=index > 0,
            metadata={"sample_index": index},
        )


def build_isolation_probe(
    *,
    run_id: str,
    asset: PromptAsset,
    model: str = MODEL_DEFAULT,
) -> list[RequestSpec]:
    run_slug = safe_slug(run_id)
    builder = SuiteBuilder(model=model)
    namespace_a = f"{run_slug}-capability-a"
    namespace_b = f"{run_slug}-capability-b"
    payloads = {
        namespace: render_system_prompt(
            asset,
            namespace=namespace,
            case_id="capability-isolation",
        )
        for namespace in (namespace_a, namespace_b)
    }
    for namespace, expected, member in (
        (namespace_a, "cold", "a-cold"),
        (namespace_b, "cold", "b-cold"),
        (namespace_a, "warm", "a-warm"),
    ):
        builder.add(
            experiment="capability_probe",
            arm=member,
            namespace=namespace,
            instructions=payloads[namespace],
            input_text=(
                "Phép đo capability synthetic. Chỉ trả đúng chuỗi "
                "CACHE_CAPABILITY_OK."
            ),
            prompt_cache_key=cache_key(namespace),
            max_output_tokens=MATCHED_MAX_OUTPUT_TOKENS,
            expected_cache_state=expected,
            metadata={"probe_member": member},
        )
    specs = builder.snapshot()
    if len(specs) != PROBE_ATTEMPTS:
        raise ValidationError("Capability probe allocation is invalid.")
    if (
        specs[0].request_payload_fingerprint()
        != specs[2].request_payload_fingerprint()
    ):
        raise ValidationError("Capability probe A payload is not identical.")
    if specs[0].namespace == specs[1].namespace:
        raise ValidationError("Capability probe namespaces are not isolated.")
    return specs
