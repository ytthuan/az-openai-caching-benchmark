from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .commands.context import (
    CommonOptions,
    LiveOptions,
    ReportOptions,
    SampleOptions,
    new_run_id,
)
from .commands.dry_run import run_dry
from .commands.live import run_live
from .commands.reports import rerender_report, run_sample
from .constants import (
    MAX_ATTEMPTS_DEFAULT,
    MIN_REQUIRED_ATTEMPTS,
    MODEL_DEFAULT,
)
from .errors import BenchmarkError
from .pricing.models import PricingOptions
from .serialization import safe_slug


def _add_common_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_base: Path,
) -> None:
    parser.add_argument("--model", default=MODEL_DEFAULT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=default_base / "runs",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=default_base / ".env",
        help=(
            "Dotenv file for two allowlisted variables. Existing process "
            "values take precedence."
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=MAX_ATTEMPTS_DEFAULT,
    )
    parser.add_argument(
        "--idle-gap-seconds",
        type=float,
        default=660.0,
    )
    parser.add_argument(
        "--paced-interval-seconds",
        type=float,
        default=4.2,
    )
    parser.add_argument("--skip-pricing", action="store_true")
    parser.add_argument("--pricing-timeout", type=float, default=30.0)
    parser.add_argument("--input-price", type=float)
    parser.add_argument("--cached-input-price", type=float)
    parser.add_argument("--output-price", type=float)


def build_parser(
    default_base: Path | None = None,
) -> argparse.ArgumentParser:
    base = default_base if default_base is not None else Path.cwd()
    parser = argparse.ArgumentParser(
        description="Standalone Azure OpenAI prompt-cache benchmark."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    dry = subparsers.add_parser(
        "dry-run",
        help="Validate suite and cost ceiling without model calls.",
    )
    _add_common_arguments(dry, default_base=base)
    live = subparsers.add_parser(
        "live",
        help="Run capped benchmark against configured endpoint.",
    )
    _add_common_arguments(live, default_base=base)
    live.add_argument("--confirm-live", action="store_true")
    live.add_argument("--timeout", type=float, default=180.0)
    live.add_argument("--max-retries", type=int, default=1)
    report = subparsers.add_parser(
        "report",
        help="Rebuild derived files from manifest.json and requests.jsonl.",
    )
    report.add_argument("run_dir", type=Path)
    sample = subparsers.add_parser(
        "sample",
        help="Write allowlisted sample files from a live summary.",
    )
    sample.add_argument("summary", type=Path)
    sample.add_argument(
        "--output-dir",
        type=Path,
        default=base / "examples",
    )
    return parser


def _validate_common(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.run_id is None:
        args.run_id = new_run_id(
            "dry" if args.command == "dry-run" else "live"
        )
    elif safe_slug(args.run_id) != args.run_id:
        parser.error(
            "--run-id may contain only letters, numbers, underscores "
            "and hyphens."
        )
    if args.idle_gap_seconds < 0:
        parser.error("--idle-gap-seconds cannot be negative.")
    if args.paced_interval_seconds < 0:
        parser.error("--paced-interval-seconds cannot be negative.")
    if args.pricing_timeout <= 0:
        parser.error("--pricing-timeout must be positive.")
    if not MIN_REQUIRED_ATTEMPTS <= args.max_attempts <= MAX_ATTEMPTS_DEFAULT:
        parser.error(
            f"--max-attempts must be between {MIN_REQUIRED_ATTEMPTS} "
            f"and {MAX_ATTEMPTS_DEFAULT}."
        )
    if args.command == "live":
        if args.timeout <= 0:
            parser.error("--timeout must be positive.")
        if args.max_retries < 0:
            parser.error("--max-retries cannot be negative.")


def _common_options(args: argparse.Namespace) -> CommonOptions:
    pricing = PricingOptions(
        skip=args.skip_pricing,
        timeout_seconds=args.pricing_timeout,
        input_price=args.input_price,
        cached_input_price=args.cached_input_price,
        output_price=args.output_price,
    )
    return CommonOptions(
        model=args.model,
        run_id=args.run_id,
        output_root=args.output_root,
        env_file=args.env_file,
        max_attempts=args.max_attempts,
        idle_gap_seconds=args.idle_gap_seconds,
        paced_interval_seconds=args.paced_interval_seconds,
        pricing=pricing,
    )


def main(
    argv: Sequence[str] | None = None,
    default_base: Path | None = None,
) -> int:
    parser = build_parser(default_base)
    args = parser.parse_args(argv)
    if args.command in {"dry-run", "live"}:
        _validate_common(parser, args)
    try:
        if args.command == "dry-run":
            return run_dry(_common_options(args))
        if args.command == "live":
            return run_live(
                LiveOptions(
                    common=_common_options(args),
                    confirm_live=args.confirm_live,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                )
            )
        if args.command == "report":
            return rerender_report(ReportOptions(run_dir=args.run_dir))
        return run_sample(
            SampleOptions(
                summary=args.summary,
                output_dir=args.output_dir,
            )
        )
    except (
        BenchmarkError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
