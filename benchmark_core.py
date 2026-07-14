from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import threading
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


MODEL_DEFAULT = "gpt-5.4-mini"
MAX_ATTEMPTS_DEFAULT = 120
PLANNED_ATTEMPTS = 102
PROBE_ATTEMPTS = 3
MIN_REQUIRED_ATTEMPTS = PLANNED_ATTEMPTS + PROBE_ATTEMPTS
MIN_CACHEABLE_TOKENS = 1_024
WARM_PREFIX_EFFICIENCY_FLOOR = 0.80
ROOT_CAUSE_DELTA_FLOOR = 0.10
ROOT_CAUSE_MIN_COMPLETED = 5
OPTIMIZED_COHORT_EXPECTED_REQUESTS = 44
MATCHED_PAIR_COUNT = 10
MATCHED_MAX_OUTPUT_TOKENS = 512
OPTIMIZED_REQUEST_HIT_FLOOR = 0.90
OPTIMIZED_TOKEN_CACHE_FLOOR = 0.80
OPTIMIZED_PREFIX_P50_FLOOR = 0.90

PROJECT_DIR = Path(__file__).resolve().parent
PROMPT_PATH = (
    PROJECT_DIR
    / "prompts"
    / "enterprise_agent_creator_system_prompt_vi.md"
)
DYNAMIC_START = (
    "=== BẮT ĐẦU PHẦN ĐỘNG — NGỮ CẢNH RUNTIME "
    "(KHÔNG CÓ THẨM QUYỀN CHÍNH SÁCH) ==="
)
DYNAMIC_END = "=== KẾT THÚC PHẦN ĐỘNG ==="

PRICE_SKUS = {
    "input": "5.4 mini Inp Gl",
    "cached_input": "5.4 mini cd Inp Gl",
    "output": "5.4 mini Opt Gl",
}
PROMPT_CACHE_DOC_URL = (
    "https://learn.microsoft.com/azure/ai-services/openai/how-to/prompt-caching"
)
RESPONSES_DOC_URL = (
    "https://learn.microsoft.com/azure/ai-services/openai/how-to/responses"
)
LATENCY_DOC_URL = (
    "https://learn.microsoft.com/azure/ai-services/openai/how-to/latency"
)
RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
OFFICIAL_SOURCES = [
    PROMPT_CACHE_DOC_URL,
    RESPONSES_DOC_URL,
    LATENCY_DOC_URL,
    RETAIL_PRICES_URL,
]

EXPECTED_ALLOCATION = {
    "qualification": 6,
    "prefix_stability": 20,
    "tool_schema_stability": 20,
    "cache_key_cardinality": 20,
    "traffic_shape": 12,
    "idle_retention": 4,
    "matched_latency": 20,
}
DEGRADED_ARMS = {
    ("prefix_stability", "volatile-prefix"),
    ("tool_schema_stability", "shuffled"),
    ("cache_key_cardinality", "per-request-key"),
    ("traffic_shape", "burst"),
}
SAMPLE_SUMMARY_ALLOWLIST = {
    "schema_version",
    "sample",
    "run_id",
    "model",
    "generated_at",
    "run_status",
    "suite",
    "records",
    "overall",
    "optimized",
    "degraded",
    "arms",
    "root_causes",
    "matched_latency",
    "acceptance",
    "pricing",
    "prompt",
    "provenance",
}


class BenchmarkError(RuntimeError):
    pass


class ConfigurationError(BenchmarkError):
    pass


class ValidationError(BenchmarkError):
    pass


class PricingError(BenchmarkError):
    pass


class BudgetExhausted(BenchmarkError):
    pass


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def short_hash(value: str, length: int = 12) -> str:
    return sha256_text(value)[:length]


def safe_slug(value: str, max_length: int = 64) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")
    if not slug:
        raise ValidationError("Identifier does not contain safe characters.")
    return slug[:max_length]


def count_words(value: str) -> int:
    return len(re.findall(r"\S+", value))


def normalize_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ConfigurationError("Azure OpenAI base URL is empty.")
    parsed = urlsplit(raw)
    hostname = parsed.hostname or ""
    is_loopback = hostname.casefold() == "localhost"
    try:
        is_loopback = (
            is_loopback or ipaddress.ip_address(hostname).is_loopback
        )
    except ValueError:
        pass
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("Base URL must not contain user information.")
    scheme_allowed = parsed.scheme == "https" or (
        parsed.scheme == "http" and is_loopback
    )
    if not scheme_allowed or not parsed.netloc:
        raise ConfigurationError(
            "Base URL must be absolute HTTPS, or HTTP on a loopback proxy."
        )
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "Azure OpenAI base URL must not contain query or fragment data."
        )
    path = parsed.path.rstrip("/")
    lowered = path.casefold()
    if "/openai/deployments/" in lowered:
        raise ConfigurationError(
            "Use the Azure OpenAI v1 root, not a deployment-specific URL."
        )
    if lowered.endswith("/openai/v1"):
        normalized_path = path + "/"
    elif is_loopback and lowered.endswith("/v1"):
        normalized_path = path + "/"
    elif is_loopback and not path:
        normalized_path = "/v1/"
    elif lowered.endswith("/openai"):
        normalized_path = path + "/v1/"
    elif not path:
        normalized_path = "/openai/v1/"
    else:
        normalized_path = path + "/openai/v1/"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, normalized_path, "", "")
    )


def resolve_endpoint_env(env: Mapping[str, str]) -> tuple[str, str]:
    preferred = env.get("OPENAI_BASER_URL", "").strip()
    standard = env.get("OPENAI_BASE_URL", "").strip()
    if preferred:
        return normalize_base_url(preferred), "OPENAI_BASER_URL"
    if standard:
        return normalize_base_url(standard), "OPENAI_BASE_URL"
    raise ConfigurationError(
        "Set OPENAI_BASER_URL or OPENAI_BASE_URL for Azure OpenAI v1."
    )


def endpoint_fingerprint(base_url: str) -> dict[str, str]:
    parsed = urlsplit(base_url)
    return {
        "scheme": parsed.scheme,
        "hostname_sha256": sha256_text(parsed.hostname or ""),
        "path": parsed.path,
    }


def redact_text(
    value: str,
    secrets: Iterable[str] = (),
    base_url: str | None = None,
) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED_SECRET]")
    if base_url:
        parsed = urlsplit(base_url)
        for endpoint_value in (base_url, parsed.netloc, parsed.hostname or ""):
            if endpoint_value:
                redacted = redacted.replace(
                    endpoint_value,
                    "[REDACTED_ENDPOINT]",
                )
    return re.sub(
        r"(?i)(api[-_ ]?key\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED_SECRET]",
        redacted,
    )


def get_encoding(model: str = MODEL_DEFAULT) -> tuple[Any, str]:
    try:
        import tiktoken
    except ImportError as exc:
        raise ConfigurationError(
            "tiktoken is required. Install requirements.txt in a venv."
        ) from exc
    try:
        return tiktoken.encoding_for_model(model), f"model:{model}"
    except KeyError:
        return tiktoken.get_encoding("o200k_base"), "fallback:o200k_base"


def estimate_tokens(value: str, model: str = MODEL_DEFAULT) -> int:
    encoding, _ = get_encoding(model)
    return len(encoding.encode(value))


def truncate_to_tokens(
    value: str,
    token_limit: int,
    model: str = MODEL_DEFAULT,
) -> str:
    encoding, _ = get_encoding(model)
    return encoding.decode(encoding.encode(value)[:token_limit])


@dataclass(frozen=True)
class PromptAsset:
    path: str
    template: str
    sha256: str
    word_count: int
    estimated_tokens: int
    encoding: str


def load_prompt_asset(
    path: Path = PROMPT_PATH,
    model: str = MODEL_DEFAULT,
) -> PromptAsset:
    template = path.read_text(encoding="utf-8")
    encoding, encoding_name = get_encoding(model)
    try:
        safe_path = str(path.resolve().relative_to(PROJECT_DIR))
    except ValueError:
        safe_path = path.name
    return PromptAsset(
        path=safe_path,
        template=template,
        sha256=sha256_text(template),
        word_count=count_words(template),
        estimated_tokens=len(encoding.encode(template)),
        encoding=encoding_name,
    )


def validate_prompt_asset(asset: PromptAsset) -> list[str]:
    errors: list[str] = []
    if not 4_000 <= asset.word_count <= 5_000:
        errors.append(
            f"Prompt word count {asset.word_count} is outside 4,000-5,000."
        )
    if asset.template.count(DYNAMIC_START) != 1:
        errors.append("Prompt must contain exactly one dynamic start marker.")
    if not asset.template.rstrip().endswith(DYNAMIC_END):
        errors.append("Dynamic section must be the final prompt section.")
    if asset.estimated_tokens < MIN_CACHEABLE_TOKENS:
        errors.append("Prompt is below the prompt-cache token threshold.")
    return errors


def split_prompt_boundary(rendered_prompt: str) -> tuple[str, str]:
    if DYNAMIC_START not in rendered_prompt:
        raise ValidationError("Prompt dynamic marker is missing.")
    stable, dynamic = rendered_prompt.split(DYNAMIC_START, maxsplit=1)
    return stable, DYNAMIC_START + dynamic


def _replace_dynamic_section(
    template: str,
    replacements: Mapping[str, str],
) -> str:
    stable, dynamic_tail = split_prompt_boundary(template)
    for key, replacement in replacements.items():
        dynamic_tail = dynamic_tail.replace(
            "{{" + key + "}}",
            replacement,
        )
    unresolved = sorted(
        set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", dynamic_tail))
    )
    if unresolved:
        raise ValidationError(
            f"Dynamic prompt values are unresolved: {unresolved}"
        )
    return stable + dynamic_tail


def render_system_prompt(
    asset: PromptAsset,
    *,
    namespace: str,
    case_id: str,
    timestamp_utc: str = "2026-01-01T00:00:00Z",
    session_id: str = "SESSION-GIA-LAP-001",
    early_metadata: str | None = None,
) -> str:
    rendered = _replace_dynamic_section(
        asset.template,
        {
            "REQUEST_TIMESTAMP_UTC": timestamp_utc,
            "SESSION_ID": session_id,
            "USER_ID": "VIEWER-GIA-LAP-DEFAULT",
            "USER_ROLES": '["viewer"]',
            "WORKSPACE_ID": "WS-GIA-LAP-01",
            "IDEMPOTENCY_KEY": f"IDEM-{safe_slug(case_id).upper()}",
            "EVAL_BUDGET_REMAINING": "1000 synthetic units",
            "BENCHMARK_NAMESPACE": namespace,
            "BENCHMARK_CASE_ID": case_id,
            "PROMPT_SHA256": asset.sha256,
            "PROMPT_WORD_COUNT": str(asset.word_count),
            "EVIDENCE_BLOCK": (
                "[EVD-CACHE-001] Dữ liệu synthetic: marker hợp lệ là "
                "CACHE_BENCHMARK_OK."
            ),
            "RETRIEVED_CONTENT": "Không có nội dung truy xuất.",
        },
    )
    header = (
        f"[BENCHMARK_NAMESPACE={namespace}; POLICY_EFFECT=NONE]\n"
        "Siêu dữ liệu này chỉ cô lập phép đo prompt cache.\n"
    )
    if early_metadata:
        header += f"[VOLATILE_REQUEST_METADATA={early_metadata}]\n"
    return header + rendered


def response_json_schema(namespace: str) -> dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": f"enterprise_cache_response_{short_hash(namespace, 10)}",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["CACHE_BENCHMARK_OK"],
                    },
                    "namespace": {"type": "string"},
                },
                "required": ["status", "namespace"],
                "additionalProperties": False,
            },
        }
    }


def build_tools(
    namespace: str,
    *,
    shuffle_seed: int | None = None,
) -> tuple[dict[str, Any], ...]:
    suffix = short_hash(namespace, 8)
    definitions = (
        ("registry_read", "Đọc metadata agent tổng hợp."),
        ("policy_read", "Đọc policy version tổng hợp."),
        ("usage_read", "Đọc token usage tổng hợp."),
        ("approval_read", "Đọc trạng thái phê duyệt tổng hợp."),
    )
    tools = [
        {
            "type": "function",
            "name": f"enterprise_{name}_{suffix}",
            "description": (
                f"{description} Namespace {namespace}; không gọi trong benchmark."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["identifier", "fields"],
                "additionalProperties": False,
            },
        }
        for name, description in definitions
    ]
    if shuffle_seed is not None:
        offset = (shuffle_seed % (len(tools) - 1)) + 1
        tools = tools[offset:] + tools[:offset]
        if shuffle_seed % 2:
            tools = list(reversed(tools))
    return tuple(tools)


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


def _cache_key(namespace: str) -> str:
    return "enterprise-cache-" + short_hash(namespace, 32)


def _default_input(namespace: str) -> str:
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


def _new_spec(
    specs: list[RequestSpec],
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
    model: str = MODEL_DEFAULT,
) -> RequestSpec:
    order = len(specs) + 1
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
            model,
        ),
        delay_before_seconds=delay_before_seconds,
        batch_id=batch_id,
        optimized_cohort=optimized_cohort,
        pair_id=pair_id,
        metadata=dict(metadata or {}),
    )
    specs.append(spec)
    return spec


def build_suite(
    *,
    run_id: str,
    asset: PromptAsset,
    model: str = MODEL_DEFAULT,
    idle_gap_seconds: float = 660.0,
    paced_interval_seconds: float = 4.2,
) -> list[RequestSpec]:
    run_slug = safe_slug(run_id)
    specs: list[RequestSpec] = []

    short_namespace = f"{run_slug}-qualification-short"
    short_instructions = truncate_to_tokens(asset.template, 700, model)
    for index in range(3):
        _new_spec(
            specs,
            experiment="qualification",
            arm="under-threshold",
            namespace=short_namespace,
            instructions=short_instructions,
            input_text="Trả đúng chuỗi CACHE_QUALIFICATION_OK.",
            prompt_cache_key=_cache_key(short_namespace),
            max_output_tokens=64,
            expected_cache_state="under_threshold",
            metadata={"sample_index": index},
            model=model,
        )

    full_namespace = f"{run_slug}-qualification-full"
    full_instructions = render_system_prompt(
        asset,
        namespace=full_namespace,
        case_id="qualification-full",
    )
    full_schema = response_json_schema(full_namespace)
    for index in range(3):
        _new_spec(
            specs,
            experiment="qualification",
            arm="full-prompt",
            namespace=full_namespace,
            instructions=full_instructions,
            input_text=_default_input(full_namespace),
            prompt_cache_key=_cache_key(full_namespace),
            text_config=full_schema,
            expected_cache_state="cold" if index == 0 else "warm",
            optimized_cohort=index > 0,
            metadata={"sample_index": index},
            model=model,
        )

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
            _new_spec(
                specs,
                experiment="prefix_stability",
                arm=arm,
                namespace=namespace,
                instructions=instructions,
                input_text=_default_input(namespace),
                prompt_cache_key=_cache_key(namespace),
                tools=tools,
                text_config=schema,
                expected_cache_state=(
                    "cold"
                    if index == 0
                    else ("warm" if not volatile else "observe")
                ),
                optimized_cohort=not volatile and index > 0,
                metadata={"sample_index": index},
                model=model,
            )

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
            _new_spec(
                specs,
                experiment="tool_schema_stability",
                arm=arm,
                namespace=namespace,
                instructions=instructions,
                input_text=_default_input(namespace),
                prompt_cache_key=_cache_key(namespace),
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
                model=model,
            )

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
            key_namespace = (
                f"{namespace}-{index}" if unique_key else namespace
            )
            _new_spec(
                specs,
                experiment="cache_key_cardinality",
                arm=arm,
                namespace=namespace,
                instructions=instructions,
                input_text=_default_input(namespace),
                prompt_cache_key=_cache_key(key_namespace),
                tools=tools,
                text_config=schema,
                expected_cache_state=(
                    "cold"
                    if index == 0
                    else ("warm" if not unique_key else "observe")
                ),
                optimized_cohort=not unique_key and index > 0,
                metadata={"sample_index": index},
                model=model,
            )

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
            _new_spec(
                specs,
                experiment="traffic_shape",
                arm=arm,
                namespace=namespace,
                instructions=instructions,
                input_text=_default_input(namespace),
                prompt_cache_key=_cache_key(namespace),
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
                model=model,
            )

    idle_namespace = f"{run_slug}-idle-retention"
    idle_instructions = render_system_prompt(
        asset,
        namespace=idle_namespace,
        case_id="idle-retention",
    )
    idle_schema = response_json_schema(idle_namespace)
    for index, stage in enumerate(
        ("seed", "immediate_warm", "post_idle", "rewarm")
    ):
        _new_spec(
            specs,
            experiment="idle_retention",
            arm="observational",
            namespace=idle_namespace,
            instructions=idle_instructions,
            input_text=_default_input(idle_namespace),
            prompt_cache_key=_cache_key(idle_namespace),
            text_config=idle_schema,
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
            model=model,
        )

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
            "prompt_cache_key": _cache_key(namespace),
            "tools": build_tools(namespace),
            "text_config": response_json_schema(namespace),
            "max_output_tokens": MATCHED_MAX_OUTPUT_TOKENS,
            "pair_id": pair_id,
            "model": model,
        }
        _new_spec(
            specs,
            expected_cache_state="cold",
            metadata={"pair_index": pair_index, "member": "cold"},
            **common,
        )
        _new_spec(
            specs,
            expected_cache_state="warm",
            optimized_cohort=True,
            metadata={"pair_index": pair_index, "member": "warm"},
            **common,
        )

    validate_suite(specs)
    return specs


def build_isolation_probe(
    *,
    run_id: str,
    asset: PromptAsset,
    model: str = MODEL_DEFAULT,
) -> list[RequestSpec]:
    run_slug = safe_slug(run_id)
    specs: list[RequestSpec] = []
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
        _new_spec(
            specs,
            experiment="capability_probe",
            arm=member,
            namespace=namespace,
            instructions=payloads[namespace],
            input_text=(
                "Phép đo capability synthetic. Chỉ trả đúng chuỗi "
                "CACHE_CAPABILITY_OK."
            ),
            prompt_cache_key=_cache_key(namespace),
            max_output_tokens=MATCHED_MAX_OUTPUT_TOKENS,
            expected_cache_state=expected,
            metadata={"probe_member": member},
            model=model,
        )
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


def validate_suite(specs: Sequence[RequestSpec]) -> None:
    if len(specs) != PLANNED_ATTEMPTS:
        raise ValidationError(
            f"Planned suite must contain {PLANNED_ATTEMPTS} requests; "
            f"got {len(specs)}."
        )
    allocation = Counter(spec.experiment for spec in specs)
    if dict(allocation) != EXPECTED_ALLOCATION:
        raise ValidationError(
            f"Unexpected scenario allocation: {dict(allocation)}"
        )
    if [spec.order for spec in specs] != list(
        range(1, PLANNED_ATTEMPTS + 1)
    ):
        raise ValidationError("Request order is not contiguous.")
    optimized = [spec for spec in specs if spec.optimized_cohort]
    if len(optimized) != OPTIMIZED_COHORT_EXPECTED_REQUESTS:
        raise ValidationError(
            "Optimized cohort must contain "
            f"{OPTIMIZED_COHORT_EXPECTED_REQUESTS} warm requests; "
            f"got {len(optimized)}."
        )
    if any(spec.expected_cache_state != "warm" for spec in optimized):
        raise ValidationError("Optimized cohort may contain only warm requests.")

    pairs: dict[str, list[RequestSpec]] = defaultdict(list)
    for spec in specs:
        if spec.pair_id:
            pairs[spec.pair_id].append(spec)
    if len(pairs) != MATCHED_PAIR_COUNT:
        raise ValidationError(
            f"Expected {MATCHED_PAIR_COUNT} matched latency pairs."
        )
    namespaces: set[str] = set()
    keys: set[str] = set()
    previous_order = 0
    for pair_id in sorted(pairs):
        members = sorted(pairs[pair_id], key=lambda spec: spec.order)
        if len(members) != 2:
            raise ValidationError(f"{pair_id} must contain two members.")
        cold, warm = members
        if (
            cold.expected_cache_state,
            warm.expected_cache_state,
        ) != ("cold", "warm"):
            raise ValidationError(f"{pair_id} must be cold then warm.")
        if warm.order != cold.order + 1 or cold.order <= previous_order:
            raise ValidationError(f"{pair_id} is not an adjacent ordered pair.")
        if (
            cold.request_payload_fingerprint()
            != warm.request_payload_fingerprint()
        ):
            raise ValidationError(
                f"{pair_id} payload members are not byte-identical."
            )
        if cold.batch_id or warm.batch_id:
            raise ValidationError(f"{pair_id} cannot execute concurrently.")
        namespaces.add(cold.namespace)
        keys.add(cold.prompt_cache_key)
        previous_order = warm.order
    if len(namespaces) != MATCHED_PAIR_COUNT:
        raise ValidationError("Matched pair namespaces are not unique.")
    if len(keys) != MATCHED_PAIR_COUNT:
        raise ValidationError("Matched pair cache keys are not unique.")


class AttemptBudget:
    def __init__(self, maximum: int = MAX_ATTEMPTS_DEFAULT) -> None:
        if maximum <= 0:
            raise ValueError("Attempt budget must be positive.")
        self.maximum = maximum
        self._used = 0
        self._claims: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.maximum - self._used

    def claim(
        self,
        *,
        logical_request_id: str,
        reason: str,
    ) -> int:
        with self._lock:
            if self._used >= self.maximum:
                raise BudgetExhausted(
                    f"HTTP attempt cap {self.maximum} is exhausted."
                )
            self._used += 1
            number = self._used
            self._claims.append(
                {
                    "attempt_number": number,
                    "logical_request_id": logical_request_id,
                    "reason": reason,
                    "claimed_at": utc_now_iso(),
                }
            )
            return number

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "maximum": self.maximum,
                "used": self._used,
                "remaining": self.maximum - self._used,
                "claims": list(self._claims),
            }


def usage_from_response_payload(
    response_payload: Mapping[str, Any],
) -> dict[str, int]:
    usage = response_payload.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)
    total_tokens = int(
        usage.get("total_tokens") or input_tokens + output_tokens
    )
    if min(
        input_tokens,
        cached_tokens,
        output_tokens,
        reasoning_tokens,
        total_tokens,
    ) < 0:
        raise ValidationError("Token usage cannot be negative.")
    if cached_tokens > input_tokens:
        raise ValidationError("cached_tokens exceeds input_tokens.")
    if reasoning_tokens > output_tokens:
        raise ValidationError("reasoning_tokens exceeds output_tokens.")
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "uncached_input_tokens": input_tokens - cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def cacheable_prefix_efficiency(
    cached_tokens: int,
    cacheable_prefix_tokens_estimate: int,
) -> float | None:
    if cacheable_prefix_tokens_estimate <= 0:
        return None
    return cached_tokens / cacheable_prefix_tokens_estimate


def cache_state_is_valid(
    expected_state: str,
    *,
    cached_tokens: int,
    cacheable_prefix_tokens_estimate: int,
) -> bool:
    if expected_state in {"cold", "under_threshold"}:
        return cached_tokens == 0
    if expected_state == "warm":
        efficiency = cacheable_prefix_efficiency(
            cached_tokens,
            cacheable_prefix_tokens_estimate,
        )
        return (
            cached_tokens >= MIN_CACHEABLE_TOKENS
            and efficiency is not None
            and efficiency >= WARM_PREFIX_EFFICIENCY_FLOOR
        )
    return True


@dataclass(frozen=True)
class PriceBook:
    currency: str
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float
    source: str
    retrieved_at: str
    meters: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _latest_consistent_meter(
    items: Sequence[Mapping[str, Any]],
    sku_name: str,
) -> dict[str, Any]:
    candidates = [
        dict(item)
        for item in items
        if item.get("productName") == "Azure OpenAI GPT5"
        and item.get("skuName") == sku_name
        and item.get("unitOfMeasure") == "1M"
        and item.get("type") == "Consumption"
    ]
    if not candidates:
        raise PricingError(f"Missing Global Standard meter {sku_name!r}.")
    latest_date = max(
        str(item.get("effectiveStartDate") or "") for item in candidates
    )
    latest = [
        item
        for item in candidates
        if str(item.get("effectiveStartDate") or "") == latest_date
    ]
    prices = {float(item["retailPrice"]) for item in latest}
    currencies = {str(item.get("currencyCode")) for item in latest}
    if len(prices) != 1 or len(currencies) != 1:
        raise PricingError(
            f"Ambiguous price rows for {sku_name}: "
            f"prices={prices}, currencies={currencies}"
        )
    row = latest[0]
    return {
        "sku_name": sku_name,
        "meter_name": row.get("meterName"),
        "meter_id": row.get("meterId"),
        "unit_of_measure": row.get("unitOfMeasure"),
        "retail_price": next(iter(prices)),
        "currency": next(iter(currencies)),
        "effective_start_date": latest_date,
        "region_row_count": len(latest),
    }


def select_global_standard_price_book(
    items: Sequence[Mapping[str, Any]],
    *,
    retrieved_at: str | None = None,
) -> PriceBook:
    meters = {
        kind: _latest_consistent_meter(items, sku)
        for kind, sku in PRICE_SKUS.items()
    }
    currencies = {meter["currency"] for meter in meters.values()}
    if len(currencies) != 1:
        raise PricingError(
            f"Pricing meters use inconsistent currencies: {currencies}"
        )
    return PriceBook(
        currency=currencies.pop(),
        input_usd_per_million=float(meters["input"]["retail_price"]),
        cached_input_usd_per_million=float(
            meters["cached_input"]["retail_price"]
        ),
        output_usd_per_million=float(meters["output"]["retail_price"]),
        source=RETAIL_PRICES_URL,
        retrieved_at=retrieved_at or utc_now_iso(),
        meters=meters,
    )


def override_price_book(
    *,
    input_price: float,
    cached_input_price: float,
    output_price: float,
    currency: str = "USD",
) -> PriceBook:
    for label, value in (
        ("input", input_price),
        ("cached input", cached_input_price),
        ("output", output_price),
    ):
        if value < 0:
            raise PricingError(f"{label} price cannot be negative.")
    return PriceBook(
        currency=currency,
        input_usd_per_million=input_price,
        cached_input_usd_per_million=cached_input_price,
        output_usd_per_million=output_price,
        source="cli_override",
        retrieved_at=utc_now_iso(),
        meters={},
    )


def compute_cost(
    usage: Mapping[str, int],
    price_book: PriceBook,
) -> dict[str, float]:
    uncached_input = int(usage["uncached_input_tokens"])
    cached_input = int(usage["cached_tokens"])
    output_tokens = int(usage["output_tokens"])
    uncached_input_usd = (
        uncached_input
        * price_book.input_usd_per_million
        / 1_000_000
    )
    cached_input_usd = (
        cached_input
        * price_book.cached_input_usd_per_million
        / 1_000_000
    )
    output_usd = (
        output_tokens
        * price_book.output_usd_per_million
        / 1_000_000
    )
    no_cache_input_usd = (
        int(usage["input_tokens"])
        * price_book.input_usd_per_million
        / 1_000_000
    )
    actual_input_usd = uncached_input_usd + cached_input_usd
    actual_usd = actual_input_usd + output_usd
    no_cache_usd = no_cache_input_usd + output_usd
    input_savings_usd = no_cache_input_usd - actual_input_usd
    savings_usd = no_cache_usd - actual_usd
    return {
        "uncached_input_usd": uncached_input_usd,
        "cached_input_usd": cached_input_usd,
        "actual_input_usd": actual_input_usd,
        "no_cache_input_usd": no_cache_input_usd,
        "input_savings_usd": input_savings_usd,
        "input_savings_rate": (
            input_savings_usd / no_cache_input_usd
            if no_cache_input_usd > 0
            else 0.0
        ),
        "output_usd": output_usd,
        "actual_usd": actual_usd,
        "no_cache_usd": no_cache_usd,
        "savings_usd": savings_usd,
        "total_savings_rate": (
            savings_usd / no_cache_usd if no_cache_usd > 0 else 0.0
        ),
    }


def percentile(
    values: Sequence[float],
    percentile_value: float,
) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _latest_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = dict(raw)
        logical_id = str(record.get("logical_request_id") or "")
        if not logical_id:
            continue
        previous = latest.get(logical_id)
        current_attempt = int(record.get("attempt_number") or 0)
        previous_attempt = int((previous or {}).get("attempt_number") or 0)
        if previous is None or current_attempt >= previous_attempt:
            latest[logical_id] = record
    return sorted(
        latest.values(),
        key=lambda record: (
            int(record.get("order") or 0),
            str(record.get("logical_request_id") or ""),
        ),
    )


def _metric_summary(
    records: Sequence[Mapping[str, Any]],
    price_book: PriceBook | None,
) -> dict[str, Any]:
    completed = [
        record for record in records if record.get("status") == "completed"
    ]
    failed = [
        record for record in records if record.get("status") == "failed"
    ]
    usage = {
        key: sum(
            int((record.get("usage") or {}).get(key) or 0)
            for record in completed
        )
        for key in (
            "input_tokens",
            "cached_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
    }
    efficiencies = [
        float(record["cacheable_prefix_efficiency"])
        for record in completed
        if record.get("cacheable_prefix_efficiency") is not None
        and int((record.get("usage") or {}).get("cached_tokens") or 0) > 0
    ]
    substantive_hits = sum(
        1
        for record in completed
        if cache_state_is_valid(
            "warm",
            cached_tokens=int(
                (record.get("usage") or {}).get("cached_tokens") or 0
            ),
            cacheable_prefix_tokens_estimate=int(
                record.get("cacheable_prefix_tokens_estimate") or 0
            ),
        )
    )
    timing = {}
    for name in ("first_event_ms", "first_text_ms", "ttlt_ms", "tbt_ms"):
        values = [
            float((record.get("timing") or {})[name])
            for record in completed
            if (record.get("timing") or {}).get(name) is not None
        ]
        timing[name] = {
            "count": len(values),
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
        }
    return {
        "logical_requests": len(records),
        "completed": len(completed),
        "failed": len(failed),
        "request_hit_rate": (
            sum(
                int((record.get("usage") or {}).get("cached_tokens") or 0)
                > 0
                for record in completed
            )
            / len(completed)
            if completed
            else None
        ),
        "substantive_request_hit_rate": (
            substantive_hits / len(completed) if completed else None
        ),
        "substantive_hits": substantive_hits,
        "token_weighted_cache_rate": (
            usage["cached_tokens"] / usage["input_tokens"]
            if usage["input_tokens"] > 0
            else None
        ),
        "prefix_efficiency_p50": percentile(efficiencies, 0.50),
        "usage": usage,
        "timing": timing,
        "cost": compute_cost(usage, price_book) if price_book else None,
    }


def _root_cause_summary(
    final_records: Sequence[Mapping[str, Any]],
    price_book: PriceBook | None,
) -> list[dict[str, Any]]:
    definitions = (
        (
            "prefix_volatility",
            ("prefix_stability", "stable-prefix"),
            ("prefix_stability", "volatile-prefix"),
        ),
        (
            "tool_schema_churn",
            ("tool_schema_stability", "canonical"),
            ("tool_schema_stability", "shuffled"),
        ),
        (
            "cache_key_cardinality",
            ("cache_key_cardinality", "stable-key"),
            ("cache_key_cardinality", "per-request-key"),
        ),
        (
            "traffic_shape",
            ("traffic_shape", "paced"),
            ("traffic_shape", "burst"),
        ),
    )
    rows: list[dict[str, Any]] = []
    for factor, baseline_key, degraded_key in definitions:
        baseline_records = [
            record
            for record in final_records
            if (record.get("experiment"), record.get("arm")) == baseline_key
            and int((record.get("metadata") or {}).get("sample_index") or 0)
            > 0
        ]
        degraded_records = [
            record
            for record in final_records
            if (record.get("experiment"), record.get("arm")) == degraded_key
            and int((record.get("metadata") or {}).get("sample_index") or 0)
            > 0
        ]
        baseline = _metric_summary(baseline_records, price_book)
        degraded = _metric_summary(degraded_records, price_book)
        baseline_rate = baseline["token_weighted_cache_rate"]
        degraded_rate = degraded["token_weighted_cache_rate"]
        delta = (
            baseline_rate - degraded_rate
            if baseline_rate is not None and degraded_rate is not None
            else None
        )
        enough = (
            baseline["completed"] >= ROOT_CAUSE_MIN_COMPLETED
            and degraded["completed"] >= ROOT_CAUSE_MIN_COMPLETED
        )
        rows.append(
            {
                "factor": factor,
                "baseline_arm": "/".join(baseline_key),
                "degraded_arm": "/".join(degraded_key),
                "baseline_completed": baseline["completed"],
                "degraded_completed": degraded["completed"],
                "baseline_token_weighted_cache_rate": baseline_rate,
                "degraded_token_weighted_cache_rate": degraded_rate,
                "delta": delta,
                "status": (
                    "supported"
                    if enough
                    and delta is not None
                    and delta >= ROOT_CAUSE_DELTA_FLOOR
                    else ("not_observed" if enough else "insufficient_data")
                ),
            }
        )
    return rows


def _matched_latency_summary(
    final_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for record in final_records:
        pair_id = record.get("pair_id")
        if pair_id:
            member = str((record.get("metadata") or {}).get("member") or "")
            grouped[str(pair_id)][member] = record
    valid_deltas: list[float] = []
    first_text_deltas: list[float] = []
    warm_faster = 0
    cold_faster = 0
    equal = 0
    invalid_reasons: Counter[str] = Counter()
    for pair_id in sorted(grouped):
        cold = grouped[pair_id].get("cold")
        warm = grouped[pair_id].get("warm")
        if not cold or not warm:
            invalid_reasons["missing_member"] += 1
            continue
        if cold.get("status") != "completed" or warm.get("status") != "completed":
            invalid_reasons["incomplete_member"] += 1
            continue
        if int(cold.get("retry_index") or 0) != 0 or int(
            warm.get("retry_index") or 0
        ) != 0:
            invalid_reasons["retried_member"] += 1
            continue
        cold_usage = cold.get("usage") or {}
        warm_usage = warm.get("usage") or {}
        if not cache_state_is_valid(
            "cold",
            cached_tokens=int(cold_usage.get("cached_tokens") or 0),
            cacheable_prefix_tokens_estimate=int(
                cold.get("cacheable_prefix_tokens_estimate") or 0
            ),
        ):
            invalid_reasons["cold_not_miss"] += 1
            continue
        if not cache_state_is_valid(
            "warm",
            cached_tokens=int(warm_usage.get("cached_tokens") or 0),
            cacheable_prefix_tokens_estimate=int(
                warm.get("cacheable_prefix_tokens_estimate") or 0
            ),
        ):
            invalid_reasons["warm_not_substantive_hit"] += 1
            continue
        cold_ttlt = (cold.get("timing") or {}).get("ttlt_ms")
        warm_ttlt = (warm.get("timing") or {}).get("ttlt_ms")
        if cold_ttlt is None or warm_ttlt is None:
            invalid_reasons["missing_ttlt"] += 1
            continue
        delta = float(warm_ttlt) - float(cold_ttlt)
        valid_deltas.append(delta)
        if delta < 0:
            warm_faster += 1
        elif delta > 0:
            cold_faster += 1
        else:
            equal += 1
        cold_first = (cold.get("timing") or {}).get("first_text_ms")
        warm_first = (warm.get("timing") or {}).get("first_text_ms")
        if cold_first is not None and warm_first is not None:
            first_text_deltas.append(float(warm_first) - float(cold_first))
    return {
        "planned_pairs": MATCHED_PAIR_COUNT,
        "observed_pairs": len(grouped),
        "valid_pairs": len(valid_deltas),
        "invalid_pairs": MATCHED_PAIR_COUNT - len(valid_deltas),
        "invalid_reasons": dict(invalid_reasons),
        "warm_faster_ttlt_pairs": warm_faster,
        "cold_faster_ttlt_pairs": cold_faster,
        "equal_ttlt_pairs": equal,
        "warm_minus_cold_ttlt_ms_p50": percentile(valid_deltas, 0.50),
        "warm_minus_cold_first_text_ms_p50": percentile(
            first_text_deltas,
            0.50,
        ),
        "causal_latency_claimed": False,
        "interpretation": (
            "directional_only"
            if valid_deltas
            else "insufficient_valid_pairs"
        ),
    }


def build_summary(
    *,
    run_id: str,
    model: str,
    records: Sequence[Mapping[str, Any]],
    price_book: PriceBook | None,
    attempt_budget: Mapping[str, Any],
    run_status: str,
    completed_at: str,
    prompt_metadata: Mapping[str, Any] | None = None,
    fatal_error: str | None = None,
) -> dict[str, Any]:
    final_records = _latest_records(records)
    planned_final = [
        record
        for record in final_records
        if record.get("experiment") != "capability_probe"
    ]
    optimized_records = [
        record
        for record in planned_final
        if bool(record.get("optimized_cohort"))
    ]
    degraded_records = [
        record
        for record in planned_final
        if (str(record.get("experiment")), str(record.get("arm")))
        in DEGRADED_ARMS
    ]
    arms: dict[str, Any] = {}
    for key in sorted(
        {
            f"{record.get('experiment')}/{record.get('arm')}"
            for record in planned_final
        }
    ):
        experiment, arm = key.split("/", maxsplit=1)
        arms[key] = _metric_summary(
            [
                record
                for record in planned_final
                if record.get("experiment") == experiment
                and record.get("arm") == arm
            ],
            price_book,
        )
    overall = _metric_summary(final_records, price_book)
    optimized = _metric_summary(optimized_records, price_book)
    degraded = _metric_summary(degraded_records, price_book)
    reliability_pass = (
        optimized["logical_requests"]
        == OPTIMIZED_COHORT_EXPECTED_REQUESTS
        and optimized["completed"] == OPTIMIZED_COHORT_EXPECTED_REQUESTS
        and optimized["failed"] == 0
    )
    hit_rate = optimized["substantive_request_hit_rate"]
    token_rate = optimized["token_weighted_cache_rate"]
    prefix_p50 = optimized["prefix_efficiency_p50"]
    cache_pass = bool(
        hit_rate is not None
        and hit_rate >= OPTIMIZED_REQUEST_HIT_FLOOR
        and (
            (
                token_rate is not None
                and token_rate >= OPTIMIZED_TOKEN_CACHE_FLOOR
            )
            or (
                prefix_p50 is not None
                and prefix_p50 >= OPTIMIZED_PREFIX_P50_FLOOR
            )
        )
    )
    acceptance = {
        "status": (
            "pass"
            if reliability_pass and cache_pass
            else ("fail" if run_status != "incomplete" else "incomplete")
        ),
        "optimized_reliability_pass": reliability_pass,
        "optimized_cache_pass": cache_pass,
        "optimized_expected_requests": (
            OPTIMIZED_COHORT_EXPECTED_REQUESTS
        ),
        "optimized_completed_requests": optimized["completed"],
        "optimized_failed_requests": optimized["failed"],
        "substantive_request_hit_rate": hit_rate,
        "token_weighted_cache_rate": token_rate,
        "prefix_efficiency_p50": prefix_p50,
        "thresholds": {
            "substantive_request_hit_rate": OPTIMIZED_REQUEST_HIT_FLOOR,
            "token_weighted_cache_rate": OPTIMIZED_TOKEN_CACHE_FLOOR,
            "prefix_efficiency_p50": OPTIMIZED_PREFIX_P50_FLOOR,
            "minimum_cached_tokens": MIN_CACHEABLE_TOKENS,
            "warm_prefix_efficiency": WARM_PREFIX_EFFICIENCY_FLOOR,
            "maximum_optimized_failures": 0,
        },
    }
    allocation = Counter(
        str(record.get("experiment"))
        for record in planned_final
    )
    return {
        "schema_version": "2.0.0",
        "run_id": run_id,
        "model": model,
        "generated_at": completed_at,
        "run_status": run_status,
        "fatal_error": fatal_error,
        "suite": {
            "planned_requests": PLANNED_ATTEMPTS,
            "probe_requests": PROBE_ATTEMPTS,
            "hard_cap": int(attempt_budget.get("maximum") or 0),
            "optimized_expected_requests": (
                OPTIMIZED_COHORT_EXPECTED_REQUESTS
            ),
            "matched_pair_count": MATCHED_PAIR_COUNT,
            "allocation": dict(allocation),
        },
        "records": {
            "attempt_records": len(records),
            "final_logical_requests": len(final_records),
            "planned_final_logical_requests": len(planned_final),
            "probe_final_logical_requests": (
                len(final_records) - len(planned_final)
            ),
            "attempt_budget": dict(attempt_budget),
        },
        "overall": overall,
        "optimized": optimized,
        "degraded": degraded,
        "arms": arms,
        "root_causes": _root_cause_summary(planned_final, price_book),
        "matched_latency": _matched_latency_summary(planned_final),
        "acceptance": acceptance,
        "pricing": price_book.to_dict() if price_book else None,
        "prompt": dict(prompt_metadata or {}),
        "provenance": {
            "source_kind": "live_run",
            "source_run_id": run_id,
            "derived_from": ["manifest.json", "requests.jsonl"],
            "generated_at": completed_at,
        },
    }


def _safe_metric(metric: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "logical_requests",
        "completed",
        "failed",
        "request_hit_rate",
        "substantive_request_hit_rate",
        "substantive_hits",
        "token_weighted_cache_rate",
        "prefix_efficiency_p50",
        "usage",
        "timing",
        "cost",
    )
    return {
        key: metric.get(key)
        for key in allowed
        if key in metric
    }


def sanitize_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    records = summary.get("records") or {}
    safe = {
        "schema_version": summary.get("schema_version"),
        "sample": {
            "sanitized": True,
            "description": (
                "Allowlisted aggregate view of a real live benchmark run."
            ),
        },
        "run_id": summary.get("run_id"),
        "model": summary.get("model"),
        "generated_at": summary.get("generated_at"),
        "run_status": summary.get("run_status"),
        "suite": dict(summary.get("suite") or {}),
        "records": {
            key: records.get(key)
            for key in (
                "attempt_records",
                "final_logical_requests",
                "planned_final_logical_requests",
                "probe_final_logical_requests",
            )
        },
        "overall": _safe_metric(summary.get("overall") or {}),
        "optimized": _safe_metric(summary.get("optimized") or {}),
        "degraded": _safe_metric(summary.get("degraded") or {}),
        "arms": {
            str(name): _safe_metric(value)
            for name, value in (summary.get("arms") or {}).items()
            if isinstance(value, Mapping)
        },
        "root_causes": [
            {
                key: row.get(key)
                for key in (
                    "factor",
                    "baseline_arm",
                    "degraded_arm",
                    "baseline_completed",
                    "degraded_completed",
                    "baseline_token_weighted_cache_rate",
                    "degraded_token_weighted_cache_rate",
                    "delta",
                    "status",
                )
            }
            for row in (summary.get("root_causes") or [])
            if isinstance(row, Mapping)
        ],
        "matched_latency": {
            key: (summary.get("matched_latency") or {}).get(key)
            for key in (
                "planned_pairs",
                "observed_pairs",
                "valid_pairs",
                "invalid_pairs",
                "invalid_reasons",
                "warm_faster_ttlt_pairs",
                "cold_faster_ttlt_pairs",
                "equal_ttlt_pairs",
                "warm_minus_cold_ttlt_ms_p50",
                "warm_minus_cold_first_text_ms_p50",
                "causal_latency_claimed",
                "interpretation",
            )
        },
        "acceptance": dict(summary.get("acceptance") or {}),
        "pricing": (
            dict(summary["pricing"])
            if isinstance(summary.get("pricing"), Mapping)
            else None
        ),
        "prompt": {
            key: (summary.get("prompt") or {}).get(key)
            for key in (
                "path",
                "sha256",
                "word_count",
                "estimated_tokens",
                "encoding",
                "dynamic_start_marker",
            )
        },
        "provenance": {
            "source_kind": "sanitized_live_run",
            "source_run_id": summary.get("run_id"),
            "derived_from": ["live summary aggregate"],
            "generated_at": summary.get("generated_at"),
            "sanitized": True,
        },
    }
    if set(safe) != SAMPLE_SUMMARY_ALLOWLIST:
        raise ValidationError("Sanitized summary allowlist drifted.")
    return safe


def _format_percent(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1%}"


def _format_ms(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1f} ms"


def _format_usd(value: Any) -> str:
    return "n/a" if value is None else f"${float(value):.6f}"


def render_markdown_report(
    summary: Mapping[str, Any],
    *,
    sanitized_sample: bool = False,
) -> str:
    suite = summary.get("suite") or {}
    overall = summary.get("overall") or {}
    optimized = summary.get("optimized") or {}
    degraded = summary.get("degraded") or {}
    acceptance = summary.get("acceptance") or {}
    thresholds = acceptance.get("thresholds") or {}
    matched = summary.get("matched_latency") or {}
    prompt = summary.get("prompt") or {}
    pricing = summary.get("pricing")
    overall_cost = overall.get("cost") or {}
    optimized_cost = optimized.get("cost") or {}
    allocation = suite.get("allocation") or EXPECTED_ALLOCATION
    sample_notice = (
        "> **Mẫu đã khử nhạy cảm:** báo cáo này được render từ allowlist "
        "aggregate của một live run thật; không chứa endpoint, prompt/input/output "
        "thô, response/request ID hoặc attempt ledger.\n\n"
        if sanitized_sample
        else ""
    )
    price_input = (
        pricing.get("input_usd_per_million")
        if isinstance(pricing, Mapping)
        else None
    )
    price_cached = (
        pricing.get("cached_input_usd_per_million")
        if isinstance(pricing, Mapping)
        else None
    )
    price_output = (
        pricing.get("output_usd_per_million")
        if isinstance(pricing, Mapping)
        else None
    )
    allocation_rows = "\n".join(
        f"| `{name}` | {count} |"
        for name, count in allocation.items()
    )
    root_rows = "\n".join(
        "| `{factor}` | {baseline} | {degraded} | {delta} | `{status}` |".format(
            factor=row.get("factor"),
            baseline=_format_percent(
                row.get("baseline_token_weighted_cache_rate")
            ),
            degraded=_format_percent(
                row.get("degraded_token_weighted_cache_rate")
            ),
            delta=_format_percent(row.get("delta")),
            status=row.get("status"),
        )
        for row in summary.get("root_causes") or []
    )
    if not root_rows:
        root_rows = "| n/a | n/a | n/a | n/a | `insufficient_data` |"
    marker = str(prompt.get("dynamic_start_marker") or DYNAMIC_START)
    sources = "\n".join(f"- {url}" for url in OFFICIAL_SOURCES)
    return f"""# Báo cáo Azure OpenAI Prompt Cache Benchmark

{sample_notice}Run `{summary.get("run_id")}` dùng model `{summary.get("model")}`. Trạng thái run: `{summary.get("run_status")}`. Acceptance cache-only: **{acceptance.get("status", "incomplete")}**.

## Kết quả chính

| Phạm vi | Logical requests | Completed | Substantive hit rate | Token-weighted cache rate | Input savings | Total savings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Overall | {overall.get("logical_requests", 0)} | {overall.get("completed", 0)} | {_format_percent(overall.get("substantive_request_hit_rate"))} | {_format_percent(overall.get("token_weighted_cache_rate"))} | {_format_percent(overall_cost.get("input_savings_rate"))} | {_format_percent(overall_cost.get("total_savings_rate"))} |
| Optimized | {optimized.get("logical_requests", 0)} | {optimized.get("completed", 0)} | {_format_percent(optimized.get("substantive_request_hit_rate"))} | {_format_percent(optimized.get("token_weighted_cache_rate"))} | {_format_percent(optimized_cost.get("input_savings_rate"))} | {_format_percent(optimized_cost.get("total_savings_rate"))} |
| Degraded diagnostic | {degraded.get("logical_requests", 0)} | {degraded.get("completed", 0)} | {_format_percent(degraded.get("substantive_request_hit_rate"))} | {_format_percent(degraded.get("token_weighted_cache_rate"))} | n/a | n/a |

Overall gồm cold seeds và {suite.get("probe_requests", PROBE_ATTEMPTS)} capability/isolation calls. Optimized chỉ gồm đúng {suite.get("optimized_expected_requests", OPTIMIZED_COHORT_EXPECTED_REQUESTS)} warm requests production-like. Degraded là các arm cố ý gây prefix/tool/key/traffic instability; không dùng chúng làm acceptance denominator.

## Cách benchmark cache efficiency

### 1. Preflight và dry-run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python benchmark.py dry-run
```

Dry-run validate prompt boundary, exact allocation {suite.get("planned_requests", PLANNED_ATTEMPTS)}, optimized cohort {suite.get("optimized_expected_requests", OPTIMIZED_COHORT_EXPECTED_REQUESTS)}, {suite.get("matched_pair_count", MATCHED_PAIR_COUNT)} pair identities, hard cap {suite.get("hard_cap", MAX_ATTEMPTS_DEFAULT)} và cost ceiling. Dry-run không gọi model; mặc định chỉ gọi Azure Retail Prices API để lấy ba meter.

### 2. Cold/warm và A/B design

| Experiment | Planned requests |
| --- | ---: |
{allocation_rows}

Mỗi optimized arm có một cold seed bị loại khỏi denominator, sau đó là warm steady requests. A/B giữ workload tương đương và chỉ đổi một factor: prefix volatility, tool/schema order, cache-key cardinality hoặc traffic shape.

### 3. Pair isolation cho matched latency

Có {matched.get("planned_pairs", MATCHED_PAIR_COUNT)} cặp cold/warm. Mỗi cặp dùng `pair_id`, namespace và `prompt_cache_key` riêng; cặp 2–{matched.get("planned_pairs", MATCHED_PAIR_COUNT)} không thể reuse prefix của cặp trước. Trong một cặp, `instructions`, `input`, `tools`, `text`, cache key và `max_output_tokens={MATCHED_MAX_OUTPUT_TOKENS}` byte-identical. Chỉ nhận cặp khi cold có `cached_tokens=0` và warm là substantive hit.

Valid pairs: {matched.get("valid_pairs", 0)}/{matched.get("planned_pairs", MATCHED_PAIR_COUNT)}. Warm-faster TTLT: {matched.get("warm_faster_ttlt_pairs", 0)}; cold-faster TTLT: {matched.get("cold_faster_ttlt_pairs", 0)}; median warm-minus-cold TTLT: {_format_ms(matched.get("warm_minus_cold_ttlt_ms_p50"))}. `causal_latency_claimed={str(bool(matched.get("causal_latency_claimed"))).lower()}`: latency chỉ directional vì load, routing và token-generation variance vẫn là confounders.

### 4. Lệnh chạy và rebuild

```bash
.venv/bin/python benchmark.py live --confirm-live
.venv/bin/python benchmark.py report runs/<run-id>
```

Live chạy {suite.get("probe_requests", PROBE_ATTEMPTS)} probe calls trước {suite.get("planned_requests", PLANNED_ATTEMPTS)} planned requests. Mọi HTTP attempt, kể cả bounded retry, claim cùng atomic hard cap {suite.get("hard_cap", MAX_ATTEMPTS_DEFAULT)} trước khi gọi. SDK internal retry bị tắt. `report` chỉ đọc `manifest.json` và `requests.jsonl`; không gọi model hay price API.

### 5. Công thức và denominator

```text
request_hit_rate = completed(cached_tokens > 0) / completed
substantive_request_hit_rate =
  completed(cached_tokens >= {MIN_CACHEABLE_TOKENS}
    and cached_tokens / estimated_cacheable_prefix >= {WARM_PREFIX_EFFICIENCY_FLOOR:.0%})
  / completed
token_weighted_cache_rate = sum(cached_tokens) / sum(input_tokens)
actual_cost = uncached_input × input_rate + cached_input × cached_rate
              + output_tokens × output_rate
input_savings_rate = input_savings / no_cache_input_cost
total_savings_rate = total_savings / no_cache_total_cost
```

`input_tokens` đã bao gồm cached tokens; reasoning tokens đã nằm trong output tokens và không tính lần hai. Input-savings và total-savings có denominator khác nhau nên phải báo riêng.

### 6. Pricing và cost guard

| Meter | USD / 1M tokens |
| --- | ---: |
| Uncached input | {_format_usd(price_input)} |
| Cached input | {_format_usd(price_cached)} |
| Output | {_format_usd(price_output)} |

Nguồn pricing: `{pricing.get("source") if isinstance(pricing, Mapping) else "n/a"}`; retrieved at `{pricing.get("retrieved_at") if isinstance(pricing, Mapping) else "n/a"}`. Actual aggregate cost: {_format_usd(overall_cost.get("actual_usd"))}; no-cache comparator: {_format_usd(overall_cost.get("no_cache_usd"))}. Khi bỏ pricing, report ghi `n/a`, không biến unknown thành zero-cost.

Pricing status: {"available" if isinstance(pricing, Mapping) else "n/a (pricing skipped)"}.

### 7. Pass thresholds

- optimized logical requests phải bằng {acceptance.get("optimized_expected_requests", OPTIMIZED_COHORT_EXPECTED_REQUESTS)}, completed đủ và final failures bằng {thresholds.get("maximum_optimized_failures", 0)};
- substantive request hit rate tối thiểu {_format_percent(thresholds.get("substantive_request_hit_rate", OPTIMIZED_REQUEST_HIT_FLOOR))};
- đồng thời token-weighted rate tối thiểu {_format_percent(thresholds.get("token_weighted_cache_rate", OPTIMIZED_TOKEN_CACHE_FLOOR))} **hoặc** p50 prefix efficiency tối thiểu {_format_percent(thresholds.get("prefix_efficiency_p50", OPTIMIZED_PREFIX_P50_FLOOR))};
- matched latency không quyết định pass/fail và không tạo causal claim.

### 8. Root-cause evidence

| Factor | Stable/optimized | Degraded | Delta | Status |
| --- | ---: | ---: | ---: | --- |
{root_rows}

`supported` cần delta tối thiểu {_format_percent(ROOT_CAUSE_DELTA_FLOOR)} và mỗi phía có ít nhất {ROOT_CAUSE_MIN_COMPLETED} completed steady samples. `not_observed` không chứng minh factor vô hại.

### 9. Artifact checks

- `manifest.json`: model, prompt hash, suite allocation, payload hashes, price provenance và execution state; không chứa API key hoặc raw prompt.
- `requests.jsonl`: một record/HTTP attempt, terminal usage, cache fields, timing, retry/error đã redact.
- `summary.json`, `summary.csv`, `report.md`: derived artifacts, rebuild được từ hai raw files trên.
- Kiểm tra `attempt_records <= hard_cap`, planned count = {suite.get("planned_requests", PLANNED_ATTEMPTS)}, optimized count = {suite.get("optimized_expected_requests", OPTIMIZED_COHORT_EXPECTED_REQUESTS)}, và không có unresolved final optimized failure.

## Cách triển khai cache với system prompt

### 1. Stable prefix và dynamic tail

Đặt role, policy, trust boundary, workflow, tool contracts, output schema và few-shot ổn định ở đầu. Timestamp, request/session/user ID, retrieved context và user request nằm sau marker đầy đủ:

```text
{marker}
```

Một ký tự đổi trong 1.024 token đầu có thể tạo miss. Tail thay đổi vẫn được xử lý mới; prompt cache không phải response cache.

### 2. Canonical tools và schema

- sort tools theo stable function name;
- serialize JSON với stable key order và separators;
- giữ description, required arrays, enum order và schema name ổn định;
- version contract khi behavior/schema đổi, không randomize order mỗi request.

### 3. Stable low-cardinality `prompt_cache_key`

Dùng hash của `agent_id + prompt_version + stable_bucket`. Không dùng request ID, timestamp, session ID hoặc user ID. Key hỗ trợ routing nhưng không thay exact prefix match; nếu rate/key quá cao, dùng số bucket nhỏ và ổn định.

### 4. Version/hash và warm-up khi deploy

Mỗi release lưu `prompt_version`, `prompt_sha256`, `tool_schema_sha256`, `output_schema_sha256`. Sau deploy, gửi một warm-up request synthetic cho từng stable bucket, xác nhận `cached_tokens`, rồi mới tăng traffic canary. Prompt version cũ và mới dùng key khác nhau.

### 5. Python Responses API sample

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=(
        os.environ.get("OPENAI_BASER_URL")
        or os.environ["OPENAI_BASE_URL"]
    ),
    api_key=os.environ["OPENAI_API_KEY"],
    max_retries=0,
)
terminal = None
stream = client.responses.create(
    model="{summary.get("model")}",
    instructions=stable_system_prompt,
    input=dynamic_user_input,
    prompt_cache_key=stable_cache_key,
    tools=canonical_tools,
    text=canonical_text_config,
    max_output_tokens=512,
    store=False,
    stream=True,
)
for event in stream:
    if event.type == "response.completed":
        terminal = event.response
if terminal is None:
    raise RuntimeError("Missing terminal response")
input_tokens = terminal.usage.input_tokens
cached_tokens = terminal.usage.input_tokens_details.cached_tokens
cache_rate = cached_tokens / input_tokens if input_tokens else 0.0
```

Đọc `cached_tokens` từ terminal `response.completed`, không cộng delta events và không suy luận hit từ latency.

### 6. Rollout và monitoring checklist

1. Baseline request hit, token-weighted rate, token mix và 429.
2. Canonicalize prefix/tools/schema/key; chốt version/hash.
3. Dry-run cost ceiling; chạy synthetic benchmark.
4. Warm-up từng deployment/bucket sau release.
5. Canary theo prompt version; theo dõi tokens, first-text, TTLT và errors.
6. Alert khi cache rate giảm, hash cardinality tăng hoặc optimized failures xuất hiện.
7. Rollback prompt/key version khi acceptance gate fail.
8. Đo production theo traffic thật; không biến synthetic uplift thành SLA.

### 7. Cache-breaking anti-patterns

- timestamp/UUID/user/session/RAG document đặt trước common policy;
- tool hoặc schema order thay đổi do unordered collection;
- schema name chứa request nonce;
- `prompt_cache_key` per request;
- random bucket hoặc quá nhiều prompt versions đồng thời;
- warm requests chạy burst ngay sau cold deploy;
- so latency giữa request có output/reasoning token mix khác nhau;
- coi `cached_tokens > 0` là đủ dù hit dưới {MIN_CACHEABLE_TOKENS} hoặc prefix efficiency thấp.

## Nguồn chính thức

{sources}
"""


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
