from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark import (  # noqa: E402
    DEFAULT_ENV_FILE,
    JsonlWriter,
    ResponseRunner,
    expected_cost_envelope,
    load_allowed_env_file,
    rerender_report,
    write_sanitized_examples,
)
from benchmark_core import (  # noqa: E402
    AttemptBudget,
    RequestSpec,
    build_tools,
    override_price_book,
    response_json_schema,
)


class FakeEvent:
    def __init__(self, event_type: str, **values: object) -> None:
        self.type = event_type
        for key, value in values.items():
            setattr(self, key, value)


class DumpingEvent(FakeEvent):
    def __init__(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        super().__init__(event_type)
        self.payload = payload

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return {"type": self.type, **self.payload}


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        return self.payload


class FakeResponses:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    def create(self, **kwargs: object):
        self.kwargs = kwargs
        terminal = FakeResponse(
            {
                "id": "resp-unit",
                "status": "completed",
                "model": "gpt-5.4-mini",
                "usage": {
                    "input_tokens": 2_000,
                    "input_tokens_details": {"cached_tokens": 1_800},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 10},
                    "total_tokens": 2_020,
                },
                "output": [],
            }
        )
        return iter(
            (
                FakeEvent("response.created"),
                FakeEvent(
                    "response.output_text.delta",
                    delta="CACHE_BENCHMARK_OK",
                ),
                FakeEvent("response.completed", response=terminal),
            )
        )


def unit_spec(**overrides: object) -> RequestSpec:
    values: dict[str, object] = {
        "order": 1,
        "logical_request_id": "unit-request",
        "experiment": "unit",
        "arm": "warm",
        "namespace": "unit",
        "instructions": "x" * 4_000,
        "input_text": "synthetic",
        "prompt_cache_key": "cache-key",
        "expected_cache_state": "warm",
        "cacheable_prefix_tokens_estimate": 2_000,
    }
    values.update(overrides)
    return RequestSpec(**values)


class EnvironmentTests(unittest.TestCase):
    def test_default_env_file_is_repo_root(self) -> None:
        self.assertEqual(ROOT / ".env", DEFAULT_ENV_FILE)

    def test_dotenv_allowlist_process_precedence_and_baser_preference(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "\n".join(
                    (
                        "OPENAI_BASER_URL=https://preferred.openai.azure.com",
                        "OPENAI_BASE_URL=https://dotenv.openai.azure.com",
                        "OPENAI_API_KEY=dotenv-secret",
                        "UNRELATED_SECRET=must-not-load",
                    )
                ),
                encoding="utf-8",
            )
            environ = {
                "OPENAI_BASE_URL": "https://process.openai.azure.com",
                "OPENAI_API_KEY": "process-secret",
            }
            info = load_allowed_env_file(path, environ)
        self.assertEqual(
            "https://preferred.openai.azure.com",
            environ["OPENAI_BASER_URL"],
        )
        self.assertEqual(
            "https://process.openai.azure.com",
            environ["OPENAI_BASE_URL"],
        )
        self.assertEqual("process-secret", environ["OPENAI_API_KEY"])
        self.assertNotIn("UNRELATED_SECRET", environ)
        self.assertEqual(["OPENAI_BASER_URL"], info["loaded_keys"])
        serialized = json.dumps(info, sort_keys=True)
        self.assertNotIn("dotenv-secret", serialized)
        self.assertNotIn("process-secret", serialized)


class RunnerTests(unittest.TestCase):
    def test_terminal_completed_event_is_only_usage_source(self) -> None:
        fake_responses = FakeResponses()
        runner = None
        with tempfile.TemporaryDirectory() as directory:
            runner = ResponseRunner(
                client=SimpleNamespace(responses=fake_responses),
                model="gpt-5.4-mini",
                base_url="https://example.openai.azure.com/openai/v1/",
                api_key="secret",
                budget=AttemptBudget(),
                writer=JsonlWriter(Path(directory) / "requests.jsonl"),
                price_book=override_price_book(
                    input_price=0.75,
                    cached_input_price=0.075,
                    output_price=4.5,
                ),
                max_retries=0,
            )
            record = runner.run_spec(unit_spec(), allow_retry=False)
        self.assertEqual("completed", record["status"])
        self.assertEqual(1_800, record["usage"]["cached_tokens"])
        self.assertTrue(record["cache_state_valid"])
        self.assertEqual(1, runner.budget.used)
        self.assertIsNotNone(fake_responses.kwargs)
        self.assertTrue(fake_responses.kwargs["stream"])
        self.assertFalse(fake_responses.kwargs["store"])
        self.assertEqual(
            "cache-key",
            fake_responses.kwargs["prompt_cache_key"],
        )

    def test_stream_error_payload_is_recursively_redacted(self) -> None:
        secret = "unit-secret"
        host = "example.openai.azure.com"
        responses = SimpleNamespace(
            create=lambda **_: iter(
                (
                    DumpingEvent(
                        "response.failed",
                        {
                            "error": {
                                "message": (
                                    f"api_key={secret} at "
                                    f"https://{host}/openai/v1/"
                                )
                            }
                        },
                    ),
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            runner = ResponseRunner(
                client=SimpleNamespace(responses=responses),
                model="gpt-5.4-mini",
                base_url=f"https://{host}/openai/v1/",
                api_key=secret,
                budget=AttemptBudget(),
                writer=JsonlWriter(Path(directory) / "requests.jsonl"),
                price_book=None,
                max_retries=0,
            )
            record = runner.run_spec(unit_spec(), allow_retry=False)
        serialized = json.dumps(record, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(host, serialized)
        self.assertIn("[REDACTED_SECRET]", serialized)
        self.assertIn("[REDACTED_ENDPOINT]", serialized)

    def test_incomplete_stream_is_failed_without_success_fallback(self) -> None:
        responses = SimpleNamespace(
            create=lambda **_: iter((FakeEvent("response.incomplete"),))
        )
        with tempfile.TemporaryDirectory() as directory:
            runner = ResponseRunner(
                client=SimpleNamespace(responses=responses),
                model="gpt-5.4-mini",
                base_url="https://example.openai.azure.com/openai/v1/",
                api_key="unit",
                budget=AttemptBudget(),
                writer=JsonlWriter(Path(directory) / "requests.jsonl"),
                price_book=None,
                max_retries=1,
            )
            record = runner.run_spec(unit_spec(), allow_retry=True)
        self.assertEqual("failed", record["status"])
        self.assertEqual("StreamResponseError", record["error"]["type"])
        self.assertEqual(1, runner.budget.used)

    def test_installed_sdk_serializes_cache_tools_schema_and_streaming(
        self,
    ) -> None:
        captured: dict[str, object] = {}
        terminal_response = {
            "id": "resp-sdk-shape",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "instructions": "synthetic",
            "max_output_tokens": 256,
            "model": "gpt-5.4-mini",
            "output": [],
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": "low", "summary": None},
            "store": False,
            "temperature": None,
            "text": {"format": {"type": "text"}},
            "tool_choice": "none",
            "tools": [],
            "top_p": None,
            "truncation": "disabled",
            "usage": {
                "input_tokens": 2_000,
                "input_tokens_details": {"cached_tokens": 1_800},
                "output_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 2_000,
            },
            "user": None,
            "metadata": {},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            event = json.dumps(
                {
                    "type": "response.completed",
                    "response": terminal_response,
                }
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"event: response.completed\ndata: {event}\n\n",
            )

        sdk_client = OpenAI(
            api_key="unit",
            base_url="https://example.openai.azure.com/openai/v1/",
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler)
            ),
            max_retries=0,
        )
        namespace = "sdk-shape"
        spec = unit_spec(
            namespace=namespace,
            tools=build_tools(namespace),
            text_config=response_json_schema(namespace),
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                runner = ResponseRunner(
                    client=sdk_client,
                    model="gpt-5.4-mini",
                    base_url=(
                        "https://example.openai.azure.com/openai/v1/"
                    ),
                    api_key="unit",
                    budget=AttemptBudget(),
                    writer=JsonlWriter(
                        Path(directory) / "requests.jsonl"
                    ),
                    price_book=None,
                    max_retries=0,
                )
                record = runner.run_spec(spec, allow_retry=False)
        finally:
            sdk_client.close()
        self.assertEqual("completed", record["status"])
        body = captured["body"]
        self.assertIsInstance(body, dict)
        self.assertEqual("cache-key", body["prompt_cache_key"])
        self.assertEqual("none", body["tool_choice"])
        self.assertEqual("json_schema", body["text"]["format"]["type"])
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])


class ArtifactTests(unittest.TestCase):
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
        self.assertEqual(
            envelope["planned_estimated_input_tokens"]
            + envelope["reserve_input_tokens_ceiling"],
            envelope["estimated_input_tokens"],
        )

    def test_report_rebuild_uses_only_manifest_and_request_records(self) -> None:
        price_book = override_price_book(
            input_price=0.75,
            cached_input_price=0.075,
            output_price=4.5,
        )
        record = {
            "attempt_number": 1,
            "retry_index": 0,
            "status": "completed",
            "logical_request_id": "stable-1",
            "order": 1,
            "experiment": "prefix_stability",
            "arm": "stable-prefix",
            "optimized_cohort": True,
            "pair_id": None,
            "expected_cache_state": "warm",
            "cacheable_prefix_tokens_estimate": 2_000,
            "cacheable_prefix_efficiency": 0.9,
            "metadata": {"sample_index": 1},
            "usage": {
                "input_tokens": 2_100,
                "cached_tokens": 1_800,
                "uncached_input_tokens": 300,
                "output_tokens": 20,
                "reasoning_tokens": 5,
                "total_tokens": 2_120,
            },
            "timing": {
                "first_event_ms": 10.0,
                "first_text_ms": 20.0,
                "ttlt_ms": 100.0,
                "tbt_ms": 4.0,
            },
        }
        manifest = {
            "run_id": "rebuild-unit",
            "model": "gpt-5.4-mini",
            "pricing": price_book.to_dict(),
            "prompt": {
                "path": "prompts/system.md",
                "sha256": "template-hash",
                "word_count": 4_500,
                "estimated_tokens": 7_500,
                "encoding": "fallback:o200k_base",
                "dynamic_start_marker": "=== marker ===",
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
            (run_dir / "summary.json").write_text(
                '{"stale": true}\n',
                encoding="utf-8",
            )
            with mock.patch(
                "benchmark.fetch_retail_price_items",
                side_effect=AssertionError("network call forbidden"),
            ):
                self.assertEqual(
                    0,
                    rerender_report(SimpleNamespace(run_dir=run_dir)),
                )
            first_summary = (run_dir / "summary.json").read_bytes()
            first_report = (run_dir / "report.md").read_bytes()
            self.assertEqual(
                0,
                rerender_report(SimpleNamespace(run_dir=run_dir)),
            )
            self.assertEqual(
                first_summary,
                (run_dir / "summary.json").read_bytes(),
            )
            self.assertEqual(
                first_report,
                (run_dir / "report.md").read_bytes(),
            )
            summary = json.loads(first_summary)
            self.assertEqual("rebuild-unit", summary["run_id"])
            self.assertEqual(
                "2026-07-14T10:00:00Z",
                summary["generated_at"],
            )
            self.assertNotIn("stale", summary)
            self.assertTrue((run_dir / "summary.csv").exists())

    def test_sample_writer_emits_allowlisted_real_run_view(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
