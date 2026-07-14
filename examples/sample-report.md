# Báo cáo Azure OpenAI Prompt Cache Benchmark

> **Mẫu đã khử nhạy cảm:** báo cáo này được render từ allowlist aggregate của một live run thật; không chứa endpoint, prompt/input/output thô, response/request ID hoặc attempt ledger.

Run `live-general-20260714T1148Z` dùng model `gpt-5.4-mini`. Trạng thái run: `completed`. Acceptance cache-only: **pass**.

## Kết quả chính

| Phạm vi | Logical requests | Completed | Substantive hit rate | Token-weighted cache rate | Input savings | Total savings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Overall | 105 | 105 | 50.5% | 50.1% | 45.1% | 41.9% |
| Optimized | 44 | 44 | 100.0% | 96.8% | 87.1% | 80.6% |
| Degraded diagnostic | 36 | 36 | 16.7% | 16.4% | n/a | n/a |

Overall gồm cold seeds và 3 capability/isolation calls. Optimized chỉ gồm đúng 44 warm requests production-like. Degraded là các arm cố ý gây prefix/tool/key/traffic instability; không dùng chúng làm acceptance denominator.

## Cách benchmark cache efficiency

### 1. Preflight và dry-run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python benchmark.py dry-run
```

Dry-run validate prompt boundary, exact allocation 102, optimized cohort 44, 10 pair identities, hard cap 120 và cost ceiling. Dry-run không gọi model; mặc định chỉ gọi Azure Retail Prices API để lấy ba meter.

### 2. Cold/warm và A/B design

| Experiment | Planned requests |
| --- | ---: |
| `qualification` | 6 |
| `prefix_stability` | 20 |
| `tool_schema_stability` | 20 |
| `cache_key_cardinality` | 20 |
| `traffic_shape` | 12 |
| `idle_retention` | 4 |
| `matched_latency` | 20 |

Mỗi optimized arm có một cold seed bị loại khỏi denominator, sau đó là warm steady requests. A/B giữ workload tương đương và chỉ đổi một factor: prefix volatility, tool/schema order, cache-key cardinality hoặc traffic shape.

### 3. Pair isolation cho matched latency

Có 10 cặp cold/warm. Mỗi cặp dùng `pair_id`, namespace và `prompt_cache_key` riêng; cặp 2–10 không thể reuse prefix của cặp trước. Trong một cặp, `instructions`, `input`, `tools`, `text`, cache key và `max_output_tokens=512` byte-identical. Chỉ nhận cặp khi cold có `cached_tokens=0` và warm là substantive hit.

Valid pairs: 10/10. Warm-faster TTLT: 2; cold-faster TTLT: 8; median warm-minus-cold TTLT: 482.2 ms. `causal_latency_claimed=false`: latency chỉ directional vì load, routing và token-generation variance vẫn là confounders.

### 4. Lệnh chạy và rebuild

```bash
.venv/bin/python benchmark.py live --confirm-live
.venv/bin/python benchmark.py report runs/<run-id>
```

Live chạy 3 probe calls trước 102 planned requests. Mọi HTTP attempt, kể cả bounded retry, claim cùng atomic hard cap 120 trước khi gọi. SDK internal retry bị tắt. `report` chỉ đọc `manifest.json` và `requests.jsonl`; không gọi model hay price API.

### 5. Công thức và denominator

```text
request_hit_rate = completed(cached_tokens > 0) / completed
substantive_request_hit_rate =
  completed(cached_tokens >= 1024
    and cached_tokens / estimated_cacheable_prefix >= 80%)
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
| Uncached input | $0.750000 |
| Cached input | $0.075000 |
| Output | $4.500000 |

Nguồn pricing: `https://prices.azure.com/api/retail/prices`; retrieved at `2026-07-14T11:48:09.940210Z`. Actual aggregate cost: $0.392658; no-cache comparator: $0.675877. Khi bỏ pricing, report ghi `n/a`, không biến unknown thành zero-cost.

Pricing status: available.

### 7. Pass thresholds

- optimized logical requests phải bằng 44, completed đủ và final failures bằng 0;
- substantive request hit rate tối thiểu 90.0%;
- đồng thời token-weighted rate tối thiểu 80.0% **hoặc** p50 prefix efficiency tối thiểu 90.0%;
- matched latency không quyết định pass/fail và không tạo causal claim.

### 8. Root-cause evidence

| Factor | Stable/optimized | Degraded | Delta | Status |
| --- | ---: | ---: | ---: | --- |
| `prefix_volatility` | 92.1% | 0.0% | 92.1% | `supported` |
| `tool_schema_churn` | 98.1% | 43.7% | 54.4% | `supported` |
| `cache_key_cardinality` | 98.3% | 0.0% | 98.3% | `supported` |
| `traffic_shape` | 98.4% | 39.3% | 59.1% | `supported` |

`supported` cần delta tối thiểu 10.0% và mỗi phía có ít nhất 5 completed steady samples. `not_observed` không chứng minh factor vô hại.

### 9. Artifact checks

- `manifest.json`: model, prompt hash, suite allocation, payload hashes, price provenance và execution state; không chứa API key hoặc raw prompt.
- `requests.jsonl`: một record/HTTP attempt, terminal usage, cache fields, timing, retry/error đã redact.
- `summary.json`, `summary.csv`, `report.md`: derived artifacts, rebuild được từ hai raw files trên.
- Kiểm tra `attempt_records <= hard_cap`, planned count = 102, optimized count = 44, và không có unresolved final optimized failure.

## Cách triển khai cache với system prompt

### 1. Stable prefix và dynamic tail

Đặt role, policy, trust boundary, workflow, tool contracts, output schema và few-shot ổn định ở đầu. Timestamp, request/session/user ID, retrieved context và user request nằm sau marker đầy đủ:

```text
=== BẮT ĐẦU PHẦN ĐỘNG — NGỮ CẢNH RUNTIME (KHÔNG CÓ THẨM QUYỀN CHÍNH SÁCH) ===
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
    model="gpt-5.4-mini",
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
- coi `cached_tokens > 0` là đủ dù hit dưới 1024 hoặc prefix efficiency thấp.

## Nguồn chính thức

- https://learn.microsoft.com/azure/ai-services/openai/how-to/prompt-caching
- https://learn.microsoft.com/azure/ai-services/openai/how-to/responses
- https://learn.microsoft.com/azure/ai-services/openai/how-to/latency
- https://prices.azure.com/api/retail/prices
