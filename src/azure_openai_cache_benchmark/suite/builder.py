from __future__ import annotations

from typing import Any, Mapping

from ..constants import MODEL_DEFAULT
from ..specs import RequestSpec, estimate_cacheable_prefix_tokens


class SuiteBuilder:
    def __init__(self, *, model: str = MODEL_DEFAULT) -> None:
        self.model = model
        self._specs: list[RequestSpec] = []

    def add(
        self,
        *,
        experiment: str,
        arm: str,
        namespace: str,
        instructions: str,
        input_text: str,
        prompt_cache_key: str,
        tools: tuple[dict[str, Any], ...] = (),
        text_config: dict[str, Any] | None = None,
        max_output_tokens: int = 512,
        expected_cache_state: str = "observe",
        delay_before_seconds: float = 0.0,
        batch_id: str | None = None,
        optimized_cohort: bool = False,
        pair_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RequestSpec:
        order = len(self._specs) + 1
        spec = RequestSpec(
            order=order,
            logical_request_id=f"{experiment}-{arm}-{order:03d}",
            experiment=experiment,
            arm=arm,
            namespace=namespace,
            instructions=instructions,
            input_text=input_text,
            prompt_cache_key=prompt_cache_key,
            tools=tools,
            text_config=text_config,
            max_output_tokens=max_output_tokens,
            expected_cache_state=expected_cache_state,
            cacheable_prefix_tokens_estimate=estimate_cacheable_prefix_tokens(
                instructions,
                tools,
                text_config,
                self.model,
            ),
            delay_before_seconds=delay_before_seconds,
            batch_id=batch_id,
            optimized_cohort=optimized_cohort,
            pair_id=pair_id,
            metadata=dict(metadata or {}),
        )
        self._specs.append(spec)
        return spec

    def snapshot(self) -> list[RequestSpec]:
        return list(self._specs)
