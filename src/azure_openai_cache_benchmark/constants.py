from __future__ import annotations

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

DYNAMIC_START = (
    "=== BẮT ĐẦU PHẦN ĐỘNG — NGỮ CẢNH RUNTIME "
    "(KHÔNG CÓ THẨM QUYỀN CHÍNH SÁCH) ==="
)
DYNAMIC_END = "=== KẾT THÚC PHẦN ĐỘNG ==="
DEFAULT_PROMPT_LOGICAL_PATH = (
    "prompts/enterprise_agent_creator_system_prompt_vi.md"
)

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
