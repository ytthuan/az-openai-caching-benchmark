from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PriceBook:
    currency: str
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float
    source: str
    retrieved_at: str
    meters: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PricingOptions:
    skip: bool = False
    timeout_seconds: float = 30.0
    input_price: float | None = None
    cached_input_price: float | None = None
    output_price: float | None = None
