from __future__ import annotations

import csv
import json
import threading
from pathlib import Path
from typing import Any, Mapping

from ..errors import ConfigurationError


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, value: Mapping[str, Any]) -> None:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
                handle.flush()


def read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    line_number = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ConfigurationError(
                        f"{path}:{line_number} is not an object."
                    )
                records.append(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Invalid JSON in {path}:{line_number}: {exc.msg}"
        ) from exc
    return records


def write_summary_csv(
    path: Path,
    summary: Mapping[str, Any],
) -> None:
    fields = [
        "experiment_arm",
        "logical_requests",
        "completed",
        "failed",
        "request_hit_rate",
        "substantive_request_hit_rate",
        "token_weighted_cache_rate",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "first_text_p50_ms",
        "ttlt_p50_ms",
        "actual_cost_usd",
        "no_cache_cost_usd",
        "input_savings_rate",
        "total_savings_rate",
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for name, metric in (summary.get("arms") or {}).items():
            usage = metric.get("usage") or {}
            timing = metric.get("timing") or {}
            cost = metric.get("cost") or {}
            writer.writerow(
                {
                    "experiment_arm": name,
                    "logical_requests": metric.get("logical_requests"),
                    "completed": metric.get("completed"),
                    "failed": metric.get("failed"),
                    "request_hit_rate": metric.get("request_hit_rate"),
                    "substantive_request_hit_rate": metric.get(
                        "substantive_request_hit_rate"
                    ),
                    "token_weighted_cache_rate": metric.get(
                        "token_weighted_cache_rate"
                    ),
                    "input_tokens": usage.get("input_tokens"),
                    "cached_tokens": usage.get("cached_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "first_text_p50_ms": (
                        timing.get("first_text_ms") or {}
                    ).get("p50"),
                    "ttlt_p50_ms": (
                        timing.get("ttlt_ms") or {}
                    ).get("p50"),
                    "actual_cost_usd": cost.get("actual_usd"),
                    "no_cache_cost_usd": cost.get("no_cache_usd"),
                    "input_savings_rate": cost.get("input_savings_rate"),
                    "total_savings_rate": cost.get("total_savings_rate"),
                }
            )
    temporary.replace(path)
