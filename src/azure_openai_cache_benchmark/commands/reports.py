from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from ..analysis.summary import build_summary
from ..errors import ConfigurationError
from ..output.artifacts import (
    atomic_write_json,
    atomic_write_text,
    read_records,
    write_summary_csv,
)
from ..output.report import render_markdown_report
from ..output.sanitizer import sanitize_summary
from ..pricing.models import PriceBook
from .context import ReportOptions, SampleOptions


def rerender_report(options: ReportOptions) -> int:
    run_dir = options.run_dir.resolve()
    manifest_path = run_dir / "manifest.json"
    requests_path = run_dir / "requests.jsonl"
    for path in (manifest_path, requests_path):
        if not path.is_file():
            raise ConfigurationError(f"Missing {path}.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = read_records(requests_path)
    pricing_payload = manifest.get("pricing")
    try:
        price_book = (
            PriceBook(**pricing_payload)
            if isinstance(pricing_payload, dict)
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"Invalid pricing payload in {manifest_path}: {exc}"
        ) from exc
    execution = manifest.get("execution") or {}
    attempt_budget = execution.get("attempt_budget")
    completed_at = execution.get("completed_at")
    if not isinstance(attempt_budget, Mapping) or not completed_at:
        raise ConfigurationError(
            "Manifest lacks execution attempt budget or completed_at."
        )
    summary = build_summary(
        run_id=str(manifest["run_id"]),
        model=str(manifest["model"]),
        records=records,
        price_book=price_book,
        attempt_budget=attempt_budget,
        run_status=str(execution.get("status") or "incomplete"),
        completed_at=str(completed_at),
        fatal_error=execution.get("fatal_error"),
        prompt_metadata=manifest.get("prompt"),
    )
    atomic_write_json(run_dir / "summary.json", summary)
    write_summary_csv(run_dir / "summary.csv", summary)
    atomic_write_text(
        run_dir / "report.md",
        render_markdown_report(summary),
    )
    print(f"Summary: {run_dir / 'summary.json'}")
    print(f"Report: {run_dir / 'report.md'}")
    return 0


def write_sanitized_examples(
    summary_path: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    sanitized = sanitize_summary(summary)
    summary_output = output_dir / "sample-summary.json"
    report_output = output_dir / "sample-report.md"
    atomic_write_json(summary_output, sanitized)
    atomic_write_text(
        report_output,
        render_markdown_report(sanitized, sanitized_sample=True),
    )
    return summary_output, report_output


def run_sample(options: SampleOptions) -> int:
    summary_path, report_path = write_sanitized_examples(
        options.summary,
        options.output_dir,
    )
    print(f"Sample summary: {summary_path}")
    print(f"Sample report: {report_path}")
    return 0
