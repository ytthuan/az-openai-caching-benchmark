from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Mapping

from .constants import (
    DEFAULT_PROMPT_LOGICAL_PATH,
    DYNAMIC_END,
    DYNAMIC_START,
    MIN_CACHEABLE_TOKENS,
    MODEL_DEFAULT,
)
from .errors import ValidationError
from .serialization import count_words, safe_slug, sha256_text
from .tokens import get_encoding


@dataclass(frozen=True)
class PromptAsset:
    path: str
    template: str
    sha256: str
    word_count: int
    estimated_tokens: int
    encoding: str


def load_prompt_asset(
    path: Path | None = None,
    model: str = MODEL_DEFAULT,
) -> PromptAsset:
    if path is None:
        template = (
            resources.files("azure_openai_cache_benchmark")
            .joinpath(DEFAULT_PROMPT_LOGICAL_PATH)
            .read_text(encoding="utf-8")
        )
        safe_path = DEFAULT_PROMPT_LOGICAL_PATH
    else:
        template = path.read_text(encoding="utf-8")
        safe_path = path.name
    encoding, encoding_name = get_encoding(model)
    return PromptAsset(
        path=safe_path,
        template=template,
        sha256=sha256_text(template),
        word_count=count_words(template),
        estimated_tokens=len(encoding.encode(template)),
        encoding=encoding_name,
    )


def validate_prompt_asset(asset: PromptAsset) -> list[str]:
    errors: list[str] = []
    if not 4_000 <= asset.word_count <= 5_000:
        errors.append(
            f"Prompt word count {asset.word_count} is outside 4,000-5,000."
        )
    if asset.template.count(DYNAMIC_START) != 1:
        errors.append("Prompt must contain exactly one dynamic start marker.")
    if not asset.template.rstrip().endswith(DYNAMIC_END):
        errors.append("Dynamic section must be the final prompt section.")
    if asset.estimated_tokens < MIN_CACHEABLE_TOKENS:
        errors.append("Prompt is below the prompt-cache token threshold.")
    return errors


def split_prompt_boundary(rendered_prompt: str) -> tuple[str, str]:
    if DYNAMIC_START not in rendered_prompt:
        raise ValidationError("Prompt dynamic marker is missing.")
    stable, dynamic = rendered_prompt.split(DYNAMIC_START, maxsplit=1)
    return stable, DYNAMIC_START + dynamic


def _replace_dynamic_section(
    template: str,
    replacements: Mapping[str, str],
) -> str:
    stable, dynamic_tail = split_prompt_boundary(template)
    for key, replacement in replacements.items():
        dynamic_tail = dynamic_tail.replace("{{" + key + "}}", replacement)
    unresolved = sorted(
        set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", dynamic_tail))
    )
    if unresolved:
        raise ValidationError(
            f"Dynamic prompt values are unresolved: {unresolved}"
        )
    return stable + dynamic_tail


def render_system_prompt(
    asset: PromptAsset,
    *,
    namespace: str,
    case_id: str,
    timestamp_utc: str = "2026-01-01T00:00:00Z",
    session_id: str = "SESSION-GIA-LAP-001",
    early_metadata: str | None = None,
) -> str:
    rendered = _replace_dynamic_section(
        asset.template,
        {
            "REQUEST_TIMESTAMP_UTC": timestamp_utc,
            "SESSION_ID": session_id,
            "USER_ID": "VIEWER-GIA-LAP-DEFAULT",
            "USER_ROLES": '["viewer"]',
            "WORKSPACE_ID": "WS-GIA-LAP-01",
            "IDEMPOTENCY_KEY": f"IDEM-{safe_slug(case_id).upper()}",
            "EVAL_BUDGET_REMAINING": "1000 synthetic units",
            "BENCHMARK_NAMESPACE": namespace,
            "BENCHMARK_CASE_ID": case_id,
            "PROMPT_SHA256": asset.sha256,
            "PROMPT_WORD_COUNT": str(asset.word_count),
            "EVIDENCE_BLOCK": (
                "[EVD-CACHE-001] Dữ liệu synthetic: marker hợp lệ là "
                "CACHE_BENCHMARK_OK."
            ),
            "RETRIEVED_CONTENT": "Không có nội dung truy xuất.",
        },
    )
    header = (
        f"[BENCHMARK_NAMESPACE={namespace}; POLICY_EFFECT=NONE]\n"
        "Siêu dữ liệu này chỉ cô lập phép đo prompt cache.\n"
    )
    if early_metadata:
        header += f"[VOLATILE_REQUEST_METADATA={early_metadata}]\n"
    return header + rendered
