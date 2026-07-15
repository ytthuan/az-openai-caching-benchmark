from __future__ import annotations

import unittest
from unittest import mock

from azure_openai_cache_benchmark.errors import PricingError
from azure_openai_cache_benchmark.pricing.costs import (
    compute_cost,
    expected_cost_envelope,
)
from azure_openai_cache_benchmark.pricing.models import PricingOptions
from azure_openai_cache_benchmark.pricing.retail import (
    override_price_book,
    resolve_price_book,
    select_global_standard_price_book,
)

from tests.helpers import unit_spec


class PricingTests(unittest.TestCase):
    def test_cost_formulas_and_savings_percentages(self) -> None:
        price_book = override_price_book(
            input_price=1.0,
            cached_input_price=0.1,
            output_price=5.0,
        )
        cost = compute_cost(
            {
                "input_tokens": 1_000,
                "cached_tokens": 800,
                "uncached_input_tokens": 200,
                "output_tokens": 100,
            },
            price_book,
        )
        self.assertAlmostEqual(0.0002, cost["uncached_input_usd"])
        self.assertAlmostEqual(0.00008, cost["cached_input_usd"])
        self.assertAlmostEqual(0.001, cost["no_cache_input_usd"])
        self.assertAlmostEqual(0.72, cost["input_savings_rate"])
        self.assertAlmostEqual(
            cost["savings_usd"] / cost["no_cache_usd"],
            cost["total_savings_rate"],
        )

    def test_cost_envelope_reserves_probe_and_retry_budget(self) -> None:
        envelope = expected_cost_envelope(
            [unit_spec(max_output_tokens=100)],
            override_price_book(
                input_price=0.75,
                cached_input_price=0.075,
                output_price=4.5,
            ),
            "gpt-5.4-mini",
            120,
        )
        self.assertIsNotNone(envelope)
        self.assertEqual(119, envelope["reserve_attempts"])
        self.assertEqual(3, envelope["capability_probe_attempts"])
        self.assertEqual(116, envelope["retry_reserve_attempts"])

    def test_override_resolution_never_calls_network(self) -> None:
        options = PricingOptions(
            input_price=0.75,
            cached_input_price=0.075,
            output_price=4.5,
        )
        with mock.patch(
            "azure_openai_cache_benchmark.pricing.retail.fetch_retail_price_items",
            side_effect=AssertionError("network call forbidden"),
        ):
            price_book = resolve_price_book(options)
        self.assertEqual("cli_override", price_book.source)
        with self.assertRaises(PricingError):
            resolve_price_book(PricingOptions(input_price=1.0))

    def test_retail_selection_uses_latest_consistent_meters(self) -> None:
        rows = []
        for sku, price in (
            ("5.4 mini Inp Gl", 0.75),
            ("5.4 mini cd Inp Gl", 0.075),
            ("5.4 mini Opt Gl", 4.5),
        ):
            rows.append(
                {
                    "productName": "Azure OpenAI GPT5",
                    "skuName": sku,
                    "unitOfMeasure": "1M",
                    "type": "Consumption",
                    "effectiveStartDate": "2026-07-01",
                    "retailPrice": price,
                    "currencyCode": "USD",
                    "meterName": sku,
                    "meterId": sku,
                }
            )
        price_book = select_global_standard_price_book(
            rows,
            retrieved_at="2026-07-15T00:00:00Z",
        )
        self.assertEqual(0.75, price_book.input_usd_per_million)
        self.assertEqual(0.075, price_book.cached_input_usd_per_million)
        self.assertEqual(4.5, price_book.output_usd_per_million)
