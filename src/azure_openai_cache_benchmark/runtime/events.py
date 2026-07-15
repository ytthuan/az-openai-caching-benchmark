from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..config import redact_text
from ..errors import BenchmarkError, ValidationError

try:
    from openai import OpenAIError
except ImportError:
    class OpenAIError(Exception):
        pass


class StreamResponseError(BenchmarkError):
    def __init__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.event_type = event_type
        self.payload = dict(payload)
        super().__init__(
            f"Responses API stream terminated with {event_type}."
        )


REQUEST_ERRORS = (
    OpenAIError,
    StreamResponseError,
    ValidationError,
    OSError,
    ValueError,
    TypeError,
)


def event_payload(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    if isinstance(event, Mapping):
        return dict(event)
    return {"type": getattr(event, "type", type(event).__name__)}


def response_payload(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    if isinstance(response, Mapping):
        return dict(response)
    raise TypeError("Unsupported response object.")


def redact_payload(
    value: Any,
    *,
    secrets: Iterable[str],
    base_url: str,
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): redact_payload(
                item,
                secrets=secrets,
                base_url=base_url,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_payload(
                item,
                secrets=secrets,
                base_url=base_url,
            )
            for item in value
        ]
    if isinstance(value, str):
        return redact_text(value, secrets=secrets, base_url=base_url)
    return value


def extract_output_text(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, Mapping):
                continue
            if content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
            elif content.get("type") == "refusal":
                parts.append(str(content.get("refusal") or ""))
    return "".join(parts)


def retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def http_status(exc: Exception) -> int | None:
    raw = getattr(exc, "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def request_id(exc: Exception) -> str | None:
    value = getattr(exc, "request_id", None)
    return str(value) if value else None


def is_retryable(record: Mapping[str, Any]) -> bool:
    error = record.get("error") or {}
    if error.get("http_status") in {408, 409, 429, 500, 502, 503, 504}:
        return True
    return error.get("type") in {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "TimeoutError",
    }
