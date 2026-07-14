from __future__ import annotations

import json
import sys
import unittest
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark_core import (  # noqa: E402
    DYNAMIC_START,
    EXPECTED_ALLOCATION,
    MATCHED_MAX_OUTPUT_TOKENS,
    MATCHED_PAIR_COUNT,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    PLANNED_ATTEMPTS,
    SAMPLE_SUMMARY_ALLOWLIST,
    AttemptBudget,
    BudgetExhausted,
    ConfigurationError,
    build_isolation_probe,
    build_suite,
    build_summary,
    compute_cost,
    load_prompt_asset,
    manifest_payload,
    normalize_base_url,
    override_price_book,
    redact_text,
    render_markdown_report,
    render_system_prompt,
    resolve_endpoint_env,
    sanitize_summary,
    split_prompt_boundary,
    validate_prompt_asset,
)


def completed_record(
    spec,
    *,
    cached_tokens: int | None = None,
    attempt_number: int | None = None,
) -> dict[str, object]:
    prefix = spec.cacheable_prefix_tokens_estimate
    input_tokens = prefix + 128
    if cached_tokens is None:
        if spec.expected_cache_state == "warm":
            cached_tokens = min(input_tokens, max(1_024, int(prefix * 0.90)))
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


class SuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.asset = load_prompt_asset()
        cls.specs = build_suite(
            run_id="unit-suite",
            asset=cls.asset,
            idle_gap_seconds=0,
            paced_interval_seconds=0,
        )

    def test_prompt_asset_and_full_dynamic_marker(self) -> None:
        self.assertEqual([], validate_prompt_asset(self.asset))
        self.assertEqual(1, self.asset.template.count(DYNAMIC_START))
        self.assertGreaterEqual(self.asset.word_count, 4_000)
        self.assertLessEqual(self.asset.word_count, 5_000)

    def test_exact_allocation_and_order(self) -> None:
        self.assertEqual(PLANNED_ATTEMPTS, len(self.specs))
        self.assertEqual(
            EXPECTED_ALLOCATION,
            dict(Counter(spec.experiment for spec in self.specs)),
        )
        self.assertEqual(
            list(range(1, PLANNED_ATTEMPTS + 1)),
            [spec.order for spec in self.specs],
        )
        experiment_order = list(
            dict.fromkeys(spec.experiment for spec in self.specs)
        )
        self.assertEqual(
            [
                "qualification",
                "prefix_stability",
                "tool_schema_stability",
                "cache_key_cardinality",
                "traffic_shape",
                "idle_retention",
                "matched_latency",
            ],
            experiment_order,
        )

    def test_optimized_cohort_is_exactly_44_warm_requests(self) -> None:
        optimized = [
            spec for spec in self.specs if spec.optimized_cohort
        ]
        self.assertEqual(
            OPTIMIZED_COHORT_EXPECTED_REQUESTS,
            len(optimized),
        )
        self.assertEqual(
            {
                ("qualification", "full-prompt"): 2,
                ("prefix_stability", "stable-prefix"): 9,
                ("tool_schema_stability", "canonical"): 9,
                ("cache_key_cardinality", "stable-key"): 9,
                ("traffic_shape", "paced"): 5,
                ("matched_latency", "cold-warm-pair"): 10,
            },
            dict(
                Counter(
                    (spec.experiment, spec.arm)
                    for spec in optimized
                )
            ),
        )
        self.assertTrue(
            all(spec.expected_cache_state == "warm" for spec in optimized)
        )

    def test_matched_pairs_are_unique_adjacent_and_byte_identical(self) -> None:
        grouped = defaultdict(list)
        for spec in self.specs:
            if spec.pair_id:
                grouped[spec.pair_id].append(spec)
        self.assertEqual(MATCHED_PAIR_COUNT, len(grouped))
        namespaces = set()
        keys = set()
        for pair_id, members in grouped.items():
            self.assertEqual(2, len(members), pair_id)
            cold, warm = sorted(members, key=lambda spec: spec.order)
            self.assertEqual("cold", cold.expected_cache_state)
            self.assertEqual("warm", warm.expected_cache_state)
            self.assertEqual(cold.order + 1, warm.order)
            self.assertEqual(
                cold.request_payload_fingerprint(),
                warm.request_payload_fingerprint(),
            )
            self.assertEqual(MATCHED_MAX_OUTPUT_TOKENS, cold.max_output_tokens)
            self.assertEqual(cold.instructions, warm.instructions)
            self.assertEqual(cold.input_text, warm.input_text)
            self.assertEqual(cold.tools, warm.tools)
            self.assertEqual(cold.text_config, warm.text_config)
            self.assertEqual(cold.prompt_cache_key, warm.prompt_cache_key)
            self.assertIsNone(cold.batch_id)
            self.assertIsNone(warm.batch_id)
            namespaces.add(cold.namespace)
            keys.add(cold.prompt_cache_key)
        self.assertEqual(MATCHED_PAIR_COUNT, len(namespaces))
        self.assertEqual(MATCHED_PAIR_COUNT, len(keys))

    def test_stable_prefix_varies_only_after_prompt_boundary(self) -> None:
        stable_specs = [
            spec
            for spec in self.specs
            if spec.experiment == "prefix_stability"
            and spec.arm == "stable-prefix"
        ]
        volatile_specs = [
            spec
            for spec in self.specs
            if spec.experiment == "prefix_stability"
            and spec.arm == "volatile-prefix"
        ]
        stable_a, dynamic_a = split_prompt_boundary(
            stable_specs[0].instructions
        )
        stable_b, dynamic_b = split_prompt_boundary(
            stable_specs[1].instructions
        )
        self.assertEqual(stable_a, stable_b)
        self.assertNotEqual(dynamic_a, dynamic_b)
        volatile_a, _ = split_prompt_boundary(
            volatile_specs[0].instructions
        )
        volatile_b, _ = split_prompt_boundary(
            volatile_specs[1].instructions
        )
        self.assertNotEqual(volatile_a, volatile_b)

    def test_isolation_probe_is_three_calls_with_identical_a_payload(self) -> None:
        specs = build_isolation_probe(
            run_id="probe-unit",
            asset=self.asset,
        )
        self.assertEqual(3, len(specs))
        self.assertEqual(
            ["cold", "cold", "warm"],
            [spec.expected_cache_state for spec in specs],
        )
        self.assertEqual(
            specs[0].request_payload_fingerprint(),
            specs[2].request_payload_fingerprint(),
        )
        self.assertNotEqual(specs[0].namespace, specs[1].namespace)

    def test_rendered_prompt_has_resolved_dynamic_tail(self) -> None:
        rendered = render_system_prompt(
            self.asset,
            namespace="render-unit",
            case_id="render-unit",
            timestamp_utc="2026-07-14T10:00:00Z",
        )
        self.assertIn(DYNAMIC_START, rendered)
        self.assertIn("2026-07-14T10:00:00Z", rendered)
        _, dynamic_tail = split_prompt_boundary(rendered)
        self.assertNotRegex(dynamic_tail, r"\{\{[A-Z0-9_]+\}\}")


class UrlAndBudgetTests(unittest.TestCase):
    def test_url_normalization_and_env_precedence(self) -> None:
        self.assertEqual(
            "https://example.openai.azure.com/openai/v1/",
            normalize_base_url("https://example.openai.azure.com"),
        )
        self.assertEqual(
            "http://127.0.0.1:8080/v1/",
            normalize_base_url("http://127.0.0.1:8080"),
        )
        resolved, source = resolve_endpoint_env(
            {
                "OPENAI_BASER_URL": "https://preferred.openai.azure.com",
                "OPENAI_BASE_URL": "https://standard.openai.azure.com",
            }
        )
        self.assertIn("preferred.openai.azure.com", resolved)
        self.assertEqual("OPENAI_BASER_URL", source)

    def test_url_rejects_unsafe_shapes(self) -> None:
        invalid = (
            "http://example.openai.azure.com",
            "https://user:pass@localhost",
            "https://example.openai.azure.com?api-version=x",
            "https://example.openai.azure.com/#fragment",
            (
                "https://example.openai.azure.com/openai/deployments/"
                "gpt-5.4-mini"
            ),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    normalize_base_url(value)

    def test_redaction_removes_secret_url_and_hostname(self) -> None:
        secret = "unit-secret"
        url = "https://example.openai.azure.com/openai/v1/"
        redacted = redact_text(
            f"api_key={secret} endpoint={url} host=example.openai.azure.com",
            secrets=(secret,),
            base_url=url,
        )
        self.assertNotIn(secret, redacted)
        self.assertNotIn("example.openai.azure.com", redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)
        self.assertIn("[REDACTED_ENDPOINT]", redacted)

    def test_attempt_budget_is_atomic_and_hard_capped(self) -> None:
        budget = AttemptBudget(maximum=10)

        def claim(index: int) -> bool:
            try:
                budget.claim(
                    logical_request_id=f"request-{index}",
                    reason="unit",
                )
                return True
            except BudgetExhausted:
                return False

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(claim, range(20)))
        self.assertEqual(10, sum(results))
        self.assertEqual(10, budget.used)
        self.assertEqual(0, budget.remaining)
        self.assertEqual(10, len(budget.snapshot()["claims"]))


class SummaryAndReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.asset = load_prompt_asset()
        cls.specs = build_suite(
            run_id="summary-unit",
            asset=cls.asset,
            idle_gap_seconds=0,
            paced_interval_seconds=0,
        )
        cls.price_book = override_price_book(
            input_price=1.0,
            cached_input_price=0.1,
            output_price=5.0,
        )

    def build_full_summary(self, *, priced: bool = True) -> dict[str, object]:
        records = [completed_record(spec) for spec in self.specs]
        return build_summary(
            run_id="summary-unit",
            model="gpt-5.4-mini",
            records=records,
            price_book=self.price_book if priced else None,
            attempt_budget={
                "maximum": 120,
                "used": len(records),
                "remaining": 120 - len(records),
                "claims": [{"secret": "must-not-copy"}],
            },
            run_status="completed",
            completed_at="2026-07-14T10:00:00Z",
            prompt_metadata={
                "path": self.asset.path,
                "sha256": self.asset.sha256,
                "word_count": self.asset.word_count,
                "estimated_tokens": self.asset.estimated_tokens,
                "encoding": self.asset.encoding,
                "dynamic_start_marker": DYNAMIC_START,
            },
        )

    def test_cost_formulas_and_savings_percentages(self) -> None:
        cost = compute_cost(
            {
                "input_tokens": 1_000,
                "cached_tokens": 800,
                "uncached_input_tokens": 200,
                "output_tokens": 100,
            },
            self.price_book,
        )
        self.assertAlmostEqual(0.0002, cost["uncached_input_usd"])
        self.assertAlmostEqual(0.00008, cost["cached_input_usd"])
        self.assertAlmostEqual(0.001, cost["no_cache_input_usd"])
        self.assertAlmostEqual(0.72, cost["input_savings_rate"])
        self.assertAlmostEqual(
            cost["savings_usd"] / cost["no_cache_usd"],
            cost["total_savings_rate"],
        )

    def test_summary_acceptance_and_matched_latency(self) -> None:
        summary = self.build_full_summary()
        self.assertEqual(
            OPTIMIZED_COHORT_EXPECTED_REQUESTS,
            summary["optimized"]["logical_requests"],
        )
        self.assertEqual("pass", summary["acceptance"]["status"])
        self.assertEqual(
            MATCHED_PAIR_COUNT,
            summary["matched_latency"]["valid_pairs"],
        )
        self.assertFalse(
            summary["matched_latency"]["causal_latency_claimed"]
        )
        self.assertEqual(
            "2026-07-14T10:00:00Z",
            summary["generated_at"],
        )

    def test_report_has_required_guides_and_dynamic_counts(self) -> None:
        report = render_markdown_report(self.build_full_summary())
        self.assertIn("## Cách benchmark cache efficiency", report)
        self.assertIn(
            "## Cách triển khai cache với system prompt",
            report,
        )
        self.assertIn(DYNAMIC_START, report)
        self.assertIn(f"exact allocation {PLANNED_ATTEMPTS}", report)
        self.assertIn(
            f"đúng {OPTIMIZED_COHORT_EXPECTED_REQUESTS} warm requests",
            report,
        )
        self.assertIn("prompt_cache_key", report)
        self.assertIn("response.completed", report)
        self.assertIn("causal_latency_claimed=false", report)

    def test_unpriced_report_uses_na_not_zero_dollars(self) -> None:
        report = render_markdown_report(self.build_full_summary(priced=False))
        self.assertIn("n/a (pricing skipped)", report)
        self.assertNotIn("$0.000000", report)

    def test_sanitized_summary_is_explicit_allowlist(self) -> None:
        summary = self.build_full_summary()
        summary["endpoint"] = {"hostname": "secret.example.com"}
        summary["response_id"] = "resp-secret"
        sanitized = sanitize_summary(summary)
        self.assertEqual(SAMPLE_SUMMARY_ALLOWLIST, set(sanitized))
        self.assertEqual(
            [
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
            ],
            list(sanitized["overall"]),
        )
        serialized = json.dumps(sanitized, sort_keys=True)
        for forbidden in (
            "secret.example.com",
            "resp-secret",
            "must-not-copy",
            "attempt_budget",
            "claims",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(sanitized["sample"]["sanitized"])
        self.assertEqual(
            "sanitized_live_run",
            sanitized["provenance"]["source_kind"],
        )
        report = render_markdown_report(
            sanitized,
            sanitized_sample=True,
        )
        self.assertIn("Mẫu đã khử nhạy cảm", report)

    def test_manifest_contains_hashes_not_raw_prompt(self) -> None:
        manifest = manifest_payload(
            run_id="manifest-unit",
            model="gpt-5.4-mini",
            asset=self.asset,
            specs=self.specs,
            max_attempts=120,
            price_book=self.price_book,
        )
        serialized = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn(self.asset.template, serialized)
        self.assertEqual(PLANNED_ATTEMPTS, len(manifest["request_plan"]))
        self.assertTrue(
            all(
                row.get("request_payload_sha256")
                for row in manifest["request_plan"]
            )
        )


if __name__ == "__main__":
    unittest.main()
