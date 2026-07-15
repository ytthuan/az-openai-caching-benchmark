from __future__ import annotations

from typing import Any, Mapping


def format_percent(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1%}"


def format_ms(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.1f} ms"


def format_usd(value: Any) -> str:
    return "n/a" if value is None else f"${float(value):.6f}"


def allocation_table(allocation: Mapping[str, Any]) -> str:
    return "\n".join(
        f"| `{name}` | {count} |"
        for name, count in allocation.items()
    )


def root_cause_table(rows: list[Mapping[str, Any]]) -> str:
    rendered = "\n".join(
        "| `{factor}` | {baseline} | {degraded} | {delta} | `{status}` |".format(
            factor=row.get("factor"),
            baseline=format_percent(
                row.get("baseline_token_weighted_cache_rate")
            ),
            degraded=format_percent(
                row.get("degraded_token_weighted_cache_rate")
            ),
            delta=format_percent(row.get("delta")),
            status=row.get("status"),
        )
        for row in rows
    )
    return (
        rendered
        or "| n/a | n/a | n/a | n/a | `insufficient_data` |"
    )
