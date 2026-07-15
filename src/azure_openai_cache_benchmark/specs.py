from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .constants import MODEL_DEFAULT
from .serialization import canonical_json, sha256_text, short_hash
from .tokens import estimate_tokens


def estimate_cacheable_prefix_tokens(
    instructions: str,
    tools: Sequence[Mapping[str, Any]],
    text_config: Mapping[str, Any] | None,
    model: str = MODEL_DEFAULT,
) -> int:
    serialized = instructions
    if tools:
        serialized += "\n" + canonical_json(tools)
    if text_config:
        serialized += "\n" + canonical_json(text_config)
    return estimate_tokens(serialized, model)


def cache_key(namespace: str) -> str:
    return "enterprise-cache-" + short_hash(namespace, 32)


def default_input(namespace: str) -> str:
    return (
        "Đây là phép đo synthetic. Trả đúng JSON theo schema với "
        f"status=CACHE_BENCHMARK_OK và namespace={namespace}."
    )


@dataclass(frozen=True)
class RequestSpec:
    order: int
    logical_request_id: str
    experiment: str
    arm: str
    namespace: str
    instructions: str
    input_text: str
    prompt_cache_key: str
    tools: tuple[dict[str, Any], ...] = ()
    text_config: dict[str, Any] | None = None
    max_output_tokens: int = 512
    expected_cache_state: str = "observe"
    cacheable_prefix_tokens_estimate: int = 0
    delay_before_seconds: float = 0.0
    batch_id: str | None = None
    optimized_cohort: bool = False
    pair_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def request_payload_fingerprint(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "instructions": self.instructions,
                    "input": self.input_text,
                    "tools": self.tools,
                    "text": self.text_config,
                    "prompt_cache_key": self.prompt_cache_key,
                    "max_output_tokens": self.max_output_tokens,
                }
            )
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "logical_request_id": self.logical_request_id,
            "experiment": self.experiment,
            "arm": self.arm,
            "namespace_sha256": sha256_text(self.namespace),
            "instructions_sha256": sha256_text(self.instructions),
            "input_sha256": sha256_text(self.input_text),
            "tools_sha256": sha256_text(canonical_json(self.tools)),
            "text_config_sha256": (
                sha256_text(canonical_json(self.text_config))
                if self.text_config
                else None
            ),
            "prompt_cache_key_sha256": sha256_text(
                self.prompt_cache_key
            ),
            "request_payload_sha256": self.request_payload_fingerprint(),
            "max_output_tokens": self.max_output_tokens,
            "expected_cache_state": self.expected_cache_state,
            "cacheable_prefix_tokens_estimate": (
                self.cacheable_prefix_tokens_estimate
            ),
            "delay_before_seconds": self.delay_before_seconds,
            "batch_id": self.batch_id,
            "optimized_cohort": self.optimized_cohort,
            "pair_id": self.pair_id,
            "metadata": self.metadata,
        }
