from __future__ import annotations

from pathlib import Path

from azure_openai_cache_benchmark.specs import RequestSpec

ROOT = Path(__file__).resolve().parents[1]


class FakeEvent:
    def __init__(self, event_type: str, **values: object) -> None:
        self.type = event_type
        for key, value in values.items():
            setattr(self, key, value)


class DumpingEvent(FakeEvent):
    def __init__(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        super().__init__(event_type)
        self.payload = payload

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {"type": self.type, **self.payload}


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return self.payload


class FakeResponses:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    def create(self, **kwargs: object):
        self.kwargs = kwargs
        terminal = FakeResponse(
            {
                "id": "resp-unit",
                "status": "completed",
                "model": "gpt-5.4-mini",
                "usage": {
                    "input_tokens": 2_000,
                    "input_tokens_details": {"cached_tokens": 1_800},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 10},
                    "total_tokens": 2_020,
                },
                "output": [],
            }
        )
        return iter(
            (
                FakeEvent("response.created"),
                FakeEvent(
                    "response.output_text.delta",
                    delta="CACHE_BENCHMARK_OK",
                ),
                FakeEvent("response.completed", response=terminal),
            )
        )


def unit_spec(**overrides: object) -> RequestSpec:
    values: dict[str, object] = {
        "order": 1,
        "logical_request_id": "unit-request",
        "experiment": "unit",
        "arm": "warm",
        "namespace": "unit",
        "instructions": "x" * 4_000,
        "input_text": "synthetic",
        "prompt_cache_key": "cache-key",
        "expected_cache_state": "warm",
        "cacheable_prefix_tokens_estimate": 2_000,
    }
    values.update(overrides)
    return RequestSpec(**values)


def completed_record(
    spec: RequestSpec,
    *,
    cached_tokens: int | None = None,
    attempt_number: int | None = None,
) -> dict[str, object]:
    prefix = spec.cacheable_prefix_tokens_estimate
    input_tokens = prefix + 128
    if cached_tokens is None:
        if spec.expected_cache_state == "warm":
            cached_tokens = min(
                input_tokens,
                max(1_024, int(prefix * 0.90)),
            )
        else:
            cached_tokens = 0
    return {
        "attempt_number": attempt_number or spec.order,
        "retry_index": 0,
        "status": "completed",
        "logical_request_id": spec.logical_request_id,
        "order": spec.order,
        "experiment": spec.experiment,
        "arm": spec.arm,
        "expected_cache_state": spec.expected_cache_state,
        "optimized_cohort": spec.optimized_cohort,
        "pair_id": spec.pair_id,
        "cacheable_prefix_tokens_estimate": prefix,
        "cacheable_prefix_efficiency": (
            cached_tokens / prefix if prefix else None
        ),
        "metadata": spec.metadata,
        "usage": {
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "uncached_input_tokens": input_tokens - cached_tokens,
            "output_tokens": 24,
            "reasoning_tokens": 8,
            "total_tokens": input_tokens + 24,
        },
        "timing": {
            "first_event_ms": 20.0,
            "first_text_ms": 40.0 if cached_tokens else 55.0,
            "ttlt_ms": 100.0 if cached_tokens else 130.0,
            "tbt_ms": 4.0,
        },
    }
