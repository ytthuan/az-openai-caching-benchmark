from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from benchmark_core import (
    MAX_ATTEMPTS_DEFAULT,
    MIN_REQUIRED_ATTEMPTS,
    MODEL_DEFAULT,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    PLANNED_ATTEMPTS,
    PROBE_ATTEMPTS,
    AttemptBudget,
    BenchmarkError,
    BudgetExhausted,
    ConfigurationError,
    PriceBook,
    PricingError,
    PromptAsset,
    RequestSpec,
    ValidationError,
    build_isolation_probe,
    build_suite,
    build_summary,
    cache_state_is_valid,
    cacheable_prefix_efficiency,
    canonical_json,
    compute_cost,
    endpoint_fingerprint,
    estimate_tokens,
    load_prompt_asset,
    manifest_payload,
    override_price_book,
    redact_text,
    render_markdown_report,
    resolve_endpoint_env,
    safe_slug,
    sanitize_summary,
    select_global_standard_price_book,
    sha256_text,
    usage_from_response_payload,
    utc_now_iso,
    validate_prompt_asset,
)

try:
    from openai import OpenAIError as _OpenAIError
except ImportError:
    class _OpenAIError(Exception):
        pass


ROOT_DIR = Path(__file__).resolve().parent
RUNS_DIR = ROOT_DIR / "runs"
DEFAULT_ENV_FILE = ROOT_DIR / ".env"
RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
DOTENV_ALLOWED_KEYS = (
    "OPENAI_BASER_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
)


class StreamResponseError(BenchmarkError):
    def __init__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.event_type = event_type
        self.payload = dict(payload)
        super().__init__(
            f"Responses API stream terminated with {event_type}."
        )


REQUEST_ERRORS = (
    _OpenAIError,
    StreamResponseError,
    ValidationError,
    OSError,
    ValueError,
    TypeError,
)


def load_allowed_env_file(
    path: Path,
    environ: MutableMapping[str, str],
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "found": False,
            "name": resolved.name,
            "loaded_keys": [],
            "present_keys": [],
        }
    if not resolved.is_file():
        raise ConfigurationError(f"Dotenv path is not a file: {resolved}")
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise ConfigurationError(
            "python-dotenv is required. Install requirements.txt in a venv."
        ) from exc
    values = dotenv_values(resolved, interpolate=False)
    present_keys = sorted(
        key for key in DOTENV_ALLOWED_KEYS if key in values
    )
    loaded_keys: list[str] = []
    for key in DOTENV_ALLOWED_KEYS:
        raw = values.get(key)
        value = str(raw).strip() if raw is not None else ""
        if value and not environ.get(key, "").strip():
            environ[key] = value
            loaded_keys.append(key)
    return {
        "found": True,
        "name": resolved.name,
        "loaded_keys": sorted(loaded_keys),
        "present_keys": present_keys,
    }


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, value: Mapping[str, Any]) -> None:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
                handle.flush()


def _event_payload(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    if isinstance(event, Mapping):
        return dict(event)
    return {"type": getattr(event, "type", type(event).__name__)}


def _response_payload(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    if isinstance(response, Mapping):
        return dict(response)
    raise TypeError("Unsupported response object.")


def _redact_payload(
    value: Any,
    *,
    secrets: Iterable[str],
    base_url: str,
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redact_payload(
                item,
                secrets=secrets,
                base_url=base_url,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _redact_payload(
                item,
                secrets=secrets,
                base_url=base_url,
            )
            for item in value
        ]
    if isinstance(value, str):
        return redact_text(
            value,
            secrets=secrets,
            base_url=base_url,
        )
    return value


def _extract_output_text(response_payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for item in response_payload.get("output") or []:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, Mapping):
                continue
            if content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
            elif content.get("type") == "refusal":
                parts.append(str(content.get("refusal") or ""))
    return "".join(parts)


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _http_status(exc: Exception) -> int | None:
    raw = getattr(exc, "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _request_id(exc: Exception) -> str | None:
    value = getattr(exc, "request_id", None)
    return str(value) if value else None


def _is_retryable(record: Mapping[str, Any]) -> bool:
    error = record.get("error") or {}
    if error.get("http_status") in {408, 409, 429, 500, 502, 503, 504}:
        return True
    return error.get("type") in {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "TimeoutError",
    }


def fetch_retail_price_items(
    *,
    timeout_seconds: float = 30.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = {
        "currencyCode": "USD",
        "$filter": "contains(meterName, '5.4 mini')",
    }
    url = RETAIL_PRICES_URL + "?" + urllib.parse.urlencode(query)
    items: list[dict[str, Any]] = []
    pages = 0
    retrieved_at = utc_now_iso()
    while url:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "az-openai-cache-benchmark/2.0",
            },
        )
        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds,
        ) as response:
            payload = json.load(response)
        pages += 1
        items.extend(payload.get("Items") or [])
        url = payload.get("NextPageLink") or ""
    return items, {
        "source": RETAIL_PRICES_URL,
        "filter": query["$filter"],
        "currency": "USD",
        "retrieved_at": retrieved_at,
        "pages": pages,
        "items": len(items),
    }


def resolve_price_book(args: argparse.Namespace) -> PriceBook | None:
    overrides = (
        args.input_price,
        args.cached_input_price,
        args.output_price,
    )
    if any(value is not None for value in overrides):
        if not all(value is not None for value in overrides):
            raise PricingError(
                "Provide all three price overrides or none of them."
            )
        return override_price_book(
            input_price=float(args.input_price),
            cached_input_price=float(args.cached_input_price),
            output_price=float(args.output_price),
        )
    if args.skip_pricing:
        return None
    items, retrieval = fetch_retail_price_items(
        timeout_seconds=args.pricing_timeout
    )
    return select_global_standard_price_book(
        items,
        retrieved_at=retrieval["retrieved_at"],
    )


def expected_cost_envelope(
    specs: Sequence[RequestSpec],
    price_book: PriceBook | None,
    model: str,
    max_attempts: int,
) -> dict[str, Any] | None:
    if price_book is None:
        return None
    planned_input = sum(
        spec.cacheable_prefix_tokens_estimate
        + estimate_tokens(spec.input_text, model)
        for spec in specs
    )
    planned_output = sum(spec.max_output_tokens for spec in specs)
    reserve_attempts = max(0, max_attempts - len(specs))
    maximum_input = max(
        (
            spec.cacheable_prefix_tokens_estimate
            + estimate_tokens(spec.input_text, model)
            for spec in specs
        ),
        default=0,
    )
    maximum_output = max(
        (spec.max_output_tokens for spec in specs),
        default=0,
    )
    reserve_input = reserve_attempts * maximum_input
    reserve_output = reserve_attempts * maximum_output
    estimated_input = planned_input + reserve_input
    output_ceiling = planned_output + reserve_output
    no_cache_input_usd = (
        estimated_input
        * price_book.input_usd_per_million
        / 1_000_000
    )
    maximum_output_usd = (
        output_ceiling
        * price_book.output_usd_per_million
        / 1_000_000
    )
    return {
        "planned_attempts": len(specs),
        "reserve_attempts": reserve_attempts,
        "capability_probe_attempts": PROBE_ATTEMPTS,
        "retry_reserve_attempts": max(
            0,
            reserve_attempts - PROBE_ATTEMPTS,
        ),
        "planned_estimated_input_tokens": planned_input,
        "reserve_input_tokens_ceiling": reserve_input,
        "estimated_input_tokens": estimated_input,
        "planned_maximum_output_tokens": planned_output,
        "reserve_output_tokens_ceiling": reserve_output,
        "maximum_output_tokens": output_ceiling,
        "no_cache_input_usd": no_cache_input_usd,
        "maximum_output_usd": maximum_output_usd,
        "conservative_total_usd": (
            no_cache_input_usd + maximum_output_usd
        ),
        "note": (
            "Hard-cap-aware no-cache ceiling. Reserve attempts use the "
            "largest planned request and output cap."
        ),
    }


class ResponseRunner:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        base_url: str,
        api_key: str,
        budget: AttemptBudget,
        writer: JsonlWriter,
        price_book: PriceBook | None,
        max_retries: int,
    ) -> None:
        self.client = client
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.budget = budget
        self.writer = writer
        self.price_book = price_book
        self.max_retries = max_retries
        self.records: list[dict[str, Any]] = []
        self._records_lock = threading.Lock()
        self._last_arm_start: dict[tuple[str, str], float] = {}
        self._arm_lock = threading.Lock()

    def _request_kwargs(self, spec: RequestSpec) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": spec.instructions,
            "input": spec.input_text,
            "reasoning": {"effort": "low"},
            "max_output_tokens": spec.max_output_tokens,
            "prompt_cache_key": spec.prompt_cache_key,
            "store": False,
            "stream": True,
        }
        if spec.tools:
            kwargs["tools"] = list(spec.tools)
            kwargs["tool_choice"] = "none"
        if spec.text_config:
            kwargs["text"] = spec.text_config
        return kwargs

    def _gap_seconds(self, spec: RequestSpec, now: float) -> float | None:
        key = (spec.experiment, spec.arm)
        with self._arm_lock:
            previous = self._last_arm_start.get(key)
            self._last_arm_start[key] = now
        return now - previous if previous is not None else None

    def _base_record(
        self,
        spec: RequestSpec,
        *,
        attempt_number: int,
        retry_index: int,
        started_at: str,
        gap_seconds: float | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "2.0.0",
            "attempt_number": attempt_number,
            "retry_index": retry_index,
            "logical_request_id": spec.logical_request_id,
            "order": spec.order,
            "experiment": spec.experiment,
            "arm": spec.arm,
            "expected_cache_state": spec.expected_cache_state,
            "optimized_cohort": spec.optimized_cohort,
            "pair_id": spec.pair_id,
            "cacheable_prefix_tokens_estimate": (
                spec.cacheable_prefix_tokens_estimate
            ),
            "namespace_sha256": sha256_text(spec.namespace),
            "instructions_sha256": sha256_text(spec.instructions),
            "input_sha256": sha256_text(spec.input_text),
            "tools_sha256": sha256_text(canonical_json(spec.tools)),
            "text_config_sha256": (
                sha256_text(canonical_json(spec.text_config))
                if spec.text_config
                else None
            ),
            "prompt_cache_key_sha256": sha256_text(
                spec.prompt_cache_key
            ),
            "request_payload_sha256": spec.request_payload_fingerprint(),
            "metadata": spec.metadata,
            "gap_from_previous_same_arm_seconds": gap_seconds,
            "started_at": started_at,
        }

    def _execute_once(
        self,
        spec: RequestSpec,
        *,
        retry_index: int,
    ) -> dict[str, Any]:
        attempt_number = self.budget.claim(
            logical_request_id=spec.logical_request_id,
            reason="initial" if retry_index == 0 else "retry",
        )
        started_at = utc_now_iso()
        started = time.perf_counter()
        base_record = self._base_record(
            spec,
            attempt_number=attempt_number,
            retry_index=retry_index,
            started_at=started_at,
            gap_seconds=self._gap_seconds(spec, started),
        )
        first_event_at: float | None = None
        first_text_at: float | None = None
        terminal_at: float | None = None
        text_parts: list[str] = []
        terminal_payload: dict[str, Any] | None = None
        event_counts: dict[str, int] = {}
        try:
            stream = self.client.responses.create(
                **self._request_kwargs(spec)
            )
            for event in stream:
                now = time.perf_counter()
                event_type = str(getattr(event, "type", "unknown"))
                event_counts[event_type] = event_counts.get(event_type, 0) + 1
                if first_event_at is None:
                    first_event_at = now
                if event_type == "response.output_text.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta and first_text_at is None:
                        first_text_at = now
                    text_parts.append(delta)
                elif event_type == "response.completed":
                    terminal_payload = _response_payload(
                        getattr(event, "response", None)
                    )
                    terminal_at = now
                elif event_type in {
                    "response.failed",
                    "response.incomplete",
                    "error",
                }:
                    raise StreamResponseError(
                        event_type,
                        _event_payload(event),
                    )
            if terminal_payload is None:
                raise StreamResponseError(
                    "missing_response.completed",
                    {"event_counts": event_counts},
                )
            if terminal_payload.get("status") != "completed":
                raise StreamResponseError(
                    "terminal_response_not_completed",
                    {
                        "status": terminal_payload.get("status"),
                        "incomplete_details": terminal_payload.get(
                            "incomplete_details"
                        ),
                    },
                )
            terminal_at = terminal_at or time.perf_counter()
            output_text = "".join(text_parts) or _extract_output_text(
                terminal_payload
            )
            usage = usage_from_response_payload(terminal_payload)
            visible_tokens = (
                estimate_tokens(output_text, self.model)
                if output_text
                else 0
            )
            first_event_ms = (
                (first_event_at - started) * 1_000
                if first_event_at is not None
                else None
            )
            first_text_ms = (
                (first_text_at - started) * 1_000
                if first_text_at is not None
                else None
            )
            ttlt_ms = (terminal_at - started) * 1_000
            tbt_ms = (
                (terminal_at - first_text_at) * 1_000 / (visible_tokens - 1)
                if first_text_at is not None and visible_tokens > 1
                else None
            )
            efficiency = cacheable_prefix_efficiency(
                usage["cached_tokens"],
                spec.cacheable_prefix_tokens_estimate,
            )
            return {
                **base_record,
                "status": "completed",
                "completed_at": utc_now_iso(),
                "response_id": terminal_payload.get("id"),
                "response_status": terminal_payload.get("status"),
                "model": terminal_payload.get("model"),
                "usage": usage,
                "cost": (
                    compute_cost(usage, self.price_book)
                    if self.price_book
                    else None
                ),
                "cacheable_prefix_efficiency": efficiency,
                "cache_state_valid": cache_state_is_valid(
                    spec.expected_cache_state,
                    cached_tokens=usage["cached_tokens"],
                    cacheable_prefix_tokens_estimate=(
                        spec.cacheable_prefix_tokens_estimate
                    ),
                ),
                "timing": {
                    "first_event_ms": first_event_ms,
                    "first_text_ms": first_text_ms,
                    "ttlt_ms": ttlt_ms,
                    "tbt_ms": tbt_ms,
                    "visible_output_tokens_estimate": visible_tokens,
                },
                "event_counts": event_counts,
                "output_sha256": sha256_text(output_text),
            }
        except REQUEST_ERRORS as exc:
            stream_payload = (
                exc.payload
                if isinstance(exc, StreamResponseError)
                else None
            )
            return {
                **base_record,
                "status": "failed",
                "completed_at": utc_now_iso(),
                "timing": {
                    "first_event_ms": (
                        (first_event_at - started) * 1_000
                        if first_event_at is not None
                        else None
                    ),
                    "first_text_ms": (
                        (first_text_at - started) * 1_000
                        if first_text_at is not None
                        else None
                    ),
                    "ttlt_ms": (
                        time.perf_counter() - started
                    ) * 1_000,
                    "tbt_ms": None,
                },
                "event_counts": event_counts,
                "error": {
                    "type": type(exc).__name__,
                    "message": redact_text(
                        str(exc),
                        secrets=(self.api_key,),
                        base_url=self.base_url,
                    ),
                    "http_status": _http_status(exc),
                    "request_id": _request_id(exc),
                    "retry_after_seconds": _retry_after_seconds(exc),
                    "stream_payload": _redact_payload(
                        stream_payload,
                        secrets=(self.api_key,),
                        base_url=self.base_url,
                    ),
                },
            }

    def _store_record(self, record: dict[str, Any]) -> None:
        with self._records_lock:
            self.records.append(record)
        self.writer.write(record)

    def run_spec(
        self,
        spec: RequestSpec,
        *,
        allow_retry: bool,
    ) -> dict[str, Any]:
        retry_limit = self.max_retries if allow_retry else 0
        final_record: dict[str, Any] | None = None
        for retry_index in range(retry_limit + 1):
            record = self._execute_once(
                spec,
                retry_index=retry_index,
            )
            self._store_record(record)
            final_record = record
            if record["status"] == "completed":
                return record
            if retry_index >= retry_limit or not _is_retryable(record):
                break
            retry_after = (
                (record.get("error") or {}).get("retry_after_seconds")
            )
            delay = (
                float(retry_after)
                if retry_after is not None
                else min(8.0, 2.0**retry_index)
            )
            time.sleep(delay)
        assert final_record is not None
        return final_record


def _run_burst(
    runner: ResponseRunner,
    specs: Sequence[RequestSpec],
) -> None:
    with ThreadPoolExecutor(max_workers=len(specs)) as executor:
        futures = [
            executor.submit(
                runner.run_spec,
                spec,
                allow_retry=False,
            )
            for spec in specs
        ]
        for future in as_completed(futures):
            future.result()


def _validate_capability_probe(
    specs: Sequence[RequestSpec],
    records: Sequence[Mapping[str, Any]],
) -> None:
    if len(records) != PROBE_ATTEMPTS:
        raise ValidationError(
            "Capability/isolation probe did not complete three requests."
        )
    for spec, record in zip(specs, records):
        if record.get("status") != "completed":
            raise ValidationError(
                f"Capability/isolation probe failed for {spec.arm}."
            )
        usage = record.get("usage") or {}
        if not cache_state_is_valid(
            spec.expected_cache_state,
            cached_tokens=int(usage.get("cached_tokens") or 0),
            cacheable_prefix_tokens_estimate=(
                spec.cacheable_prefix_tokens_estimate
            ),
        ):
            raise ValidationError(
                "Capability/isolation probe cache state invalid for "
                f"{spec.arm}: cached_tokens={usage.get('cached_tokens')}."
            )


def _validate_under_threshold(records: Sequence[Mapping[str, Any]]) -> None:
    for record in records:
        if (
            record.get("experiment") == "qualification"
            and record.get("arm") == "under-threshold"
            and record.get("status") == "completed"
        ):
            input_tokens = int(
                (record.get("usage") or {}).get("input_tokens") or 0
            )
            if input_tokens >= 1_024:
                raise ValidationError(
                    "Qualification control reached 1,024 input tokens."
                )


def execute_plan(
    *,
    runner: ResponseRunner,
    specs: Sequence[RequestSpec],
) -> None:
    index = 0
    while index < len(specs):
        spec = specs[index]
        if spec.delay_before_seconds > 0:
            time.sleep(spec.delay_before_seconds)
        if spec.batch_id:
            batch_id = spec.batch_id
            batch: list[RequestSpec] = []
            while index < len(specs) and specs[index].batch_id == batch_id:
                batch.append(specs[index])
                index += 1
            _run_burst(runner, batch)
            continue
        allow_retry = spec.experiment != "matched_latency"
        record = runner.run_spec(spec, allow_retry=allow_retry)
        if (
            spec.experiment == "qualification"
            and spec.arm == "under-threshold"
            and record.get("status") == "completed"
        ):
            _validate_under_threshold(runner.records)
        index += 1


def _write_summary_csv(
    path: Path,
    summary: Mapping[str, Any],
) -> None:
    fields = [
        "experiment_arm",
        "logical_requests",
        "completed",
        "failed",
        "request_hit_rate",
        "substantive_request_hit_rate",
        "token_weighted_cache_rate",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "first_text_p50_ms",
        "ttlt_p50_ms",
        "actual_cost_usd",
        "no_cache_cost_usd",
        "input_savings_rate",
        "total_savings_rate",
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for name, metric in (summary.get("arms") or {}).items():
            usage = metric.get("usage") or {}
            timing = metric.get("timing") or {}
            cost = metric.get("cost") or {}
            writer.writerow(
                {
                    "experiment_arm": name,
                    "logical_requests": metric.get("logical_requests"),
                    "completed": metric.get("completed"),
                    "failed": metric.get("failed"),
                    "request_hit_rate": metric.get("request_hit_rate"),
                    "substantive_request_hit_rate": metric.get(
                        "substantive_request_hit_rate"
                    ),
                    "token_weighted_cache_rate": metric.get(
                        "token_weighted_cache_rate"
                    ),
                    "input_tokens": usage.get("input_tokens"),
                    "cached_tokens": usage.get("cached_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "first_text_p50_ms": (
                        timing.get("first_text_ms") or {}
                    ).get("p50"),
                    "ttlt_p50_ms": (
                        timing.get("ttlt_ms") or {}
                    ).get("p50"),
                    "actual_cost_usd": cost.get("actual_usd"),
                    "no_cache_cost_usd": cost.get("no_cache_usd"),
                    "input_savings_rate": cost.get("input_savings_rate"),
                    "total_savings_rate": cost.get("total_savings_rate"),
                }
            )
    temporary.replace(path)


def _create_run_dir(output_root: Path, run_id: str) -> Path:
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _new_run_id(prefix: str) -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{prefix}-{timestamp}-{uuid.uuid4().hex[:8]}"


def _load_context(
    args: argparse.Namespace,
) -> tuple[PromptAsset, list[RequestSpec]]:
    asset = load_prompt_asset(model=args.model)
    errors = validate_prompt_asset(asset)
    if errors:
        raise ValidationError("; ".join(errors))
    specs = build_suite(
        run_id=args.run_id,
        asset=asset,
        model=args.model,
        idle_gap_seconds=args.idle_gap_seconds,
        paced_interval_seconds=args.paced_interval_seconds,
    )
    return asset, specs


def run_dry(args: argparse.Namespace) -> int:
    asset, specs = _load_context(args)
    price_book = resolve_price_book(args)
    run_dir = _create_run_dir(args.output_root, args.run_id)
    manifest = manifest_payload(
        run_id=args.run_id,
        model=args.model,
        asset=asset,
        specs=specs,
        max_attempts=args.max_attempts,
        price_book=price_book,
    )
    manifest["mode"] = "dry-run"
    manifest["validation"] = {
        "status": "passed",
        "planned_requests": len(specs),
        "optimized_expected_requests": (
            OPTIMIZED_COHORT_EXPECTED_REQUESTS
        ),
        "probe_requests": PROBE_ATTEMPTS,
    }
    manifest["expected_cost_envelope"] = expected_cost_envelope(
        specs,
        price_book,
        args.model,
        args.max_attempts,
    )
    _atomic_write_json(run_dir / "manifest.json", manifest)
    print(f"Dry-run valid: {len(specs)} planned requests.")
    print(
        f"Prompt: {asset.word_count} words, "
        f"{asset.estimated_tokens} estimated tokens, SHA-256 {asset.sha256}."
    )
    envelope = manifest["expected_cost_envelope"]
    if envelope:
        print(
            "Conservative no-cache envelope: "
            f"${envelope['conservative_total_usd']:.6f}."
        )
    else:
        print("Conservative no-cache envelope: n/a (pricing skipped).")
    print(f"Manifest: {run_dir / 'manifest.json'}")
    return 0


def _finalize_live_artifacts(
    *,
    run_dir: Path,
    manifest: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    price_book: PriceBook | None,
    budget: AttemptBudget,
    run_status: str,
    fatal_error: str | None,
) -> dict[str, Any]:
    completed_at = utc_now_iso()
    snapshot = budget.snapshot()
    manifest["execution"] = {
        "status": run_status,
        "completed_at": completed_at,
        "attempt_budget": snapshot,
        "fatal_error": fatal_error,
    }
    _atomic_write_json(run_dir / "manifest.json", manifest)
    summary = build_summary(
        run_id=str(manifest["run_id"]),
        model=str(manifest["model"]),
        records=records,
        price_book=price_book,
        attempt_budget=snapshot,
        run_status=run_status,
        completed_at=completed_at,
        fatal_error=fatal_error,
        prompt_metadata=manifest.get("prompt"),
    )
    _atomic_write_json(run_dir / "summary.json", summary)
    _write_summary_csv(run_dir / "summary.csv", summary)
    _atomic_write_text(
        run_dir / "report.md",
        render_markdown_report(summary),
    )
    return summary


def _has_final_failures(records: Sequence[Mapping[str, Any]]) -> bool:
    latest: dict[str, Mapping[str, Any]] = {}
    for record in records:
        logical_id = str(record.get("logical_request_id") or "")
        if logical_id:
            latest[logical_id] = record
    return any(record.get("status") != "completed" for record in latest.values())


def run_live(args: argparse.Namespace) -> int:
    if not args.confirm_live:
        raise ConfigurationError(
            "Live mode requires --confirm-live after reviewing dry-run cost."
        )
    load_info = load_allowed_env_file(args.env_file, os.environ)
    base_url, endpoint_env = resolve_endpoint_env(os.environ)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigurationError("OPENAI_API_KEY is required for live mode.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ConfigurationError(
            "openai is required. Install requirements.txt in a venv."
        ) from exc

    asset, specs = _load_context(args)
    price_book = resolve_price_book(args)
    run_dir = _create_run_dir(args.output_root, args.run_id)
    manifest = manifest_payload(
        run_id=args.run_id,
        model=args.model,
        asset=asset,
        specs=specs,
        max_attempts=args.max_attempts,
        endpoint=endpoint_fingerprint(base_url),
        price_book=price_book,
    )
    manifest["mode"] = "live"
    manifest["endpoint_env"] = endpoint_env
    manifest["env_file"] = load_info
    manifest["expected_cost_envelope"] = expected_cost_envelope(
        specs,
        price_book,
        args.model,
        args.max_attempts,
    )
    _atomic_write_json(run_dir / "manifest.json", manifest)

    writer = JsonlWriter(run_dir / "requests.jsonl")
    budget = AttemptBudget(args.max_attempts)
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=args.timeout,
        max_retries=0,
    )
    runner = ResponseRunner(
        client=client,
        model=args.model,
        base_url=base_url,
        api_key=api_key,
        budget=budget,
        writer=writer,
        price_book=price_book,
        max_retries=args.max_retries,
    )
    run_status = "completed"
    fatal_error: str | None = None
    try:
        probe_specs = build_isolation_probe(
            run_id=args.run_id,
            asset=asset,
            model=args.model,
        )
        probe_records = [
            runner.run_spec(spec, allow_retry=False)
            for spec in probe_specs
        ]
        _validate_capability_probe(probe_specs, probe_records)
        execute_plan(runner=runner, specs=specs)
        if _has_final_failures(runner.records):
            run_status = "completed_with_findings"
    except KeyboardInterrupt:
        run_status = "incomplete"
        fatal_error = "KeyboardInterrupt: interrupted by operator"
    except (
        BudgetExhausted,
        BenchmarkError,
        _OpenAIError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        run_status = "incomplete"
        fatal_error = redact_text(
            f"{type(exc).__name__}: {exc}",
            secrets=(api_key,),
            base_url=base_url,
        )
    finally:
        client.close()

    summary = _finalize_live_artifacts(
        run_dir=run_dir,
        manifest=manifest,
        records=runner.records,
        price_book=price_book,
        budget=budget,
        run_status=run_status,
        fatal_error=fatal_error,
    )
    print(
        f"Run {run_status}: {budget.used}/{budget.maximum} HTTP attempts."
    )
    print(
        "Optimized cache: "
        f"{summary['optimized']['token_weighted_cache_rate']!r}; "
        f"acceptance: {summary['acceptance']['status']}."
    )
    print(f"Report: {run_dir / 'report.md'}")
    return 0 if run_status != "incomplete" else 2


def _read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    line_number = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ConfigurationError(
                        f"{path}:{line_number} is not an object."
                    )
                records.append(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Invalid JSON in {path}:{line_number}: {exc.msg}"
        ) from exc
    return records


def rerender_report(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    manifest_path = run_dir / "manifest.json"
    requests_path = run_dir / "requests.jsonl"
    for path in (manifest_path, requests_path):
        if not path.is_file():
            raise ConfigurationError(f"Missing {path}.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = _read_records(requests_path)
    pricing_payload = manifest.get("pricing")
    try:
        price_book = (
            PriceBook(**pricing_payload)
            if isinstance(pricing_payload, dict)
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"Invalid pricing payload in {manifest_path}: {exc}"
        ) from exc
    execution = manifest.get("execution") or {}
    attempt_budget = execution.get("attempt_budget")
    completed_at = execution.get("completed_at")
    if not isinstance(attempt_budget, Mapping) or not completed_at:
        raise ConfigurationError(
            "Manifest lacks execution attempt budget or completed_at."
        )
    summary = build_summary(
        run_id=str(manifest["run_id"]),
        model=str(manifest["model"]),
        records=records,
        price_book=price_book,
        attempt_budget=attempt_budget,
        run_status=str(execution.get("status") or "incomplete"),
        completed_at=str(completed_at),
        fatal_error=execution.get("fatal_error"),
        prompt_metadata=manifest.get("prompt"),
    )
    _atomic_write_json(run_dir / "summary.json", summary)
    _write_summary_csv(run_dir / "summary.csv", summary)
    _atomic_write_text(
        run_dir / "report.md",
        render_markdown_report(summary),
    )
    print(f"Summary: {run_dir / 'summary.json'}")
    print(f"Report: {run_dir / 'report.md'}")
    return 0


def write_sanitized_examples(
    summary_path: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    sanitized = sanitize_summary(summary)
    summary_output = output_dir / "sample-summary.json"
    report_output = output_dir / "sample-report.md"
    _atomic_write_json(summary_output, sanitized)
    _atomic_write_text(
        report_output,
        render_markdown_report(sanitized, sanitized_sample=True),
    )
    return summary_output, report_output


def run_sample(args: argparse.Namespace) -> int:
    summary_path, report_path = write_sanitized_examples(
        args.summary,
        args.output_dir,
    )
    print(f"Sample summary: {summary_path}")
    print(f"Sample report: {report_path}")
    return 0


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=MODEL_DEFAULT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=RUNS_DIR,
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=(
            "Dotenv file for three allowlisted variables. Existing process "
            "values take precedence."
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=MAX_ATTEMPTS_DEFAULT,
    )
    parser.add_argument(
        "--idle-gap-seconds",
        type=float,
        default=660.0,
    )
    parser.add_argument(
        "--paced-interval-seconds",
        type=float,
        default=4.2,
    )
    parser.add_argument("--skip-pricing", action="store_true")
    parser.add_argument("--pricing-timeout", type=float, default=30.0)
    parser.add_argument("--input-price", type=float)
    parser.add_argument("--cached-input-price", type=float)
    parser.add_argument("--output-price", type=float)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone Azure OpenAI prompt-cache benchmark."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    dry = subparsers.add_parser(
        "dry-run",
        help="Validate suite and cost ceiling without model calls.",
    )
    _add_common_arguments(dry)
    live = subparsers.add_parser(
        "live",
        help="Run capped benchmark against configured endpoint.",
    )
    _add_common_arguments(live)
    live.add_argument("--confirm-live", action="store_true")
    live.add_argument("--timeout", type=float, default=180.0)
    live.add_argument("--max-retries", type=int, default=1)
    report = subparsers.add_parser(
        "report",
        help="Rebuild derived files from manifest.json and requests.jsonl.",
    )
    report.add_argument("run_dir", type=Path)
    sample = subparsers.add_parser(
        "sample",
        help="Write allowlisted sample files from a live summary.",
    )
    sample.add_argument("summary", type=Path)
    sample.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT_DIR / "examples",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"dry-run", "live"}:
        if args.run_id is None:
            args.run_id = _new_run_id(
                "dry" if args.command == "dry-run" else "live"
            )
        elif safe_slug(args.run_id) != args.run_id:
            parser.error(
                "--run-id may contain only letters, numbers, underscores "
                "and hyphens."
            )
        if args.idle_gap_seconds < 0:
            parser.error("--idle-gap-seconds cannot be negative.")
        if args.paced_interval_seconds < 0:
            parser.error("--paced-interval-seconds cannot be negative.")
        if args.pricing_timeout <= 0:
            parser.error("--pricing-timeout must be positive.")
        if not MIN_REQUIRED_ATTEMPTS <= args.max_attempts <= MAX_ATTEMPTS_DEFAULT:
            parser.error(
                f"--max-attempts must be between {MIN_REQUIRED_ATTEMPTS} "
                f"and {MAX_ATTEMPTS_DEFAULT}."
            )
        if args.command == "live":
            if args.timeout <= 0:
                parser.error("--timeout must be positive.")
            if args.max_retries < 0:
                parser.error("--max-retries cannot be negative.")
    try:
        if args.command == "dry-run":
            return run_dry(args)
        if args.command == "live":
            return run_live(args)
        if args.command == "report":
            return rerender_report(args)
        return run_sample(args)
    except (
        BenchmarkError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
