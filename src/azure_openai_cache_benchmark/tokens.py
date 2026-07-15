from __future__ import annotations

from typing import Any

from .constants import MODEL_DEFAULT
from .errors import ConfigurationError


def get_encoding(model: str = MODEL_DEFAULT) -> tuple[Any, str]:
    try:
        import tiktoken
    except ImportError as exc:
        raise ConfigurationError(
            "tiktoken is required. Install the package dependencies."
        ) from exc
    try:
        return tiktoken.encoding_for_model(model), f"model:{model}"
    except KeyError:
        return tiktoken.get_encoding("o200k_base"), "fallback:o200k_base"


def estimate_tokens(value: str, model: str = MODEL_DEFAULT) -> int:
    encoding, _ = get_encoding(model)
    return len(encoding.encode(value))


def truncate_to_tokens(
    value: str,
    token_limit: int,
    model: str = MODEL_DEFAULT,
) -> str:
    encoding, _ = get_encoding(model)
    return encoding.decode(encoding.encode(value)[:token_limit])
