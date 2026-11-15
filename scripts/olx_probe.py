#!/usr/bin/env python3
"""Read-only olx-mcp probe: auth status + market benchmark. Sends no messages, writes no lots.

Usage: olx_probe.py "RTX 3090" [--city Київ] [--min-valid 15000] [--max-price 45000]
                    [--exclude "пк,комплект,ноутбук,майнінг"] [--category 123]
                    [--condition used|new] [--sample 40] [--all]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from olx_mcp.server import OlxBuyerService  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("query")
    p.add_argument("--city")
    p.add_argument("--min-valid", type=float)
    p.add_argument("--max-price", type=float)
    p.add_argument("--exclude", help="comma-separated keywords to drop")
    p.add_argument("--category", type=int)
    p.add_argument("--condition", choices=["used", "new"])
    p.add_argument("--sample", type=int, default=40)
    p.add_argument("--all", action="store_true", help="print every candidate, not the top 3")
    a = p.parse_args()

    s = OlxBuyerService()
    auth = s.auth_status({})
    print("auth:", auth.get("live_check"), "expires", auth.get("token_expires_at"))

    args = {"query": a.query, "sample_size": a.sample}
    if a.city:
        args["city"] = a.city
    if a.min_valid:
        args["min_valid_price"] = a.min_valid
    if a.max_price:
        args["max_price"] = a.max_price
    if a.exclude:
        args["exclude_keywords"] = [k.strip() for k in a.exclude.split(",") if k.strip()]
    if a.category:
        args["category_id"] = a.category
    if a.condition:
        args["condition"] = a.condition
    r = s.benchmark_market_price(args)

    for key in ("stats", "haggling"):
        if key in r:
            print(f"{key}:", json.dumps(r[key], ensure_ascii=False))
    lists = [k for k, v in r.items() if isinstance(v, list) and v and isinstance(v[0], dict)]
    for k in lists:
        rows = r[k] if a.all else r[k][:3]
        print(f"{k} ({len(rows)} of {len(r[k])}):")
        for o in rows:
            if a.all:
                print(f"  {o.get('price')} | {str(o.get('title'))[:70]} | {o.get('city')} | {o.get('age_days')}d | {o.get('reason', '')} | {o.get('url', '')}")
            else:
                print(" ", json.dumps(o, ensure_ascii=False)[:500])
    if not lists and "stats" not in r:
        print(json.dumps(r, ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
