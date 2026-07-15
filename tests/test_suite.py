from __future__ import annotations

import unittest
from collections import Counter, defaultdict

from azure_openai_cache_benchmark.constants import (
    EXPECTED_ALLOCATION,
    MATCHED_MAX_OUTPUT_TOKENS,
    MATCHED_PAIR_COUNT,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    PLANNED_ATTEMPTS,
)
from azure_openai_cache_benchmark.prompt_assets import (
    load_prompt_asset,
    split_prompt_boundary,
)
from azure_openai_cache_benchmark.serialization import (
    canonical_json,
    sha256_text,
)
from azure_openai_cache_benchmark.suite.plan import build_suite
from azure_openai_cache_benchmark.suite.qualification import (
    build_isolation_probe,
)


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
            list(dict.fromkeys(spec.experiment for spec in self.specs)),
        )

    def test_optimized_cohort_is_exactly_44_warm_requests(self) -> None:
        optimized = [spec for spec in self.specs if spec.optimized_cohort]
        self.assertEqual(OPTIMIZED_COHORT_EXPECTED_REQUESTS, len(optimized))
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

    def test_matched_pairs_are_unique_adjacent_and_identical(self) -> None:
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
            self.assertEqual(("cold", "warm"), (
                cold.expected_cache_state,
                warm.expected_cache_state,
            ))
            self.assertEqual(cold.order + 1, warm.order)
            self.assertEqual(
                cold.request_payload_fingerprint(),
                warm.request_payload_fingerprint(),
            )
            self.assertEqual(MATCHED_MAX_OUTPUT_TOKENS, cold.max_output_tokens)
            self.assertIsNone(cold.batch_id)
            self.assertIsNone(warm.batch_id)
            namespaces.add(cold.namespace)
            keys.add(cold.prompt_cache_key)
        self.assertEqual(MATCHED_PAIR_COUNT, len(namespaces))
        self.assertEqual(MATCHED_PAIR_COUNT, len(keys))

    def test_stable_prefix_varies_only_after_boundary(self) -> None:
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

    def test_fixed_run_manifest_hashes(self) -> None:
        suite = build_suite(
            run_id="architecture-characterization",
            asset=self.asset,
            idle_gap_seconds=0,
            paced_interval_seconds=0,
        )
        probe = build_isolation_probe(
            run_id="architecture-characterization",
            asset=self.asset,
        )
        self.assertEqual(
            "4de65a3b699d15221e161099280104920136bc66e9d4704ad38f9d2095b5324d",
            sha256_text(canonical_json([spec.to_manifest() for spec in suite])),
        )
        self.assertEqual(
            "2ba5170b75635b6ea4363713295d7181e997ed9e651ea7731565e235fcd02f2e",
            sha256_text(canonical_json([spec.to_manifest() for spec in probe])),
        )

    def test_isolation_probe_has_identical_a_payload(self) -> None:
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
