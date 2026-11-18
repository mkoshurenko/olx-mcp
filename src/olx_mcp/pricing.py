"""Market price benchmarking, outlier removal, and haggling anchor engine for `olx-mcp`.

When the user says "I want to buy X", raw OLX results often contain cheap cases, boxes,
cables, or broken parts that skew the average price. This module:
1. Filters out accessory/parts noise using keyword & statistical IQR rules.
2. Computes true market percentiles (`min`, `p10`, `p25`, `median`, `p75`, `max`).
3. Calculates haggling anchors (`recommended_opening_offer`, `target_buy_price`, `walk_away_price`).
4. Scores each offer by **haggling leverage** (whether `is_negotiable=True`, listing age, private vs business seller, and discount vs median).
"""

from __future__ import annotations

import statistics
from typing import Any

from olx_mcp.models import OfferSummary

DEFAULT_NOISE_KEYWORDS: tuple[str, ...] = (
    "чохол",
    "чехол",
    "скло",
    "стекло",
    "плівка",
    "пленка",
    "коробка",
    "кабель",
    "ремінець",
    "ремешок",
    "запчастини",
    "запчасти",
    "на запчастини",
    "розборка",
    "разборка",
    "труп",
    "битий",
    "битый",
    "не працює",
    "не работает",
    "оренда",
    "аренда",
    "прокат",
    "куплю",
    "обмін",
)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 2)
    idx = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac, 2)


def _round_to_50(value: float) -> float:
    """Round haggling offers and targets to clean multiples of 50 UAH (ending in 00 or 50)."""
    if value < 100:
        return round(value, -1)
    return float(int(round(value / 50.0) * 50))


def compute_haggling_leverage(
    offer: OfferSummary,
    market_median: float,
    market_p25: float,
) -> dict[str, Any]:
    """Score an offer's haggling potential (0..100) and generate tactical negotiation tips."""
    score = 35
    reasons: list[str] = []

    if offer.is_negotiable:
        score += 25
        reasons.append("seller marked price as negotiable ('Торг')")

    if not offer.is_business:
        score += 12
        reasons.append("private seller (more flexible than shops)")

    if offer.age_days >= 14:
        score += 18
        reasons.append(f"listed {offer.age_days}d ago (stale listing)")
    elif offer.age_days >= 5:
        score += 10
        reasons.append(f"listed {offer.age_days}d ago")

    price = offer.price_value or 0.0
    if price > 0 and market_median > 0:
        diff_pct = round(((price - market_median) / market_median) * 100.0, 1)
        if price <= market_p25:
            score += 10
            reasons.append(f"already in bottom 25% of market ({diff_pct}% vs median)")
        elif price > market_median * 1.15:
            score += 8
            reasons.append(f"priced +{diff_pct}% above median — use market comps to anchor down")
    else:
        diff_pct = 0.0

    score = max(0, min(100, score))

    # Suggested opening offer & target price for this specific listing (supports 500-1000 UAH hardware steps + %)
    if price > 0:
        if offer.is_negotiable or offer.age_days >= 7:
            opening_discount = 0.16 if price > market_median else 0.12
            target_discount = 0.10 if price > market_median else 0.07
        else:
            opening_discount = 0.10
            target_discount = 0.06

        raw_open_disc = max(price * opening_discount, 500.0 if price >= 3500 else price * 0.08)
        if 3000 <= price <= 15000:
            raw_open_disc = min(max(raw_open_disc, 500.0), 1200.0)
        raw_target_disc = max(price * target_discount, 300.0 if price >= 3500 else price * 0.05)

        suggested_opening = _round_to_50(min(price - raw_open_disc, market_p25 * 0.95 if market_p25 > 0 else price))
        suggested_opening = max(suggested_opening, _round_to_50(price * 0.72))
        suggested_target = _round_to_50(min(price - raw_target_disc, market_median if market_median > 0 else price))
        suggested_target = max(suggested_target, suggested_opening)
    else:
        suggested_opening = 0.0
        suggested_target = 0.0

    open_disc_uah = int(max(0.0, price - suggested_opening))
    target_disc_uah = int(max(0.0, price - suggested_target))
    pre_haggle_pitch_uk = (
        f"Знайшов гарний варіант: «{offer.title}» ({offer.city or 'Україна'}) за {int(price)} {offer.currency} "
        f"(медіана ринку ~{int(market_median)} {offer.currency}). "
        f"Прогноз по торгу: можемо просити знижку -{open_disc_uah} {offer.currency} "
        f"(старт з {int(suggested_opening)} {offer.currency}, очікувана фінальна ціна ~{int(suggested_target)} {offer.currency})."
    )

    return {
        "leverage_score": score,
        "vs_median_pct": diff_pct,
        "suggested_opening_offer": int(suggested_opening) if suggested_opening >= 100 else suggested_opening,
        "suggested_target_price": int(suggested_target) if suggested_target >= 100 else suggested_target,
        "expected_discount_range_uah": f"{target_disc_uah}–{open_disc_uah}",
        "pre_haggle_pitch_uk": pre_haggle_pitch_uk,
        "reasons": reasons,
    }


def benchmark_offers(
    offers: list[OfferSummary],
    *,
    query: str = "",
    target_currency: str = "UAH",
    exclude_keywords: list[str] | None = None,
    min_valid_price: float | None = None,
) -> dict[str, Any]:
    """Analyze a list of offers, strip accessory/broken noise, and return price percentiles + ranked candidates."""
    query_lower = query.lower()
    noise_words = [
        w for w in (exclude_keywords if exclude_keywords is not None else DEFAULT_NOISE_KEYWORDS)
        if w.lower() not in query_lower
    ]

    priced_offers: list[OfferSummary] = []
    excluded_noise: list[dict[str, Any]] = []

    for off in offers:
        if off.is_free or off.price_value is None or off.price_value <= 0:
            continue
        if target_currency and off.currency.upper() != target_currency.upper():
            continue
        title_lower = off.title.lower()
        matched_noise = next((w for w in noise_words if w in title_lower), None)
        if matched_noise:
            excluded_noise.append({"id": off.id, "title": off.title, "price": off.price_value, "reason": f"keyword:{matched_noise}"})
            continue
        if min_valid_price is not None and off.price_value < min_valid_price:
            excluded_noise.append({"id": off.id, "title": off.title, "price": off.price_value, "reason": "below_min_valid_price"})
            continue
        priced_offers.append(off)

    # Second pass: statistical floor filter (strip accessories priced < 22% of rough median)
    if len(priced_offers) >= 5 and min_valid_price is None:
        rough_median = statistics.median([o.price_value for o in priced_offers if o.price_value])
        floor_cutoff = rough_median * 0.22
        ceiling_cutoff = rough_median * 3.5
        filtered: list[OfferSummary] = []
        for off in priced_offers:
            val = off.price_value or 0.0
            if val < floor_cutoff:
                excluded_noise.append({"id": off.id, "title": off.title, "price": val, "reason": f"statistical_low_outlier(<{round(floor_cutoff)})"})
            elif val > ceiling_cutoff:
                excluded_noise.append({"id": off.id, "title": off.title, "price": val, "reason": f"statistical_high_outlier(>{round(ceiling_cutoff)})"})
            else:
                filtered.append(off)
        if filtered:
            priced_offers = filtered

    prices = sorted(o.price_value for o in priced_offers if o.price_value is not None)
    if not prices:
        return {
            "query": query,
            "currency": target_currency,
            "total_fetched": len(offers),
            "valid_count": 0,
            "excluded_count": len(excluded_noise),
            "stats": None,
            "haggling_strategy": None,
            "top_candidates": [],
            "excluded_samples": excluded_noise[:10],
        }

    p10 = _percentile(prices, 10)
    p25 = _percentile(prices, 25)
    median = _percentile(prices, 50)
    p75 = _percentile(prices, 75)
    mean = round(statistics.mean(prices), 2)

    recommended_opening = _round_to_50(p25 * 0.88) if p25 >= 100 else round(p25 * 0.88, 2)
    target_buy_price = _round_to_50(p25 * 0.96) if p25 >= 100 else round(p25 * 0.96, 2)
    walk_away_price = _round_to_50(median) if median >= 100 else round(median, 2)

    candidates: list[dict[str, Any]] = []
    for off in priced_offers:
        lev = compute_haggling_leverage(off, market_median=median, market_p25=p25)
        # Rank by combination of good price vs median + high haggling leverage
        price_ratio = (off.price_value or median) / median if median > 0 else 1.0
        deal_rank_score = round((lev["leverage_score"] * 0.55) + max(0.0, (1.4 - price_ratio) * 45.0), 1)
        candidates.append(
            {
                "id": off.id,
                "title": off.title,
                "price": off.price_value,
                "currency": off.currency,
                "is_negotiable": off.is_negotiable,
                "condition": off.condition,
                "city": off.city,
                "age_days": off.age_days,
                "seller_name": off.seller_name,
                "is_business": off.is_business,
                "has_delivery": off.has_delivery,
                "url": off.url,
                "deal_score": deal_rank_score,
                "haggling": lev,
            }
        )

    candidates.sort(key=lambda c: (-c["deal_score"], c["price"] or 0))

    return {
        "query": query,
        "currency": target_currency,
        "total_fetched": len(offers),
        "valid_count": len(priced_offers),
        "excluded_count": len(excluded_noise),
        "stats": {
            "min": prices[0],
            "p10_steal": p10,
            "p25_good_deal": p25,
            "median_market": median,
            "mean": mean,
            "p75": p75,
            "max": prices[-1],
            "negotiable_share_pct": round(
                100.0 * sum(1 for o in priced_offers if o.is_negotiable) / len(priced_offers), 1
            ),
        },
        "haggling_strategy": {
            "recommended_opening_offer": int(recommended_opening) if recommended_opening >= 100 else recommended_opening,
            "target_buy_price": int(target_buy_price) if target_buy_price >= 100 else target_buy_price,
            "walk_away_ceiling": int(walk_away_price) if walk_away_price >= 100 else walk_away_price,
            "playbook_uk": (
                f"Ринкова медіана: ~{int(median)} {target_currency}, хороша ціна (p25): ~{int(p25)} {target_currency}. "
                f"Рекомендований старт торгу: {int(recommended_opening)} {target_currency} із ціллю зійтися на "
                f"{int(target_buy_price)}–{int(p25)} {target_currency}. Напиши 3–5 продавцям із найвищим deal_score "
                f"(особливо тим, де є позначка 'Торг' або оголошенню >5 днів)."
            ),
        },
        "top_candidates": candidates[:15],
        "excluded_samples": excluded_noise[:8],
    }
