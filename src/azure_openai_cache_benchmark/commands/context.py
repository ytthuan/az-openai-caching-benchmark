from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..pricing.models import PricingOptions
from ..prompt_assets import PromptAsset, load_prompt_asset, validate_prompt_asset
from ..specs import RequestSpec
from ..suite.plan import build_suite
from ..errors import ValidationError


@dataclass(frozen=True)
class CommonOptions:
    model: str
    run_id: str
    output_root: Path
    env_file: Path
    max_attempts: int
    idle_gap_seconds: float
    paced_interval_seconds: float
    pricing: PricingOptions


@dataclass(frozen=True)
class LiveOptions:
    common: CommonOptions
    confirm_live: bool
    timeout: float
    max_retries: int


@dataclass(frozen=True)
class ReportOptions:
    run_dir: Path


@dataclass(frozen=True)
class SampleOptions:
    summary: Path
    output_dir: Path


def new_run_id(prefix: str) -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{prefix}-{timestamp}-{uuid.uuid4().hex[:8]}"


def create_run_dir(output_root: Path, run_id: str) -> Path:
    run_dir = output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def load_benchmark_context(
    options: CommonOptions,
) -> tuple[PromptAsset, list[RequestSpec]]:
    asset = load_prompt_asset(model=options.model)
    errors = validate_prompt_asset(asset)
    if errors:
        raise ValidationError("; ".join(errors))
    specs = build_suite(
        run_id=options.run_id,
        asset=asset,
        model=options.model,
        idle_gap_seconds=options.idle_gap_seconds,
        paced_interval_seconds=options.paced_interval_seconds,
    )
    return asset, specs
