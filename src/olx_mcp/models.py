"""Compact data models and OLX.ua API response normalizers for `olx-mcp`.

Strips 400KB+ raw OLX `/api/v1/offers/` responses down to clean, token-efficient
records containing price, negotiable status, seller tenure, condition, location,
and haggling leverage signals.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_html_text(raw: str | None, max_len: int = 800) -> str:
    """Strip HTML tags and normalize whitespace for offer descriptions."""
    if not raw:
        return ""
    unescaped = html.unescape(str(raw))
    with_breaks = re.sub(r"<br\s*/?>|</p>|</li>", "\n", unescaped, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", with_breaks)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    compact = "\n".join(line for line in lines if line)
    if len(compact) > max_len:
        return compact[: max_len - 3].rstrip() + "..."
    return compact


@dataclass(frozen=True, slots=True)
class OfferSummary:
    """Normalized OLX offer card with price, negotiable flag, and seller signals."""

    id: int
    title: str
    url: str
    price_value: float | None
    currency: str
    is_negotiable: bool
    is_free: bool
    condition: str
    city: str
    region: str
    district: str
    created_time: str
    last_refresh_time: str
    age_days: int
    seller_id: int | None
    seller_name: str
    seller_created: str
    is_business: bool
    has_delivery: bool
    is_highlighted: bool
    is_top_ad: bool
    description_snippet: str
    top_image_url: str
    params: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_iso_age_days(iso_str: str | None, now: datetime | None = None) -> int:
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        ref = now or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = ref - dt
        return max(0, int(delta.total_seconds() // 86400))
    except Exception:
        return 0


def parse_offer(raw: dict[str, Any], *, max_desc_len: int = 500) -> OfferSummary:
    """Convert a raw OLX `/api/v1/offers/` item dict into an `OfferSummary`."""
    offer_id = int(raw.get("id") or 0)
    title = _WS_RE.sub(" ", str(raw.get("title") or "").strip())
    url = str(raw.get("url") or "")

    price_value: float | None = None
    currency = "UAH"
    is_negotiable = False
    is_free = False
    condition = "unknown"
    extra_params: dict[str, str] = {}

    for param in raw.get("params") or []:
        if not isinstance(param, dict):
            continue
        key = str(param.get("key") or "")
        name = str(param.get("name") or key)
        val = param.get("value") or {}
        if key == "price" and isinstance(val, dict):
            raw_val = val.get("value")
            if isinstance(raw_val, (int, float)):
                price_value = float(raw_val)
            currency = str(val.get("currency") or "UAH")
            is_negotiable = bool(val.get("negotiable", False))
            if str(val.get("type") or "") == "free":
                is_free = True
                price_value = 0.0
        elif key == "state" and isinstance(val, dict):
            condition = str(val.get("key") or val.get("label") or "unknown")
        elif isinstance(val, dict):
            label = str(val.get("label") or val.get("key") or "").strip()
            if label:
                extra_params[name] = label

    loc = raw.get("location") or {}
    city = str((loc.get("city") or {}).get("name") or "")
    region = str((loc.get("region") or {}).get("name") or "")
    district = str((loc.get("district") or {}).get("name") or "")

    created_time = str(raw.get("created_time") or "")
    last_refresh_time = str(raw.get("last_refresh_time") or "")
    age_days = _parse_iso_age_days(created_time)

    user = raw.get("user") or {}
    seller_id = int(user["id"]) if isinstance(user.get("id"), int) else None
    seller_name = str(user.get("name") or "")
    seller_created = str(user.get("created") or "")
    is_business = bool(raw.get("business", False))

    delivery_block = raw.get("delivery") or {}
    rock_block = delivery_block.get("rock") if isinstance(delivery_block, dict) else {}
    has_delivery = bool(isinstance(rock_block, dict) and rock_block.get("active", False))

    promo = raw.get("promotion") or {}
    is_highlighted = bool(promo.get("highlighted", False))
    is_top_ad = bool(promo.get("top_ad", False))

    photos = raw.get("photos") or []
    top_image_url = ""
    if photos and isinstance(photos[0], dict):
        link = str(photos[0].get("link") or "")
        top_image_url = link.replace("{width}", "800").replace("{height}", "600")

    description_snippet = clean_html_text(raw.get("description"), max_len=max_desc_len)

    return OfferSummary(
        id=offer_id,
        title=title,
        url=url,
        price_value=price_value,
        currency=currency,
        is_negotiable=is_negotiable,
        is_free=is_free,
        condition=condition,
        city=city,
        region=region,
        district=district,
        created_time=created_time,
        last_refresh_time=last_refresh_time,
        age_days=age_days,
        seller_id=seller_id,
        seller_name=seller_name,
        seller_created=seller_created,
        is_business=is_business,
        has_delivery=has_delivery,
        is_highlighted=is_highlighted,
        is_top_ad=is_top_ad,
        description_snippet=description_snippet,
        top_image_url=top_image_url,
        params=extra_params,
    )
