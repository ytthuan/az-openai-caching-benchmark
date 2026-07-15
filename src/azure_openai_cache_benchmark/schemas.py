from __future__ import annotations

from typing import Any

from .serialization import short_hash


def response_json_schema(namespace: str) -> dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": f"enterprise_cache_response_{short_hash(namespace, 10)}",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["CACHE_BENCHMARK_OK"],
                    },
                    "namespace": {"type": "string"},
                },
                "required": ["status", "namespace"],
                "additionalProperties": False,
            },
        }
    }


def build_tools(
    namespace: str,
    *,
    shuffle_seed: int | None = None,
) -> tuple[dict[str, Any], ...]:
    suffix = short_hash(namespace, 8)
    definitions = (
        ("registry_read", "Đọc metadata agent tổng hợp."),
        ("policy_read", "Đọc policy version tổng hợp."),
        ("usage_read", "Đọc token usage tổng hợp."),
        ("approval_read", "Đọc trạng thái phê duyệt tổng hợp."),
    )
    tools = [
        {
            "type": "function",
            "name": f"enterprise_{name}_{suffix}",
            "description": (
                f"{description} Namespace {namespace}; không gọi trong benchmark."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["identifier", "fields"],
                "additionalProperties": False,
            },
        }
        for name, description in definitions
    ]
    if shuffle_seed is not None:
        offset = (shuffle_seed % (len(tools) - 1)) + 1
        tools = tools[offset:] + tools[:offset]
        if shuffle_seed % 2:
            tools = list(reversed(tools))
    return tuple(tools)
