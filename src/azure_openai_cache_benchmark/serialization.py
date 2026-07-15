from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from .errors import ValidationError


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def short_hash(value: str, length: int = 12) -> str:
    return sha256_text(value)[:length]


def safe_slug(value: str, max_length: int = 64) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")
    if not slug:
        raise ValidationError("Identifier does not contain safe characters.")
    return slug[:max_length]


def count_words(value: str) -> int:
    return len(re.findall(r"\S+", value))
