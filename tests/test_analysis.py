from __future__ import annotations

import unittest

from azure_openai_cache_benchmark.analysis.metrics import latest_records
from azure_openai_cache_benchmark.analysis.summary import build_summary
from azure_openai_cache_benchmark.constants import (
    DYNAMIC_START,
    MATCHED_PAIR_COUNT,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
)
from azure_openai_cache_benchmark.pricing.retail import override_price_book
from azure_openai_cache_benchmark.prompt_assets import load_prompt_asset
from azure_openai_cache_benchmark.suite.plan import build_suite
from azure_openai_cache_benchmark.usage import usage_from_response_payload

from tests.helpers import completed_record


class AnalysisTests(unittest.TestCase):
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

    def build_full_summary(self, *, priced: bool = True):
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

    def test_latest_attempt_wins_per_logical_request(self) -> None:
        first = completed_record(self.specs[0], attempt_number=1)
        retry = {
            **first,
            "attempt_number": 2,
            "status": "failed",
        }
        self.assertEqual([retry], latest_records([first, retry]))

    def test_terminal_usage_parsing_keeps_reasoning_within_output(self) -> None:
        usage = usage_from_response_payload(
            {
                "usage": {
                    "input_tokens": 2_000,
                    "input_tokens_details": {"cached_tokens": 1_800},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 10},
                    "total_tokens": 2_020,
                }
            }
        )
        self.assertEqual(200, usage["uncached_input_tokens"])
        self.assertEqual(10, usage["reasoning_tokens"])
        self.assertEqual(20, usage["output_tokens"])
