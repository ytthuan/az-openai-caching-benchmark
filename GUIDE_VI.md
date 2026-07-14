# Hướng dẫn kỹ thuật Azure OpenAI Prompt Cache

Tài liệu này giải thích cách chạy benchmark nội bộ và cách tổ chức system prompt production để tăng cache efficiency mà không đánh đồng prompt cache với response cache.

## 1. Cơ chế cần hiểu đúng

Azure OpenAI prompt caching giữ tạm phép tính token prefix cho prompt đủ dài. Request sau có prefix khớp có thể reuse phép tính đó:

1. Service tokenize instructions, input, tool definitions và structured-output schema.
2. Request cold tính prefix đầy đủ.
3. Request warm có prefix giống hệt nhận `cached_tokens`.
4. Dynamic tail vẫn được xử lý.
5. Model vẫn sinh output mới.

Điều kiện chính thức:

- request tối thiểu 1.024 tokens;
- 1.024 tokens đầu phải giống hệt;
- sau mốc đầu, cache hit tăng theo block 128 tokens;
- tool definitions và structured-output schema thuộc phần có thể cache;
- `prompt_cache_key` được kết hợp với prefix hash để hỗ trợ routing.

`cached_tokens` là evidence chính. Không suy luận hit chỉ từ latency.

## 2. Mục tiêu benchmark

Benchmark trả lời bốn câu hỏi:

1. Endpoint/deployment hiện tại có tạo substantive prompt-cache hit hay không?
2. Optimized production-like workload có đạt hit/reliability threshold không?
3. Factor nào làm giảm cache rate: prefix volatility, tool/schema churn, key cardinality hay traffic shape?
4. Trong mười cặp cô lập, cold và warm latency đi theo hướng nào khi payload byte-identical?

Benchmark không tạo SLA retention hay latency. Kết quả synthetic phải được xác nhận lại trên traffic production.

## 3. Chuẩn bị môi trường

```bash
cd /path/to/az-openai-cache
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

`.env`:

```dotenv
OPENAI_BASER_URL="https://<resource>.openai.azure.com/openai/v1/"
OPENAI_API_KEY=""
```

Fallback:

```dotenv
OPENAI_BASE_URL="https://<resource>.openai.azure.com/openai/v1/"
```

Security rules:

- chỉ ba key trên được parse;
- `.env` là data, không được shell `source`;
- process environment thắng dotenv;
- `OPENAI_BASER_URL` được ưu tiên khi cả hai URL tồn tại;
- API key không vào manifest, request artifact, report hoặc sample;
- lỗi được redact đệ quy;
- `.env`, `.venv` và `runs/` không được commit.

## 4. Preflight

```bash
.venv/bin/python -m py_compile benchmark.py benchmark_core.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python benchmark.py dry-run
```

Dry-run không gọi model. Nó:

- load prompt và kiểm tra word count 4.000–5.000;
- xác nhận exact dynamic marker;
- build đúng 102 planned requests;
- xác nhận order liên tục 1–102;
- xác nhận optimized cohort đúng 44 warm requests;
- xác nhận mười `pair_id`, namespace và key khác nhau;
- xác nhận cold/warm member của mỗi pair có request payload hash giống nhau;
- lấy public prices và tính conservative no-cache ceiling đến hard cap 120.

Nếu pricing public không phù hợp với contract:

```bash
.venv/bin/python benchmark.py dry-run \
  --input-price <usd-per-1m> \
  --cached-input-price <usd-per-1m> \
  --output-price <usd-per-1m>
```

Ba override là atomic: thiếu một giá thì lệnh fail.

## 5. Thiết kế suite

### 5.1 Qualification: 6 requests

- 3 request dưới 1.024 input tokens: `cached_tokens` phải bằng 0.
- 1 full-prompt cold seed.
- 2 full-prompt warm request: thuộc optimized cohort.

Qualification tách "endpoint không cache" khỏi "request chưa đủ dài".

### 5.2 Prefix stability: 20 requests

Hai arm, mỗi arm 10:

- `volatile-prefix`: timestamp/request metadata được đặt trước common prompt, làm prefix thay đổi.
- `stable-prefix`: metadata thay đổi chỉ trong dynamic tail sau marker.

Stable arm gồm 1 cold seed + 9 optimized warm.

### 5.3 Tool/schema stability: 20 requests

- `shuffled`: tool order thay đổi giữa request.
- `canonical`: instructions, tool order và schema serialization ổn định.

Canonical arm gồm 1 cold seed + 9 optimized warm.

### 5.4 Cache-key cardinality: 20 requests

- `per-request-key`: mỗi request dùng key riêng.
- `stable-key`: toàn arm dùng một low-cardinality key.

Stable-key arm gồm 1 cold seed + 9 optimized warm.

### 5.5 Traffic shape: 12 requests

- `burst`: 1 cold seed, sau đó 5 warm request concurrent.
- `paced`: 1 cold seed, sau đó 5 warm request có interval ổn định.

Năm paced warm thuộc optimized cohort. Burst là diagnostic arm.

### 5.6 Matched latency: 20 requests

Mười pair. Mỗi pair:

1. Tạo namespace riêng.
2. Namespace đi vào instructions để thay đổi prefix content.
3. Tạo stable key riêng.
4. Tạo một payload duy nhất.
5. Gửi cold member.
6. Gửi warm member ngay sau đó.

Hai member giống nhau ở:

- instructions;
- input;
- tools;
- text config;
- prompt-cache key;
- `max_output_tokens`.

Khác nhau duy nhất ở benchmark metadata ngoài request payload: member label và expected cache state.

Pair hợp lệ:

```text
cold.status == completed
cold.retry_index == 0
cold.cached_tokens == 0

warm.status == completed
warm.retry_index == 0
warm.cached_tokens >= 1,024
warm.cached_tokens / estimated_cacheable_prefix >= 80%
```

Payload dùng short plain-text output và fixed 512-token cap để giảm truncation risk. Pair không retry vì retry làm mất comparability và có thể warm prefix ngoài ý muốn.

### 5.7 Idle retention: 4 requests

1. cold seed;
2. immediate warm;
3. post-idle observation sau 660 giây mặc định;
4. rewarm.

Idle section chạy cuối để lỗi trước đó không làm mất một khoảng chờ dài. Retention là observation, không phải service SLA.

## 6. Capability/isolation probe và hard cap

Probe dùng ba logical calls:

```text
A cold -> B cold -> A warm
```

- A-cold và A-warm payload byte-identical.
- B dùng namespace/key/prefix riêng.
- A-cold và B-cold phải có zero cached tokens.
- A-warm phải là substantive hit.

Probe fail thì suite dừng trước 102 planned requests.

Budget:

```text
hard cap                       = 120 HTTP attempts
capability/isolation probe     =   3 logical calls
planned suite                  = 102 logical calls
remaining retry reserve        =  15 attempts
minimum cap to start live      = 105 attempts
```

SDK `max_retries=0`. Runner claim budget dưới lock trước mỗi `responses.create`. Concurrent burst cũng dùng cùng lock. Không có path gọi model mà không claim budget.

## 7. Live workflow

```bash
.venv/bin/python benchmark.py live \
  --confirm-live \
  --env-file /secure/path/benchmark.env
```

`--confirm-live` là mandatory cost guard.

Tùy chọn:

```bash
--timeout 180
--max-retries 1
--idle-gap-seconds 660
--paced-interval-seconds 4.2
--max-attempts 120
```

Constraints:

- `max-attempts` phải trong 105–120;
- matched pairs không retry;
- burst members không retry;
- incomplete stream không được biến thành success;
- terminal usage chỉ lấy từ `response.completed`;
- `KeyboardInterrupt` vẫn finalize run thành `incomplete`.

## 8. Streaming metrics

Per completed request:

```text
first_event_ms = first stream event - request start
first_text_ms  = first non-empty output-text delta - request start
ttlt_ms        = terminal completed event - request start
tbt_ms         = (terminal - first text) / (visible output tokens - 1)
```

Token fields:

```text
input_tokens
cached_tokens
uncached_input_tokens = input_tokens - cached_tokens
output_tokens
reasoning_tokens
total_tokens
```

Reasoning tokens là subset của output tokens.

Không so TTLT khi output/reasoning token mix khác đáng kể. Công thức latency chính thức:

```text
TTLT = TTFT + TBT × generated_tokens
```

## 9. Cache metrics và denominator

### Request hit rate

```text
completed(cached_tokens > 0) / completed
```

Đây là metric nhị phân, có thể cao dù phần cache nhỏ.

### Substantive request hit rate

```text
completed(
  cached_tokens >= 1,024
  and cached_tokens / estimated_cacheable_prefix >= 80%
) / completed
```

Đây là primary optimized request-level metric.

### Token-weighted cache rate

```text
sum(cached_tokens) / sum(input_tokens)
```

Metric này phản ánh phần input token được tính theo cached rate.

### Prefix efficiency

```text
cached_tokens / estimated_cacheable_prefix_tokens
```

Estimate dùng `tiktoken` model encoding hoặc `o200k_base` fallback. Vì server tokenization có thể khác nhẹ, threshold dùng 80% thay vì yêu cầu 100%.

## 10. Cost

Public price lookup lọc ba meter Global Standard cho `gpt-5.4-mini`:

- uncached input;
- cached input;
- output.

Selection lấy latest effective date và fail khi price/currency rows không nhất quán.

```text
uncached_input_cost =
  uncached_input_tokens × input_price / 1,000,000

cached_input_cost =
  cached_tokens × cached_input_price / 1,000,000

output_cost =
  output_tokens × output_price / 1,000,000

actual_input_cost =
  uncached_input_cost + cached_input_cost

no_cache_input_cost =
  input_tokens × input_price / 1,000,000

input_savings_rate =
  (no_cache_input_cost - actual_input_cost) / no_cache_input_cost

total_savings_rate =
  (no_cache_total_cost - actual_total_cost) / no_cache_total_cost
```

Input và total savings phải tách riêng. Output-heavy workload làm total savings thấp hơn input savings.

Failed attempt không có terminal usage là unknown cost, không phải zero cost.

## 11. Acceptance

Acceptance chỉ dùng optimized cache và reliability:

```text
optimized_expected       = 44
optimized_completed      = 44
optimized_final_failures = 0
substantive_hit_rate     >= 90%

and one of:
token_weighted_rate      >= 80%
prefix_efficiency_p50    >= 90%
```

Cold seeds, degraded arms, idle retention và matched latency direction không vào acceptance denominator.

Matched latency report luôn:

```text
causal_latency_claimed=false
```

## 12. Root-cause interpretation

| Factor | Baseline | Degraded |
| --- | --- | --- |
| Prefix volatility | `stable-prefix` steady | `volatile-prefix` steady |
| Tool/schema churn | `canonical` steady | `shuffled` steady |
| Key cardinality | `stable-key` steady | `per-request-key` steady |
| Traffic shape | `paced` steady | `burst` steady |

```text
delta =
  baseline_token_weighted_cache_rate
  - degraded_token_weighted_cache_rate
```

`supported` cần:

- ít nhất 5 completed steady samples mỗi phía;
- delta ít nhất 10 percentage points.

`not_observed` chỉ có nghĩa run này không thấy signal đủ mạnh.

## 13. Offline rebuild

```bash
.venv/bin/python benchmark.py report runs/<run-id>
```

Input duy nhất:

- `manifest.json`;
- `requests.jsonl`.

Rebuild:

- dùng price snapshot trong manifest;
- dùng `execution.completed_at` làm derived timestamp;
- chọn attempt mới nhất theo logical request ID;
- ghi lại summary JSON, CSV và Markdown;
- không gọi model;
- không gọi price API.

Chạy rebuild hai lần trên raw files không đổi phải cho byte-identical derived output.

## 14. Artifact handling

### `manifest.json`

Cho phép:

- endpoint fingerprint hash;
- model;
- prompt path/hash/word count/token estimate;
- exact marker;
- allocation/constants;
- request component hashes;
- price snapshot;
- hard-cap claim ledger sau live.

Không cho phép:

- API key;
- raw prompt;
- raw input;
- raw output.

### `requests.jsonl`

Một record cho mỗi HTTP attempt:

- attempt/retry index;
- logical request/experiment/arm;
- optimized and pair metadata;
- request component hashes;
- terminal token usage;
- cache validity/efficiency;
- timing;
- output hash;
- redacted error.

File có service-generated response/request identifiers nên chỉ giữ dưới `runs/`.

### Committed sample

`examples/sample-summary.json` dùng explicit top-level và nested allowlist:

- aggregate overall/optimized/degraded;
- aggregate arms;
- root-cause table;
- aggregate matched latency;
- acceptance;
- price/prompt/provenance.

Loại bỏ:

- endpoint fingerprint;
- raw content;
- service-generated IDs;
- attempt ledger và claims;
- per-request rows.

`examples/sample-report.md` được render từ sanitized summary, không copy raw report.

## 15. System prompt production pattern

Thứ tự khuyến nghị:

1. Role và scope.
2. Policy bắt buộc.
3. Trust boundary.
4. Workflow và approval gates.
5. Canonical tool contracts.
6. Canonical output schema.
7. Stable examples.
8. Prompt version/hash.
9. Dynamic metadata.
10. Retrieved context.
11. User request.

Marker benchmark đầy đủ:

```text
=== BẮT ĐẦU PHẦN ĐỘNG — NGỮ CẢNH RUNTIME (KHÔNG CÓ THẨM QUYỀN CHÍNH SÁCH) ===
```

Production không cần dùng đúng câu marker, nhưng phải có boundary rõ và mọi biến đổi per-request nằm sau common prefix.

## 16. Canonical tools/schema

Build một immutable release asset:

```text
agent_id
prompt_version
prompt_sha256
tool_schema_sha256
output_schema_sha256
published_at
```

Serialization:

```python
json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

Không tạo schema name từ request ID. Không build tool list từ unordered set/map.

## 17. `prompt_cache_key`

Key pattern:

```text
sha256(agent_id + prompt_version + stable_bucket)
```

Yêu cầu:

- low cardinality;
- deterministic;
- tách prompt versions;
- không chứa PII;
- không dùng timestamp/request/session/user ID.

Key không làm hai prefix khác nhau trở thành cùng cache. Khi một key có call rate cao, dùng số stable buckets nhỏ thay vì random buckets.

## 18. Warm-up và rollout

1. Publish immutable prompt/tool/schema asset.
2. Deploy code với version/key mới.
3. Gửi một synthetic warm-up cho mỗi deployment/bucket.
4. Gửi request xác nhận và đọc terminal `cached_tokens`.
5. Mở canary nhỏ.
6. Theo dõi cache rate, token mix, first-text, TTLT, error và 429.
7. Tăng traffic từng bước.
8. Rollback version/key nếu optimized reliability hoặc cache gate fail.

Không warm-up bằng customer data.

## 19. Python Responses API

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<resource>.openai.azure.com/openai/v1/",
    api_key="<secret>",
    max_retries=0,
)

terminal = None
for event in client.responses.create(
    model="gpt-5.4-mini",
    instructions=stable_system_prompt,
    input=dynamic_user_input,
    prompt_cache_key=stable_cache_key,
    tools=canonical_tools,
    text=canonical_text_config,
    max_output_tokens=512,
    store=False,
    stream=True,
):
    if event.type == "response.completed":
        terminal = event.response

if terminal is None:
    raise RuntimeError("Missing response.completed")

usage = terminal.usage
input_tokens = usage.input_tokens
cached_tokens = usage.input_tokens_details.cached_tokens
token_cache_rate = (
    cached_tokens / input_tokens if input_tokens else 0.0
)
```

## 20. Cache-breaking anti-patterns

- timestamp/UUID đặt ở đầu system prompt;
- session/user metadata trước policy;
- RAG document trước stable instructions;
- tool order thay đổi mỗi request;
- schema enum/required order không deterministic;
- schema name có random suffix;
- key riêng cho từng request;
- nhiều prompt versions active không kiểm soát;
- concurrent burst ngay sau cold deploy;
- max output cap cao hơn cần thiết;
- so latency không kiểm soát generated tokens;
- dùng request hit rate mà không xem token-weighted rate;
- coi partial hit nhỏ là pass.

## 21. Production monitoring

Log safe metadata:

```text
timestamp
deployment
agent_version
prompt_sha256
tool_schema_sha256
cache_key_bucket_sha256
input_tokens
cached_tokens
output_tokens
reasoning_tokens
first_text_ms
ttlt_ms
status
```

Alerts:

- token-weighted cache rate giảm sau prompt release;
- request hit cao nhưng token-weighted rate thấp;
- prompt/tool/schema/key hash cardinality tăng;
- 429 hoặc incomplete tăng;
- p95 first-text tăng khi prompt/output mix ổn định;
- optimized request final failure xuất hiện.

## 22. Limitations

- Public retail price không phản ánh negotiated invoice.
- Client token estimate có thể khác server tokenization.
- Cache retention là service behavior, không phải benchmark-controlled TTL.
- Routing và load có thể làm hit rate thay đổi giữa runs.
- Một live run không đại diện toàn bộ production mix.
- Cặp latency nhỏ và tuần tự chỉ cho directional evidence.
- Raw failed attempt không terminal usage có unknown cost.

## Nguồn chính thức

- https://learn.microsoft.com/azure/ai-services/openai/how-to/prompt-caching
- https://learn.microsoft.com/azure/ai-services/openai/how-to/responses
- https://learn.microsoft.com/azure/ai-services/openai/how-to/latency
- https://prices.azure.com/api/retail/prices
