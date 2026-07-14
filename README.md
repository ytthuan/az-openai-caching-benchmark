# Azure OpenAI Prompt Cache Benchmark

Standalone internal benchmark for Azure OpenAI v1 Responses API prompt caching with deployment default `gpt-5.4-mini`.

Scope is cache efficiency only:

- terminal `usage.input_tokens_details.cached_tokens`;
- streaming first-event, first-text, TTLT and estimated TBT;
- dynamic Azure Retail Prices API lookup for uncached input, cached input and output;
- cold/warm controls and A/B isolation for prefix volatility, tool/schema churn, cache-key cardinality and traffic shape;
- ten namespace-isolated matched latency pairs;
- reproducible cache/cost/reliability report.

Prompt caching reuses computation for identical prompt prefixes. It is not response caching: dynamic tail still runs and model still generates output.

## Repository

| Path | Purpose |
| --- | --- |
| `benchmark.py` | CLI, streaming Responses runner, shared HTTP-attempt budget, pricing lookup and artifact I/O |
| `benchmark_core.py` | Prompt/suite construction, validation, metrics, acceptance, report and sample sanitization |
| `prompts/enterprise_agent_creator_system_prompt_vi.md` | Long Vietnamese synthetic enterprise system prompt with stable-prefix/dynamic-tail boundary |
| `tests/` | Unit tests; no model calls |
| `GUIDE_VI.md` | Detailed benchmark and production implementation guide |
| `examples/` | Allowlisted aggregate sample from one real live run |
| `runs/` | Local raw run artifacts; ignored by Git |

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Only these dotenv keys are read:

```dotenv
OPENAI_BASER_URL="https://<resource>.openai.azure.com/openai/v1/"
OPENAI_BASE_URL="https://<resource>.openai.azure.com/openai/v1/"
OPENAI_API_KEY=""
```

Rules:

- process environment wins over `.env` for each key;
- `OPENAI_BASER_URL` wins over `OPENAI_BASE_URL` when both are present;
- remote endpoints require HTTPS;
- HTTP is accepted only for loopback proxies;
- query, fragment, user information and deployment-specific URL forms are rejected;
- resource roots are normalized to `/openai/v1/`;
- `.env` and `runs/` are Git-ignored.

Use an external secure env file without copying it:

```bash
.venv/bin/python benchmark.py live \
  --confirm-live \
  --env-file /secure/path/benchmark.env
```

## Workflow

### 1. Compile and test

```bash
.venv/bin/python -m py_compile benchmark.py benchmark_core.py
.venv/bin/python -m unittest discover -s tests -v
```

### 2. Dry-run and cost preflight

```bash
.venv/bin/python benchmark.py dry-run
```

Dry-run:

- does not call model;
- validates exact 102-request allocation and order;
- validates exact 44-request optimized cohort;
- validates ten adjacent cold/warm pairs, unique pair namespaces/keys and byte-identical payload per pair;
- validates prompt boundary and hard cap;
- queries Azure Retail Prices API unless `--skip-pricing`;
- writes `runs/<dry-run-id>/manifest.json` with conservative no-cache ceiling for all 120 possible HTTP attempts.

For private or enterprise rates, override all three prices in USD/1M tokens:

```bash
.venv/bin/python benchmark.py dry-run \
  --input-price 0.75 \
  --cached-input-price 0.075 \
  --output-price 4.50
```

Partial override is rejected. `--skip-pricing` renders cost as `n/a`, never as zero.

### 3. Live run

```bash
.venv/bin/python benchmark.py live --confirm-live
```

Live sequence:

1. Run three-call capability/isolation probe: A-cold, B-cold, A-warm.
2. Run 102 planned requests.
3. Allow bounded retry only for transient request failures; matched latency and concurrent burst members do not retry.
4. Claim the same atomic budget before every HTTP attempt.
5. Stop fail-closed at hard cap 120.
6. Finalize manifest, summary, CSV and report even when run ends incomplete.

SDK internal retries are disabled. Default idle-retention wait is 660 seconds. A reduced value is useful for harness checks but not retention conclusions:

```bash
.venv/bin/python benchmark.py live \
  --confirm-live \
  --idle-gap-seconds 30
```

### 4. Offline report rebuild

```bash
.venv/bin/python benchmark.py report runs/<run-id>
```

Rebuild reads only:

- `manifest.json`;
- `requests.jsonl`.

It does not call model or Azure Retail Prices API. The pricing snapshot and completion timestamp come from manifest, so repeated rebuilds are deterministic.

### 5. Sanitized sample generation

```bash
.venv/bin/python benchmark.py sample \
  runs/<live-run-id>/summary.json \
  --output-dir examples
```

The explicit allowlist preserves aggregate cache, cost, latency, root-cause, prompt, pricing and provenance data. It excludes endpoint fingerprint, raw prompt/input/output, response/request IDs, error-attempt ledger and claim records.

## Suite allocation

| Experiment | Requests | Design |
| --- | ---: | --- |
| `qualification` | 6 | three under-threshold controls; full prompt cold + two warm |
| `prefix_stability` | 20 | ten volatile-prefix; stable-prefix cold + nine warm |
| `tool_schema_stability` | 20 | ten shuffled; canonical cold + nine warm |
| `cache_key_cardinality` | 20 | ten per-request-key; stable-key cold + nine warm |
| `traffic_shape` | 12 | burst cold + five concurrent warm; paced cold + five warm |
| `matched_latency` | 20 | ten unique cold/warm pairs |
| `idle_retention` | 4 | seed, immediate warm, post-idle, rewarm |
| **Total** | **102** | hard cap remains 120 |

Optimized cohort is exactly 44 warm requests:

- qualification full-prompt warm: 2;
- stable-prefix steady: 9;
- canonical steady: 9;
- stable-key steady: 9;
- paced steady: 5;
- matched-latency warm: 10.

Cold seeds, degraded diagnostic arms and idle-retention requests are excluded from optimized acceptance.

## Matched latency

Each of ten pairs has a unique:

- `pair_id`;
- namespace embedded in instructions;
- `prompt_cache_key`.

Within one pair, instructions, input, tools, text config, cache key and fixed `max_output_tokens` are byte-identical. A pair is valid only when:

- cold completed without retry and `cached_tokens == 0`;
- warm completed without retry;
- warm has at least 1,024 cached tokens;
- warm cached tokens are at least 80% of estimated cacheable prefix.

Latency result is directional. Report always sets `causal_latency_claimed=false`.

## Metrics

```text
request_hit_rate =
  completed_requests(cached_tokens > 0) / completed_requests

substantive_request_hit_rate =
  completed_requests(cached_tokens >= 1,024
    and cached_tokens / estimated_cacheable_prefix >= 80%)
  / completed_requests

token_weighted_cache_rate =
  sum(cached_tokens) / sum(input_tokens)

actual_cost =
  uncached_input_tokens × input_rate
  + cached_tokens × cached_input_rate
  + output_tokens × output_rate

input_savings_rate =
  input_savings_usd / no_cache_input_cost_usd

total_savings_rate =
  total_savings_usd / no_cache_total_cost_usd
```

`input_tokens` includes cached tokens. Reasoning tokens are already included in output tokens and are not charged twice.

Streaming timing:

- first event: first stream event after request start;
- first text: first non-empty output text delta;
- TTLT: request start through terminal `response.completed`;
- TBT: `(terminal - first text) / (visible output token estimate - 1)`.

## Interpretation

Three scopes must remain separate:

- **overall**: all final logical requests, including capability probe and cold seeds;
- **optimized**: 44 production-like warm requests and only acceptance denominator;
- **degraded**: deliberately unstable A/B arms used for diagnosis.

Acceptance passes only when:

- all 44 optimized requests complete with zero final failure;
- optimized substantive request hit rate is at least 90%;
- optimized token-weighted cache rate is at least 80%, or p50 prefix efficiency is at least 90%.

Root-cause factor is `supported` only when stable-versus-degraded token-weighted delta is at least 10 percentage points and both sides have at least five completed steady samples.

## Artifacts

Raw local run:

| File | Schema |
| --- | --- |
| `manifest.json` | mode, model, prompt hash/size/boundary, exact suite allocation, hashed request plan, price snapshot, endpoint fingerprint, execution state and hard-cap ledger |
| `requests.jsonl` | one row per HTTP attempt with logical ID, hashes, expected/observed cache state, terminal usage, cost, streaming timing and redacted error |
| `summary.json` | aggregate scopes, arms, root causes, matched latency, acceptance, prompt/pricing provenance |
| `summary.csv` | one row per experiment/arm |
| `report.md` | Vietnamese benchmark and system-prompt cache deployment report |

Raw artifacts can contain service-generated identifiers and must remain under ignored `runs/`. Committed examples are sanitized aggregates only.

## Limitations

- Cache retention is service-managed and can change with inactivity, load and deployment topology.
- `prompt_cache_key` influences routing but does not replace exact prefix match.
- Synthetic 102-request result does not guarantee production traffic uplift.
- Failed attempts without terminal usage can still incur service cost; report cannot infer unknown usage.
- Retail Prices API returns public list prices, not negotiated billing.
- Latency varies with model load and generated tokens; matched pairs reduce confounding but do not prove causality.

## Official sources

- https://learn.microsoft.com/azure/ai-services/openai/how-to/prompt-caching
- https://learn.microsoft.com/azure/ai-services/openai/how-to/responses
- https://learn.microsoft.com/azure/ai-services/openai/how-to/latency
- https://prices.azure.com/api/retail/prices
