from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from azure_openai_cache_benchmark.analysis.summary import build_summary
from azure_openai_cache_benchmark.commands.context import ReportOptions
from azure_openai_cache_benchmark.commands.reports import (
    rerender_report,
    write_sanitized_examples,
)
from azure_openai_cache_benchmark.constants import (
    DYNAMIC_START,
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    PLANNED_ATTEMPTS,
    SAMPLE_SUMMARY_ALLOWLIST,
)
from azure_openai_cache_benchmark.output.manifest import manifest_payload
from azure_openai_cache_benchmark.output.report import render_markdown_report
from azure_openai_cache_benchmark.output.sanitizer import sanitize_summary
from azure_openai_cache_benchmark.pricing.retail import override_price_book
from azure_openai_cache_benchmark.prompt_assets import load_prompt_asset
from azure_openai_cache_benchmark.suite.plan import build_suite

from tests.helpers import ROOT, completed_record


class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.asset = load_prompt_asset()
        cls.specs = build_suite(
            run_id="output-unit",
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
            run_id="output-unit",
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

    def test_report_has_required_guides_and_dynamic_counts(self) -> None:
        report = render_markdown_report(self.build_full_summary())
        self.assertIn("## Cách benchmark cache efficiency", report)
        self.assertIn("## Cách triển khai cache với system prompt", report)
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

    def test_tracked_sample_report_matches_renderer_exactly(self) -> None:
        summary = json.loads(
            (ROOT / "examples" / "sample-summary.json").read_text(
                encoding="utf-8"
            )
        )
        expected = render_markdown_report(summary, sanitized_sample=True)
        self.assertEqual(
            expected,
            (ROOT / "examples" / "sample-report.md").read_text(
                encoding="utf-8"
            ),
        )

    def test_sample_writer_emits_allowlisted_view(self) -> None:
        source = {
            "schema_version": "2.0.0",
            "run_id": "live-sample-unit",
            "model": "gpt-5.4-mini",
            "generated_at": "2026-07-14T10:00:00Z",
            "run_status": "completed",
            "suite": {},
            "records": {
                "attempt_records": 105,
                "attempt_budget": {
                    "claims": [{"secret": "do-not-copy"}]
                },
            },
            "overall": {},
            "optimized": {},
            "degraded": {},
            "arms": {},
            "root_causes": [],
            "matched_latency": {},
            "acceptance": {},
            "pricing": None,
            "prompt": {},
            "provenance": {},
            "endpoint": {"host": "private.example.com"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "summary.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            summary_path, report_path = write_sanitized_examples(
                source_path,
                root / "examples",
            )
            serialized = summary_path.read_text(encoding="utf-8")
            report = report_path.read_text(encoding="utf-8")
        self.assertNotIn("do-not-copy", serialized)
        self.assertNotIn("private.example.com", serialized)
        self.assertIn('"sanitized": true', serialized)
        self.assertIn("Mẫu đã khử nhạy cảm", report)

    def test_report_rebuild_is_offline_and_deterministic(self) -> None:
        record = completed_record(self.specs[0])
        manifest = {
            "run_id": "rebuild-unit",
            "model": "gpt-5.4-mini",
            "pricing": self.price_book.to_dict(),
            "prompt": {
                "path": self.asset.path,
                "sha256": self.asset.sha256,
                "word_count": self.asset.word_count,
                "estimated_tokens": self.asset.estimated_tokens,
                "encoding": self.asset.encoding,
                "dynamic_start_marker": DYNAMIC_START,
            },
            "execution": {
                "status": "completed",
                "completed_at": "2026-07-14T10:00:00Z",
                "attempt_budget": {
                    "maximum": 120,
                    "used": 1,
                    "remaining": 119,
                    "claims": [],
                },
                "fatal_error": None,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            (run_dir / "requests.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )
            with mock.patch(
                "azure_openai_cache_benchmark.pricing.retail.fetch_retail_price_items",
                side_effect=AssertionError("network call forbidden"),
            ):
                self.assertEqual(
                    0,
                    rerender_report(ReportOptions(run_dir=run_dir)),
                )
            first_summary = (run_dir / "summary.json").read_bytes()
            first_report = (run_dir / "report.md").read_bytes()
            self.assertEqual(
                0,
                rerender_report(ReportOptions(run_dir=run_dir)),
            )
            self.assertEqual(
                first_summary,
                (run_dir / "summary.json").read_bytes(),
            )
            self.assertEqual(
                first_report,
                (run_dir / "report.md").read_bytes(),
            )
