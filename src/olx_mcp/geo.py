"""Geocoding, Street/Metro Address Extraction, & Haversine Distance Engine for `olx-mcp`.

Supports:
1. Google Maps Geocoding API (`https://maps.googleapis.com/maps/api/geocode/json`) when `google_maps_api_key` is configured.
2. OpenStreetMap Nominatim (`https://nominatim.openstreetmap.org/search`) as a zero-key fallback with SQLite/JSON disk cache.
3. Extracting street addresses, residential complexes (ЖК), and metro landmarks from Ukrainian/Russian real-estate titles and descriptions.
4. Haversine distance (`distance_km`) and walking/driving time estimates between a target landmark (e.g., metro station) and an offer.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from olx_mcp import DEFAULT_STATE_DIR

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_GEOCACHE_PATH = DEFAULT_STATE_DIR / "geocache.json"

# Regex patterns to extract Ukrainian/Russian street addresses and residential complexes (ЖК) from OLX descriptions
_STREET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # вул. Хрещатик 22 / вул. С.Русової 5-б / вулиця Єлизавети Чавдар, 11 / ул. Саксаганского 12А
    re.compile(
        r"(?:^|[\s,;(])(?:вул\.?|вулиця|ул\.?|улица|просп\.?|проспект|пр-т|бул\.?|бульвар|пров\.?|провулок|пер\.?|наб\.?|набережна|шосе|площа|пл\.?)\s*"
        r"((?:[А-ЯІЇЄҐЁA-Z]\.\s*)?[А-ЯІЇЄҐЁA-Z][А-ЯІЇЄҐЁа-яіїєґёA-Za-z'\- ]{2,35})"
        r"(?:[ \t,]*(?:буд\.?|дом|д\.?|№)?[ \t]*(\d{1,3}[ \t]*[А-ЯІЇЄҐа-яіїєґA-Za-z/\-\d]{0,4}))?(?=\s*[,.;\n\r()/|]|$)",
        re.UNICODE,
    ),
    # ЖК "Французький квартал 2" / ЖК Варшавський Плюс / ЖК Great
    re.compile(
        r"(?:^|[\s,;(])(ЖК[ \t]+[«\"']?[А-ЯІЇЄҐЁA-Z0-9][А-ЯІЇЄҐЁа-яіїєґёA-Za-z0-9 \-]{2,32}?[»\"']?)(?=[,.;\n\r()/|]|$)",
        re.UNICODE,
    ),
    # м. Осокорки / метро Позняки / ст. м. Лук'янівська
    re.compile(
        r"(?:^|[\s,;(])(?:ст\.?[ \t]*м\.?|метро|м\.)[ \t]+([А-ЯІЇЄҐЁ][А-ЯІЇЄҐЁа-яіїєґё'\- ]{3,25}?)(?=[,.;\n\r()/|]|$)",
        re.UNICODE,
    ),
)

_STOP_WORDS_AFTER_STREET = (
    "поверх",
    "этаж",
    "кімнат",
    "комнат",
    "квартира",
    "здається",
    "сдается",
    "продам",
    "продаж",
    "оренда",
    "аренда",
    "ціна",
    "цена",
    "без комісії",
    "метро",
    "район",
)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in kilometers between two WGS84 coordinates."""
    r_earth = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return round(r_earth * c, 3)


def estimate_walking_minutes(distance_km: float) -> int:
    """Estimate realistic urban walking time in minutes (~1.25 road winding factor at 4.8 km/h)."""
    if distance_km <= 0:
        return 0
    return max(1, int(round((distance_km * 1.25 / 4.8) * 60.0)))


def extract_address_candidates(title: str, description: str = "", district: str = "") -> list[str]:
    """Extract plausible street addresses, residential complexes (ЖК), or metro stations from listing text."""
    combined = f"{title}\n{description}"
    candidates: list[str] = []

    for pat in _STREET_PATTERNS:
        for match in pat.finditer(combined):
            if match.lastindex and match.lastindex >= 2 and match.group(2):
                street_part = match.group(1).strip(" ,.-")
                num_part = match.group(2).strip(" ,.-")
                full_match = match.group(0).strip(" ,.;()-")
                prefix = full_match.split(street_part)[0].strip() or "вул."
                raw_cand = f"{prefix} {street_part} {num_part}".strip()
            else:
                raw_cand = match.group(0).strip(" ,.;()-«»\"'")

            # Trim trailing stop words
            lower_cand = raw_cand.lower()
            for sw in _STOP_WORDS_AFTER_STREET:
                idx = lower_cand.find(f" {sw}")
                if idx > 4:
                    raw_cand = raw_cand[:idx].strip(" ,.-")
                    lower_cand = raw_cand.lower()

            if 4 <= len(raw_cand) <= 65 and raw_cand not in candidates:
                candidates.append(raw_cand)

    if district and district not in candidates:
        candidates.append(f"{district} район")
    return candidates[:4]


class GeoResolver:
    """Geocodes addresses/landmarks via Google Maps Geocoding API (primary) or OpenStreetMap Nominatim (fallback) with local cache."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        cache_path: Path = DEFAULT_GEOCACHE_PATH,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = (api_key or os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip() or None
        self.cache_path = Path(cache_path)
        self.timeout = timeout
        self._last_nominatim_ts = 0.0

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_cache(self, cache: dict[str, dict[str, Any]]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _normalize_query(address: str, city: str | None = None, country: str = "Україна") -> str:
        clean = " ".join(address.strip().split())
        if city and city.lower() not in clean.lower():
            clean = f"{clean}, {city.strip()}"
        if country and country.lower() not in clean.lower() and "ukraine" not in clean.lower():
            clean = f"{clean}, {country}"
        return clean

    def geocode(
        self,
        address: str,
        *,
        city: str | None = None,
        country: str = "Україна",
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Resolve a street address, ЖК, district, or metro station into `{lat, lon, formatted_address, provider}`."""
        if not address or not address.strip():
            raise ValueError("`address` is required for geocoding.")

        full_query = self._normalize_query(address, city=city, country=country)
        cache_key = f"{'google' if self.api_key else 'osm'}:{full_query.lower()}"
        cache = self._load_cache() if use_cache else {}

        if use_cache and cache_key in cache:
            cached = dict(cache[cache_key])
            cached["cached"] = True
            return cached

        result: dict[str, Any] | None = None
        google_error: str | None = None

        if self.api_key:
            try:
                result = self._geocode_google(full_query)
            except Exception as exc:
                google_error = str(exc)

        if result is None:
            result = self._geocode_nominatim(full_query)
            if google_error and result:
                result["google_fallback_note"] = google_error

        if result and result.get("matched"):
            cache[cache_key] = result
            if use_cache:
                self._save_cache(cache)
            return result

        return {
            "matched": False,
            "query": full_query,
            "provider": "google_maps" if self.api_key else "nominatim_osm",
            "error": google_error or "Location not found",
        }

    def _geocode_google(self, full_query: str) -> dict[str, Any] | None:
        assert self.api_key is not None
        params = {
            "address": full_query,
            "language": "uk",
            "region": "ua",
            "key": self.api_key,
        }
        url = f"{GOOGLE_GEOCODE_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        status = data.get("status")
        if status == "ZERO_RESULTS":
            return {"matched": False, "query": full_query, "provider": "google_maps"}
        if status != "OK":
            err_msg = data.get("error_message") or status
            raise RuntimeError(f"Google Geocoding API status={status}: {err_msg}")

        results = data.get("results") or []
        if not results:
            return {"matched": False, "query": full_query, "provider": "google_maps"}

        top = results[0]
        geom = top.get("geometry") or {}
        loc = geom.get("location") or {}
        lat = float(loc["lat"])
        lon = float(loc["lng"])
        return {
            "matched": True,
            "query": full_query,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "formatted_address": top.get("formatted_address") or full_query,
            "location_type": geom.get("location_type") or "APPROXIMATE",
            "place_id": top.get("place_id"),
            "provider": "google_maps",
            "cached": False,
        }

    def _geocode_nominatim(self, full_query: str) -> dict[str, Any] | None:
        # Nominatim prefers clean landmark/street names without prefixes like 'метро ' or 'ЖК ' when those aren't in the OSM name tag
        variants = [full_query]
        stripped = re.sub(
            r"^\s*(?:ст\.?\s*м\.?|станція\s+метро|метро|м\.|жк)\s+",
            "",
            full_query,
            flags=re.IGNORECASE,
        ).strip()
        if stripped and stripped != full_query:
            variants.append(stripped)

        last_err: str | None = None
        for q_var in variants:
            elapsed = time.monotonic() - self._last_nominatim_ts
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

            params = {
                "q": q_var,
                "format": "jsonv2",
                "limit": 3,
                "accept-language": "uk,ru,en",
                "countrycodes": "ua",
            }
            url = f"{NOMINATIM_SEARCH_URL}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "olx-mcp-geocoder/1.0 (local personal assistant)",
                    "Accept": "application/json",
                },
            )
            try:
                self._last_nominatim_ts = time.monotonic()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    items = json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception as exc:
                last_err = str(exc)
                continue

            if items and isinstance(items, list):
                # Prefer railway/station/subway or building/highway if present
                top = next(
                    (
                        it
                        for it in items
                        if isinstance(it, dict)
                        and str(it.get("type") or "") in ("station", "subway", "subway_entrance", "residential", "apartments", "house")
                    ),
                    items[0],
                )
                lat = float(top["lat"])
                lon = float(top["lon"])
                return {
                    "matched": True,
                    "query": full_query,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "formatted_address": top.get("display_name") or q_var,
                    "location_type": top.get("type") or top.get("category") or "osm",
                    "place_id": str(top.get("place_id") or ""),
                    "provider": "nominatim_osm",
                    "cached": False,
                }

        return {
            "matched": False,
            "query": full_query,
            "provider": "nominatim_osm",
            "error": last_err or "Location not found",
        }

    def enrich_offer_location(
        self,
        offer_dict: dict[str, Any],
        *,
        target_lat: float | None = None,
        target_lon: float | None = None,
        geocode_extracted_streets: bool = True,
    ) -> dict[str, Any]:
        """Resolve the most accurate coordinates for an offer (extracted street/ЖК -> geocoded, or OLX map pin) and compute distance to target."""
        title = str(offer_dict.get("title") or "")
        desc = str(offer_dict.get("description_snippet") or "")
        city = str(offer_dict.get("city") or "")
        district = str(offer_dict.get("district") or "")

        offer_lat = offer_dict.get("lat")
        offer_lon = offer_dict.get("lon")
        map_radius = offer_dict.get("map_radius")
        coord_source = "olx_map_pin" if (offer_lat is not None and offer_lon is not None) else "none"
        extracted_candidates = extract_address_candidates(title, desc, district=district)
        geocoded_address: str | None = None

        # If OLX map pin is missing or is a coarse circle (radius >= 500m) and we found a specific street/ЖК in text, geocode it
        should_try_street_geocode = geocode_extracted_streets and extracted_candidates and (
            offer_lat is None
            or offer_lon is None
            or (isinstance(map_radius, (int, float)) and map_radius >= 400)
            or bool(self.api_key)
        )
        if should_try_street_geocode:
            for cand in extracted_candidates:
                if cand.endswith("район") and offer_lat is not None:
                    continue
                geo = self.geocode(cand, city=city or None)
                if geo.get("matched"):
                    offer_lat = geo["lat"]
                    offer_lon = geo["lon"]
                    geocoded_address = f"{cand} -> {geo.get('formatted_address')}"
                    coord_source = f"geocoded_text ({geo.get('provider')})"
                    break

        distance_km: float | None = None
        walking_minutes: int | None = None
        if (
            target_lat is not None
            and target_lon is not None
            and isinstance(offer_lat, (int, float))
            and isinstance(offer_lon, (int, float))
        ):
            distance_km = haversine_km(target_lat, target_lon, float(offer_lat), float(offer_lon))
            walking_minutes = estimate_walking_minutes(distance_km)

        enriched = dict(offer_dict)
        enriched["lat"] = offer_lat
        enriched["lon"] = offer_lon
        enriched["coord_source"] = coord_source
        enriched["extracted_addresses"] = extracted_candidates
        if geocoded_address:
            enriched["geocoded_address"] = geocoded_address
        if distance_km is not None:
            enriched["distance_km"] = distance_km
            enriched["walking_minutes_est"] = walking_minutes
        return enriched
