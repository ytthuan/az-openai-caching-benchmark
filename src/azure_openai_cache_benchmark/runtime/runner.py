from __future__ import annotations

import threading
import time
from typing import Any, Mapping, Protocol

from ..pricing.models import PriceBook
from ..specs import RequestSpec
from .budget import AttemptBudget
from .events import is_retryable
from .streaming import execute_once


class RecordWriter(Protocol):
    def write(self, value: Mapping[str, Any]) -> None: ...


class ResponseRunner:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        base_url: str,
        api_key: str,
        budget: AttemptBudget,
        writer: RecordWriter,
        price_book: PriceBook | None,
        max_retries: int,
    ) -> None:
        self.client = client
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.budget = budget
        self.writer = writer
        self.price_book = price_book
        self.max_retries = max_retries
        self.records: list[dict[str, Any]] = []
        self._records_lock = threading.Lock()
        self._last_arm_start: dict[tuple[str, str], float] = {}
        self._arm_lock = threading.Lock()

    def _gap_seconds(self, spec: RequestSpec, now: float) -> float | None:
        key = (spec.experiment, spec.arm)
        with self._arm_lock:
            previous = self._last_arm_start.get(key)
            self._last_arm_start[key] = now
        return now - previous if previous is not None else None

    def _store_record(self, record: dict[str, Any]) -> None:
        with self._records_lock:
            self.records.append(record)
        self.writer.write(record)

    def run_spec(
        self,
        spec: RequestSpec,
        *,
        allow_retry: bool,
    ) -> dict[str, Any]:
        retry_limit = self.max_retries if allow_retry else 0
        final_record: dict[str, Any] | None = None
        for retry_index in range(retry_limit + 1):
            record = execute_once(
                client=self.client,
                model=self.model,
                base_url=self.base_url,
                api_key=self.api_key,
                budget=self.budget,
                price_book=self.price_book,
                spec=spec,
                retry_index=retry_index,
                gap_seconds_for=lambda now: self._gap_seconds(spec, now),
            )
            self._store_record(record)
            final_record = record
            if record["status"] == "completed":
                return record
            if retry_index >= retry_limit or not is_retryable(record):
                break
            retry_after = (
                (record.get("error") or {}).get("retry_after_seconds")
            )
            delay = (
                float(retry_after)
                if retry_after is not None
                else min(8.0, 2.0**retry_index)
            )
            time.sleep(delay)
        assert final_record is not None
        return final_record
