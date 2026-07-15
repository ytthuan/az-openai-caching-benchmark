from __future__ import annotations

from typing import Any, Mapping

from ..constants import (
    DYNAMIC_START,
    EXPECTED_ALLOCATION,
    MATCHED_MAX_OUTPUT_TOKENS,
    MATCHED_PAIR_COUNT,
    MAX_ATTEMPTS_DEFAULT,
    MIN_CACHEABLE_TOKENS,
    OFFICIAL_SOURCES,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    OPTIMIZED_PREFIX_P50_FLOOR,
    OPTIMIZED_REQUEST_HIT_FLOOR,
    OPTIMIZED_TOKEN_CACHE_FLOOR,
    PLANNED_ATTEMPTS,
    PROBE_ATTEMPTS,
    ROOT_CAUSE_DELTA_FLOOR,
    ROOT_CAUSE_MIN_COMPLETED,
    WARM_PREFIX_EFFICIENCY_FLOOR,
)
from .report_sections import (
    allocation_table,
    format_ms,
    format_percent,
    format_usd,
    root_cause_table,
)


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
    allocation_rows = allocation_table(allocation)
    root_rows = root_cause_table(summary.get("root_causes") or [])
    marker = str(prompt.get("dynamic_start_marker") or DYNAMIC_START)
    sources = "\n".join(f"- {url}" for url in OFFICIAL_SOURCES)
    return f"""# Báo cáo Azure OpenAI Prompt Cache Benchmark

{sample_notice}Run `{summary.get("run_id")}` dùng model `{summary.get("model")}`. Trạng thái run: `{summary.get("run_status")}`. Acceptance cache-only: **{acceptance.get("status", "incomplete")}**.

## Kết quả chính

| Phạm vi | Logical requests | Completed | Substantive hit rate | Token-weighted cache rate | Input savings | Total savings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Overall | {overall.get("logical_requests", 0)} | {overall.get("completed", 0)} | {format_percent(overall.get("substantive_request_hit_rate"))} | {format_percent(overall.get("token_weighted_cache_rate"))} | {format_percent(overall_cost.get("input_savings_rate"))} | {format_percent(overall_cost.get("total_savings_rate"))} |
| Optimized | {optimized.get("logical_requests", 0)} | {optimized.get("completed", 0)} | {format_percent(optimized.get("substantive_request_hit_rate"))} | {format_percent(optimized.get("token_weighted_cache_rate"))} | {format_percent(optimized_cost.get("input_savings_rate"))} | {format_percent(optimized_cost.get("total_savings_rate"))} |
| Degraded diagnostic | {degraded.get("logical_requests", 0)} | {degraded.get("completed", 0)} | {format_percent(degraded.get("substantive_request_hit_rate"))} | {format_percent(degraded.get("token_weighted_cache_rate"))} | n/a | n/a |

Overall gồm cold seeds và {suite.get("probe_requests", PROBE_ATTEMPTS)} capability/isolation calls. Optimized chỉ gồm đúng {suite.get("optimized_expected_requests", OPTIMIZED_COHORT_EXPECTED_REQUESTS)} warm requests production-like. Degraded là các arm cố ý gây prefix/tool/key/traffic instability; không dùng chúng làm acceptance denominator.

## Cách benchmark cache efficiency

### 1. Preflight và dry-run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
.venv/bin/azure-openai-cache-benchmark dry-run
```

Dry-run validate prompt boundary, exact allocation {suite.get("planned_requests", PLANNED_ATTEMPTS)}, optimized cohort {suite.get("optimized_expected_requests", OPTIMIZED_COHORT_EXPECTED_REQUESTS)}, {suite.get("matched_pair_count", MATCHED_PAIR_COUNT)} pair identities, hard cap {suite.get("hard_cap", MAX_ATTEMPTS_DEFAULT)} và cost ceiling. Dry-run không gọi model; mặc định chỉ gọi Azure Retail Prices API để lấy ba meter.

### 2. Cold/warm và A/B design

| Experiment | Planned requests |
| --- | ---: |
{allocation_rows}

Mỗi optimized arm có một cold seed bị loại khỏi denominator, sau đó là warm steady requests. A/B giữ workload tương đương và chỉ đổi một factor: prefix volatility, tool/schema order, cache-key cardinality hoặc traffic shape.

### 3. Pair isolation cho matched latency

Có {matched.get("planned_pairs", MATCHED_PAIR_COUNT)} cặp cold/warm. Mỗi cặp dùng `pair_id`, namespace và `prompt_cache_key` riêng; cặp 2–{matched.get("planned_pairs", MATCHED_PAIR_COUNT)} không thể reuse prefix của cặp trước. Trong một cặp, `instructions`, `input`, `tools`, `text`, cache key và `max_output_tokens={MATCHED_MAX_OUTPUT_TOKENS}` byte-identical. Chỉ nhận cặp khi cold có `cached_tokens=0` và warm là substantive hit.

Valid pairs: {matched.get("valid_pairs", 0)}/{matched.get("planned_pairs", MATCHED_PAIR_COUNT)}. Warm-faster TTLT: {matched.get("warm_faster_ttlt_pairs", 0)}; cold-faster TTLT: {matched.get("cold_faster_ttlt_pairs", 0)}; median warm-minus-cold TTLT: {format_ms(matched.get("warm_minus_cold_ttlt_ms_p50"))}. `causal_latency_claimed={str(bool(matched.get("causal_latency_claimed"))).lower()}`: latency chỉ directional vì load, routing và token-generation variance vẫn là confounders.

### 4. Lệnh chạy và rebuild

```bash
.venv/bin/azure-openai-cache-benchmark live --confirm-live
.venv/bin/python -m azure_openai_cache_benchmark report runs/<run-id>
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
| Uncached input | {format_usd(price_input)} |
| Cached input | {format_usd(price_cached)} |
| Output | {format_usd(price_output)} |

Nguồn pricing: `{pricing.get("source") if isinstance(pricing, Mapping) else "n/a"}`; retrieved at `{pricing.get("retrieved_at") if isinstance(pricing, Mapping) else "n/a"}`. Actual aggregate cost: {format_usd(overall_cost.get("actual_usd"))}; no-cache comparator: {format_usd(overall_cost.get("no_cache_usd"))}. Khi bỏ pricing, report ghi `n/a`, không biến unknown thành zero-cost.

Pricing status: {"available" if isinstance(pricing, Mapping) else "n/a (pricing skipped)"}.

### 7. Pass thresholds

- optimized logical requests phải bằng {acceptance.get("optimized_expected_requests", OPTIMIZED_COHORT_EXPECTED_REQUESTS)}, completed đủ và final failures bằng {thresholds.get("maximum_optimized_failures", 0)};
- substantive request hit rate tối thiểu {format_percent(thresholds.get("substantive_request_hit_rate", OPTIMIZED_REQUEST_HIT_FLOOR))};
- đồng thời token-weighted rate tối thiểu {format_percent(thresholds.get("token_weighted_cache_rate", OPTIMIZED_TOKEN_CACHE_FLOOR))} **hoặc** p50 prefix efficiency tối thiểu {format_percent(thresholds.get("prefix_efficiency_p50", OPTIMIZED_PREFIX_P50_FLOOR))};
- matched latency không quyết định pass/fail và không tạo causal claim.

### 8. Root-cause evidence

| Factor | Stable/optimized | Degraded | Delta | Status |
| --- | ---: | ---: | ---: | --- |
{root_rows}

`supported` cần delta tối thiểu {format_percent(ROOT_CAUSE_DELTA_FLOOR)} và mỗi phía có ít nhất {ROOT_CAUSE_MIN_COMPLETED} completed steady samples. `not_observed` không chứng minh factor vô hại.

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
    base_url=os.environ["OPENAI_BASE_URL"],
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
