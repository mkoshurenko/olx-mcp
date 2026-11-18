"""Unittest/Pytest suite covering Stages 1, 2, and 3 of `olx-mcp`."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from olx_mcp.server import run_self_test


class TestOlxMcp(unittest.TestCase):
    def test_olx_mcp_self_test(self) -> None:
        self.assertEqual(run_self_test(), 0)

    def test_live_auth_and_chat(self) -> None:
        from olx_mcp.server import get_default_service

        srv = get_default_service()
        st = srv.auth_status({"verify_live": True})
        print("\n[LIVE AUTH STATUS]:", st, flush=True)
        self.assertIn("authenticated", str(st.get("live_check")))
        threads = srv.list_threads({"limit": 3})
        print("\n[LIVE THREADS]:", threads, flush=True)
        self.assertGreaterEqual(threads.get("count", 0), 1)

        # Verify live geocoding & radius filtering around a Kyiv metro station
        geo = srv.resolve_location({"city": "Київ", "address": "метро Осокорки, Київ"})
        print("\n[LIVE GEOCODE]:", geo, flush=True)
        self.assertTrue(geo.get("geocode", {}).get("matched"))
        self.assertAlmostEqual(float(geo["geocode"]["lat"]), 50.395, delta=0.02)
        self.assertAlmostEqual(float(geo["geocode"]["lon"]), 30.616, delta=0.02)

        # Verify live real-estate radius search around метро Осокорки (within 1.5 km)
        apt_search = srv.search_offers(
            {
                "query": "квартира Осокорки",
                "category_id": 1,
                "city": "Київ",
                "near_address": "метро Осокорки, Київ",
                "radius_km": 2.0,
                "geocode_offer_addresses": False,
                "limit": 10,
            }
        )
        print(
            f"\n[LIVE APARTMENT RADIUS SEARCH]: returned={apt_search['returned_count']}, "
            f"excluded_outside_radius={apt_search.get('excluded_outside_radius_or_district')}",
            flush=True,
        )
        for sample_apt in (apt_search.get("offers") or [])[:3]:
            print(
                f"  -> {sample_apt.get('distance_km')} km (~{sample_apt.get('walking_minutes_est')} min walk) | "
                f"{sample_apt.get('price_value')} {sample_apt.get('currency')} | {sample_apt.get('title')} | "
                f"extracted={sample_apt.get('extracted_addresses')}",
                flush=True,
            )
            self.assertLessEqual(float(sample_apt["distance_km"]), 2.0)


if __name__ == "__main__":
    unittest.main()

