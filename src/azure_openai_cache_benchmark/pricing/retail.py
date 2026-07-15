from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence

from ..constants import PRICE_SKUS, RETAIL_PRICES_URL
from ..errors import PricingError
from ..serialization import utc_now_iso
from .models import PriceBook, PricingOptions


def fetch_retail_price_items(
    *,
    timeout_seconds: float = 30.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = {
        "currencyCode": "USD",
        "$filter": "contains(meterName, '5.4 mini')",
    }
    url = RETAIL_PRICES_URL + "?" + urllib.parse.urlencode(query)
    items: list[dict[str, Any]] = []
    pages = 0
    retrieved_at = utc_now_iso()
    while url:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "az-openai-cache-benchmark/2.0",
            },
        )
        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds,
        ) as response:
            payload = json.load(response)
        pages += 1
        items.extend(payload.get("Items") or [])
        url = payload.get("NextPageLink") or ""
    return items, {
        "source": RETAIL_PRICES_URL,
        "filter": query["$filter"],
        "currency": "USD",
        "retrieved_at": retrieved_at,
        "pages": pages,
        "items": len(items),
    }


def _latest_consistent_meter(
    items: Sequence[Mapping[str, Any]],
    sku_name: str,
) -> dict[str, Any]:
    candidates = [
        dict(item)
        for item in items
        if item.get("productName") == "Azure OpenAI GPT5"
        and item.get("skuName") == sku_name
        and item.get("unitOfMeasure") == "1M"
        and item.get("type") == "Consumption"
    ]
    if not candidates:
        raise PricingError(f"Missing Global Standard meter {sku_name!r}.")
    latest_date = max(
        str(item.get("effectiveStartDate") or "") for item in candidates
    )
    latest = [
        item
        for item in candidates
        if str(item.get("effectiveStartDate") or "") == latest_date
    ]
    prices = {float(item["retailPrice"]) for item in latest}
    currencies = {str(item.get("currencyCode")) for item in latest}
    if len(prices) != 1 or len(currencies) != 1:
        raise PricingError(
            f"Ambiguous price rows for {sku_name}: "
            f"prices={prices}, currencies={currencies}"
        )
    row = latest[0]
    return {
        "sku_name": sku_name,
        "meter_name": row.get("meterName"),
        "meter_id": row.get("meterId"),
        "unit_of_measure": row.get("unitOfMeasure"),
        "retail_price": next(iter(prices)),
        "currency": next(iter(currencies)),
        "effective_start_date": latest_date,
        "region_row_count": len(latest),
    }


def select_global_standard_price_book(
    items: Sequence[Mapping[str, Any]],
    *,
    retrieved_at: str | None = None,
) -> PriceBook:
    meters = {
        kind: _latest_consistent_meter(items, sku)
        for kind, sku in PRICE_SKUS.items()
    }
    currencies = {meter["currency"] for meter in meters.values()}
    if len(currencies) != 1:
        raise PricingError(
            f"Pricing meters use inconsistent currencies: {currencies}"
        )
    return PriceBook(
        currency=currencies.pop(),
        input_usd_per_million=float(meters["input"]["retail_price"]),
        cached_input_usd_per_million=float(
            meters["cached_input"]["retail_price"]
        ),
        output_usd_per_million=float(meters["output"]["retail_price"]),
        source=RETAIL_PRICES_URL,
        retrieved_at=retrieved_at or utc_now_iso(),
        meters=meters,
    )


def override_price_book(
    *,
    input_price: float,
    cached_input_price: float,
    output_price: float,
    currency: str = "USD",
) -> PriceBook:
    for label, value in (
        ("input", input_price),
        ("cached input", cached_input_price),
        ("output", output_price),
    ):
        if value < 0:
            raise PricingError(f"{label} price cannot be negative.")
    return PriceBook(
        currency=currency,
        input_usd_per_million=input_price,
        cached_input_usd_per_million=cached_input_price,
        output_usd_per_million=output_price,
        source="cli_override",
        retrieved_at=utc_now_iso(),
        meters={},
    )


def resolve_price_book(options: PricingOptions) -> PriceBook | None:
    overrides = (
        options.input_price,
        options.cached_input_price,
        options.output_price,
    )
    if any(value is not None for value in overrides):
        if not all(value is not None for value in overrides):
            raise PricingError(
                "Provide all three price overrides or none of them."
            )
        return override_price_book(
            input_price=float(options.input_price),
            cached_input_price=float(options.cached_input_price),
            output_price=float(options.output_price),
        )
    if options.skip:
        return None
    items, retrieval = fetch_retail_price_items(
        timeout_seconds=options.timeout_seconds
    )
    return select_global_standard_price_book(
        items,
        retrieved_at=retrieval["retrieved_at"],
    )
