from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import OpenAI

from azure_openai_cache_benchmark.errors import BudgetExhausted
from azure_openai_cache_benchmark.output.artifacts import JsonlWriter
from azure_openai_cache_benchmark.pricing.retail import override_price_book
from azure_openai_cache_benchmark.runtime.budget import AttemptBudget
from azure_openai_cache_benchmark.runtime.runner import ResponseRunner
from azure_openai_cache_benchmark.schemas import (
    build_tools,
    response_json_schema,
)

from tests.helpers import (
    DumpingEvent,
    FakeEvent,
    FakeResponses,
    unit_spec,
)


class RuntimeTests(unittest.TestCase):
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

    def test_terminal_completed_event_is_only_usage_source(self) -> None:
        fake_responses = FakeResponses()
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

    def test_installed_sdk_serializes_cache_contract(self) -> None:
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

        client = OpenAI(
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
                    client=client,
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
            client.close()
        self.assertEqual("completed", record["status"])
        body = captured["body"]
        self.assertEqual("cache-key", body["prompt_cache_key"])
        self.assertEqual("none", body["tool_choice"])
        self.assertEqual("json_schema", body["text"]["format"]["type"])
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])
