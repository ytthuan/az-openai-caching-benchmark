from __future__ import annotations

from ..constants import (
    OPTIMIZED_COHORT_EXPECTED_REQUESTS,
    PROBE_ATTEMPTS,
)
from ..output.artifacts import atomic_write_json
from ..output.manifest import manifest_payload
from ..pricing.costs import expected_cost_envelope
from ..pricing.retail import resolve_price_book
from .context import CommonOptions, create_run_dir, load_benchmark_context


def run_dry(options: CommonOptions) -> int:
    asset, specs = load_benchmark_context(options)
    price_book = resolve_price_book(options.pricing)
    run_dir = create_run_dir(options.output_root, options.run_id)
    manifest = manifest_payload(
        run_id=options.run_id,
        model=options.model,
        asset=asset,
        specs=specs,
        max_attempts=options.max_attempts,
        price_book=price_book,
    )
    manifest["mode"] = "dry-run"
    manifest["validation"] = {
        "status": "passed",
        "planned_requests": len(specs),
        "optimized_expected_requests": (
            OPTIMIZED_COHORT_EXPECTED_REQUESTS
        ),
        "probe_requests": PROBE_ATTEMPTS,
    }
    manifest["expected_cost_envelope"] = expected_cost_envelope(
        specs,
        price_book,
        options.model,
        options.max_attempts,
    )
    atomic_write_json(run_dir / "manifest.json", manifest)
    print(f"Dry-run valid: {len(specs)} planned requests.")
    print(
        f"Prompt: {asset.word_count} words, "
        f"{asset.estimated_tokens} estimated tokens, SHA-256 {asset.sha256}."
    )
    envelope = manifest["expected_cost_envelope"]
    if envelope:
        print(
            "Conservative no-cache envelope: "
            f"${envelope['conservative_total_usd']:.6f}."
        )
    else:
        print("Conservative no-cache envelope: n/a (pricing skipped).")
    print(f"Manifest: {run_dir / 'manifest.json'}")
    return 0
