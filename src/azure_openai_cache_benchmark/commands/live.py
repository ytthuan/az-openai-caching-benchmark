from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

from ..analysis.summary import build_summary
from ..config import (
    endpoint_fingerprint,
    load_allowed_env_file,
    redact_text,
    resolve_endpoint_env,
)
from ..errors import (
    BenchmarkError,
    BudgetExhausted,
    ConfigurationError,
)
from ..output.artifacts import (
    JsonlWriter,
    atomic_write_json,
    atomic_write_text,
    write_summary_csv,
)
from ..output.manifest import manifest_payload
from ..output.report import render_markdown_report
from ..pricing.costs import expected_cost_envelope
from ..pricing.models import PriceBook
from ..pricing.retail import resolve_price_book
from ..runtime.budget import AttemptBudget
from ..runtime.events import OpenAIError
from ..runtime.execution import execute_plan, validate_capability_probe
from ..runtime.runner import ResponseRunner
from ..serialization import utc_now_iso
from ..suite.qualification import build_isolation_probe
from .context import (
    LiveOptions,
    create_run_dir,
    load_benchmark_context,
)


def _finalize_live_artifacts(
    *,
    run_dir,
    manifest: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    price_book: PriceBook | None,
    budget: AttemptBudget,
    run_status: str,
    fatal_error: str | None,
) -> dict[str, Any]:
    completed_at = utc_now_iso()
    snapshot = budget.snapshot()
    manifest["execution"] = {
        "status": run_status,
        "completed_at": completed_at,
        "attempt_budget": snapshot,
        "fatal_error": fatal_error,
    }
    atomic_write_json(run_dir / "manifest.json", manifest)
    summary = build_summary(
        run_id=str(manifest["run_id"]),
        model=str(manifest["model"]),
        records=records,
        price_book=price_book,
        attempt_budget=snapshot,
        run_status=run_status,
        completed_at=completed_at,
        fatal_error=fatal_error,
        prompt_metadata=manifest.get("prompt"),
    )
    atomic_write_json(run_dir / "summary.json", summary)
    write_summary_csv(run_dir / "summary.csv", summary)
    atomic_write_text(
        run_dir / "report.md",
        render_markdown_report(summary),
    )
    return summary


def _has_final_failures(records: Sequence[Mapping[str, Any]]) -> bool:
    latest: dict[str, Mapping[str, Any]] = {}
    for record in records:
        logical_id = str(record.get("logical_request_id") or "")
        if logical_id:
            latest[logical_id] = record
    return any(record.get("status") != "completed" for record in latest.values())


def run_live(options: LiveOptions) -> int:
    common = options.common
    if not options.confirm_live:
        raise ConfigurationError(
            "Live mode requires --confirm-live after reviewing dry-run cost."
        )
    load_info = load_allowed_env_file(common.env_file, os.environ)
    base_url, endpoint_env = resolve_endpoint_env(os.environ)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigurationError("OPENAI_API_KEY is required for live mode.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ConfigurationError(
            "openai is required. Install the package dependencies."
        ) from exc

    asset, specs = load_benchmark_context(common)
    price_book = resolve_price_book(common.pricing)
    run_dir = create_run_dir(common.output_root, common.run_id)
    manifest = manifest_payload(
        run_id=common.run_id,
        model=common.model,
        asset=asset,
        specs=specs,
        max_attempts=common.max_attempts,
        endpoint=endpoint_fingerprint(base_url),
        price_book=price_book,
    )
    manifest["mode"] = "live"
    manifest["endpoint_env"] = endpoint_env
    manifest["env_file"] = load_info
    manifest["expected_cost_envelope"] = expected_cost_envelope(
        specs,
        price_book,
        common.model,
        common.max_attempts,
    )
    atomic_write_json(run_dir / "manifest.json", manifest)

    writer = JsonlWriter(run_dir / "requests.jsonl")
    budget = AttemptBudget(common.max_attempts)
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=options.timeout,
        max_retries=0,
    )
    runner = ResponseRunner(
        client=client,
        model=common.model,
        base_url=base_url,
        api_key=api_key,
        budget=budget,
        writer=writer,
        price_book=price_book,
        max_retries=options.max_retries,
    )
    run_status = "completed"
    fatal_error: str | None = None
    try:
        probe_specs = build_isolation_probe(
            run_id=common.run_id,
            asset=asset,
            model=common.model,
        )
        probe_records = [
            runner.run_spec(spec, allow_retry=False)
            for spec in probe_specs
        ]
        validate_capability_probe(probe_specs, probe_records)
        execute_plan(runner=runner, specs=specs)
        if _has_final_failures(runner.records):
            run_status = "completed_with_findings"
    except KeyboardInterrupt:
        run_status = "incomplete"
        fatal_error = "KeyboardInterrupt: interrupted by operator"
    except (
        BudgetExhausted,
        BenchmarkError,
        OpenAIError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        run_status = "incomplete"
        fatal_error = redact_text(
            f"{type(exc).__name__}: {exc}",
            secrets=(api_key,),
            base_url=base_url,
        )
    finally:
        client.close()

    summary = _finalize_live_artifacts(
        run_dir=run_dir,
        manifest=manifest,
        records=runner.records,
        price_book=price_book,
        budget=budget,
        run_status=run_status,
        fatal_error=fatal_error,
    )
    print(
        f"Run {run_status}: {budget.used}/{budget.maximum} HTTP attempts."
    )
    print(
        "Optimized cache: "
        f"{summary['optimized']['token_weighted_cache_rate']!r}; "
        f"acceptance: {summary['acceptance']['status']}."
    )
    print(f"Report: {run_dir / 'report.md'}")
    return 0 if run_status != "incomplete" else 2
