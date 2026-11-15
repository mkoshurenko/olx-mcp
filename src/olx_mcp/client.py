"""Async/Sync HTTP client for `www.olx.ua` public & authenticated APIs.

Covers:
1. Public Search & Offer Details (`/api/v1/offers/`, `/api/v1/offers/{id}/`)
2. Categories & Location resolution
3. Authenticated Account, Personal Offers & Seller Chat endpoints (Bearer token + cookies)
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from olx_mcp.models import OfferSummary, parse_offer

DEFAULT_BASE_URL = "https://www.olx.ua"
CHAT_BASE_URL = "https://api.chat.olx.ua"
CHROME_MAC_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def ws_cdp_call(ws_url: str, method: str, params: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """Minimal stdlib RFC 6455 WebSocket client for localhost Chrome DevTools Protocol (CDP)."""
    parsed = urllib.parse.urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 9222)
    path = parsed.path or "/"

    with socket.create_connection((host, port), timeout=timeout) as sock:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode("ascii"))

        resp_buf = b""
        while b"\r\n\r\n" not in resp_buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP WebSocket handshake closed unexpectedly")
            resp_buf += chunk

        payload_bytes = json.dumps({"id": 1, "method": method, "params": params or {}}).encode("utf-8")
        mask_key = os.urandom(4)
        header = bytearray([0x81])  # FIN + text frame
        length = len(payload_bytes)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload_bytes))
        sock.sendall(bytes(header) + masked)

        while True:
            hdr = sock.recv(2)
            if len(hdr) < 2:
                raise RuntimeError("CDP WebSocket closed before response")
            masked_srv = bool(hdr[1] & 0x80)
            plen = hdr[1] & 0x7F
            if plen == 126:
                plen = struct.unpack("!H", sock.recv(2))[0]
            elif plen == 127:
                plen = struct.unpack("!Q", sock.recv(8))[0]
            srv_mask = sock.recv(4) if masked_srv else b""
            data = bytearray()
            while len(data) < plen:
                packet = sock.recv(plen - len(data))
                if not packet:
                    break
                data.extend(packet)
            if masked_srv:
                data = bytearray(b ^ srv_mask[i % 4] for i, b in enumerate(data))
            msg = json.loads(data.decode("utf-8", errors="replace"))
            if msg.get("id") == 1:
                return msg.get("result") or {}

# Common OLX.ua root categories for fast lookup without extra roundtrips
OLX_UA_CATEGORIES: dict[int, str] = {
    37: "Електроніка (Electronics)",
    38: "Телефони та аксесуари (Phones & Accessories)",
    1531: "Ноутбуки та аксесуари (Laptops & PC)",
    36: "Дитячий світ (Kids)",
    1: "Нерухомість (Real Estate)",
    1532: "Авто / Транспорт (Vehicles)",
    6: "Запчастини для транспорту (Auto Parts)",
    891: "Мода і стиль (Fashion)",
    89: "Дім і сад (Home & Garden)",
    597: "Хобі, відпочинок і спорт (Hobby & Sports)",
    35: "Тварини (Pets)",
    7: "Робота (Jobs)",
    75: "Бізнес та послуги (Business & Services)",
}

# Common OLX.ua major city IDs for instant resolution
OLX_UA_CITIES: dict[str, dict[str, Any]] = {
    "київ": {"city_id": 268, "region_id": 25, "name": "Київ"},
    "kyiv": {"city_id": 268, "region_id": 25, "name": "Київ"},
    "львів": {"city_id": 302, "region_id": 14, "name": "Львів"},
    "lviv": {"city_id": 302, "region_id": 14, "name": "Львів"},
    "одеса": {"city_id": 317, "region_id": 16, "name": "Одеса"},
    "odesa": {"city_id": 317, "region_id": 16, "name": "Одеса"},
    "харків": {"city_id": 280, "region_id": 21, "name": "Харків"},
    "kharkiv": {"city_id": 280, "region_id": 21, "name": "Харків"},
    "дніпро": {"city_id": 215, "region_id": 4, "name": "Дніпро"},
    "dnipro": {"city_id": 215, "region_id": 4, "name": "Дніпро"},
    "вінниця": {"city_id": 210, "region_id": 2, "name": "Вінниця"},
    "запоріжжя": {"city_id": 235, "region_id": 8, "name": "Запоріжжя"},
    "івано-франківськ": {"city_id": 244, "region_id": 9, "name": "Івано-Франківськ"},
    "тернопіль": {"city_id": 343, "region_id": 20, "name": "Тернопіль"},
    "ужгород": {"city_id": 346, "region_id": 7, "name": "Ужгород"},
    "полтава": {"city_id": 324, "region_id": 17, "name": "Полтава"},
    "рівне": {"city_id": 328, "region_id": 18, "name": "Рівне"},
    "луцьк": {"city_id": 298, "region_id": 3, "name": "Луцьк"},
    "чернівці": {"city_id": 362, "region_id": 24, "name": "Чернівці"},
    "черкаси": {"city_id": 359, "region_id": 23, "name": "Черкаси"},
    "хмельницький": {"city_id": 354, "region_id": 22, "name": "Хмельницький"},
    "житомир": {"city_id": 230, "region_id": 6, "name": "Житомир"},
    "миколаїв": {"city_id": 309, "region_id": 15, "name": "Миколаїв"},
    "кропивницький": {"city_id": 265, "region_id": 11, "name": "Кропивницький"},
    "суми": {"city_id": 339, "region_id": 19, "name": "Суми"},
    "чернігів": {"city_id": 364, "region_id": 26, "name": "Чернігів"},
}

SORT_BY_MAP: dict[str, str] = {
    "relevance": "relevance:desc",
    "newest": "created_at:desc",
    "cheapest": "filter_float_price:asc",
    "most_expensive": "filter_float_price:desc",
}


_RAW_OFFER_CACHE: dict[int, dict[str, Any]] = {}
_SHARED_CDP_PROC: subprocess.Popen[bytes] | None = None
_SHARED_CDP_WS_URL: str = ""
_SHARED_CDP_TMP_DIR: str = ""
_SHARED_CDP_PERSISTENT: bool = False


def _cleanup_shared_cdp() -> None:
    global _SHARED_CDP_PROC, _SHARED_CDP_WS_URL, _SHARED_CDP_TMP_DIR
    if _SHARED_CDP_PROC is not None:
        try:
            _SHARED_CDP_PROC.terminate()
            _SHARED_CDP_PROC.wait(timeout=2.0)
        except Exception:
            try:
                _SHARED_CDP_PROC.kill()
            except Exception:
                pass
        _SHARED_CDP_PROC = None
    _SHARED_CDP_WS_URL = ""
    if _SHARED_CDP_TMP_DIR:
        shutil.rmtree(_SHARED_CDP_TMP_DIR, ignore_errors=True)
        _SHARED_CDP_TMP_DIR = ""


atexit.register(_cleanup_shared_cdp)


def _get_or_start_shared_cdp(base_url: str, persistent: bool = False) -> str:
    """Start (or reuse) a Chrome with CDP. `persistent=True` opens a visible Chrome on the login profile."""
    global _SHARED_CDP_PROC, _SHARED_CDP_WS_URL, _SHARED_CDP_TMP_DIR, _SHARED_CDP_PERSISTENT
    if (
        _SHARED_CDP_PERSISTENT == persistent
        and _SHARED_CDP_PROC is not None
        and _SHARED_CDP_PROC.poll() is None
        and _SHARED_CDP_WS_URL
    ):
        try:
            chk = ws_cdp_call(
                _SHARED_CDP_WS_URL,
                "Runtime.evaluate",
                {"expression": "location.hostname.includes('olx.ua')", "returnByValue": True},
                timeout=1.5,
            )
            if ((chk.get("result") or {}).get("value")) is True:
                return _SHARED_CDP_WS_URL
        except Exception:
            pass
    _cleanup_shared_cdp()

    if not Path(CHROME_MAC_PATH).exists():
        return ""
    port = find_free_port()
    _SHARED_CDP_PERSISTENT = persistent
    if persistent:
        profile_dir = Path.home() / ".config" / "olx-mcp" / "chrome-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            CHROME_MAC_PATH,
            f"--user-data-dir={profile_dir}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-position=-2000,0",
            f"{base_url.rstrip('/')}/robots.txt",
        ]
    else:
        _SHARED_CDP_TMP_DIR = tempfile.mkdtemp(prefix="olx_cdp_shared_")
        cmd = [
            CHROME_MAC_PATH,
            "--headless=new",
            f"--user-agent={DEFAULT_USER_AGENT}",
            f"--user-data-dir={_SHARED_CDP_TMP_DIR}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            f"{base_url.rstrip('/')}/robots.txt",
        ]
    _SHARED_CDP_PROC = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1.5) as resp:
                targets = json.loads(resp.read().decode("utf-8", errors="replace"))
            page_targets = [
                t for t in targets
                if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            ]
            if page_targets and "olx.ua" in str(page_targets[0].get("url") or ""):
                candidate_ws = str(page_targets[0]["webSocketDebuggerUrl"])
                chk = ws_cdp_call(
                    candidate_ws,
                    "Runtime.evaluate",
                    {
                        "expression": "location.hostname.includes('olx.ua') && document.readyState === 'complete'",
                        "returnByValue": True,
                    },
                    timeout=2.0,
                )
                if ((chk.get("result") or {}).get("value")) is True:
                    _SHARED_CDP_WS_URL = candidate_ws
                    return _SHARED_CDP_WS_URL
        except Exception:
            pass
        time.sleep(0.25)
    return ""


class OlxApiError(RuntimeError):
    """Structured error from OLX API calls."""

    def __init__(self, status_code: int, message: str, url: str = "") -> None:
        super().__init__(f"HTTP {status_code} ({url}): {message}")
        self.status_code = status_code
        self.message = message
        self.url = url


class OlxClient:
    """HTTP client for `www.olx.ua` public and authenticated endpoints."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        access_token: str | None = None,
        cookie: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.cookie = cookie
        self.timeout = timeout

    def _headers(
        self,
        *,
        require_auth: bool = False,
        include_cookies: bool = False,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": f"{self.base_url}/uk/",
        }
        if self.access_token:
            tok = self.access_token.strip()
            if not tok.lower().startswith("bearer "):
                tok = f"Bearer {tok}"
            headers["Authorization"] = tok
        if include_cookies and self.cookie and "Authorization" not in headers:
            headers["Cookie"] = self.cookie.strip()
        if require_auth and "Authorization" not in headers and "Cookie" not in headers:
            raise OlxApiError(
                401,
                "Missing OLX credentials. Call `browser_login` or `set_auth_credentials` first.",
                self.base_url,
            )
        if extra:
            headers.update(extra)
        return headers

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean_params:
                qs = urllib.parse.urlencode(clean_params, doseq=True)
                url = f"{url}{'&' if '?' in url else '?'}{qs}"

        headers = self._headers(require_auth=require_auth)
        body_bytes: bytes | None = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                if not raw_bytes:
                    return {}
                return json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                pass
            if exc.code == 403 and ("The request could not be satisfied" in err_body or "CloudFront" in err_body):
                for persistent in (False, True):
                    cdp_res = self._request_via_chrome_cdp(
                        method=method.upper(),
                        url=url,
                        headers=headers,
                        json_body=json_body,
                        persistent=persistent,
                    )
                    if cdp_res is not None:
                        return cdp_res
                hint = (
                    "CloudFront WAF/geo-block (HTTP 403): www.olx.ua blocked both direct HTTP and headless Chrome fallback. "
                    "Disconnect US VPN (or route www.olx.ua via direct/UA/EU connection) and retry. "
                    f"Raw: {err_body[:250]}"
                )
                raise OlxApiError(exc.code, hint, url) from exc
            raise OlxApiError(exc.code, err_body or str(exc.reason), url) from exc
        except urllib.error.URLError as exc:
            raise OlxApiError(503, f"Network error: {exc.reason}", url) from exc

    def _request_via_chrome_cdp(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        persistent: bool = False,
    ) -> dict[str, Any] | None:
        """Execute a same-origin fetch() inside a shared Chrome (headless, or the logged-in profile when `persistent`) to bypass CloudFront JA3 TLS fingerprint blocks."""
        ws_url = _get_or_start_shared_cdp(self.base_url, persistent)
        if not ws_url:
            return None

        fetch_headers: dict[str, str] = {"Accept": "application/json, text/plain, */*"}
        if "Authorization" in headers:
            fetch_headers["Authorization"] = headers["Authorization"]
        if json_body is not None:
            fetch_headers["Content-Type"] = "application/json"

        try:
            fetch_opts: dict[str, Any] = {"method": method, "headers": fetch_headers}
            if json_body is not None:
                fetch_opts["body"] = json.dumps(json_body, ensure_ascii=False)

            js_expr = (
                f"fetch({json.dumps(url)}, {json.dumps(fetch_opts)})"
                ".then(async r => JSON.stringify({status: r.status, text: await r.text()}))"
            )
            res = ws_cdp_call(
                ws_url,
                "Runtime.evaluate",
                {"expression": js_expr, "awaitPromise": True, "returnByValue": True},
                timeout=self.timeout,
            )
            raw_val = ((res.get("result") or {}).get("value")) or ""
            if not raw_val:
                return None
            parsed_resp = json.loads(raw_val)
            status = int(parsed_resp.get("status") or 0)
            body_text = str(parsed_resp.get("text") or "")
            if 200 <= status < 300:
                return json.loads(body_text) if body_text.strip() else {}
            if status == 403 and ("The request could not be satisfied" in body_text or "CloudFront" in body_text):
                return None
            raise OlxApiError(status, body_text[:500], url)
        except OlxApiError:
            raise
        except Exception:
            return None

    def request_chat_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call OLX Ukraine Seller/Buyer Chat API (`https://api.chat.olx.ua/api/...`)."""
        url = path if path.startswith("http") else f"{CHAT_BASE_URL}{path}"
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean_params:
                qs = urllib.parse.urlencode(clean_params, doseq=True)
                url = f"{url}{'&' if '?' in url else '?'}{qs}"

        headers = self._headers(
            require_auth=True,
            extra={
                "X-Site-Code": "olxua",
                "X-Client": "DESKTOP",
                "X-Api-Version": "2",
                "Origin": "https://www.olx.ua",
                "Referer": "https://www.olx.ua/",
            },
        )
        body_bytes: bytes | None = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                if not raw_bytes:
                    return {}
                return json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                pass
            raise OlxApiError(exc.code, err_body or str(exc.reason), url) from exc
        except urllib.error.URLError as exc:
            raise OlxApiError(503, f"Network error: {exc.reason}", url) from exc

    def search_offers(
        self,
        query: str,
        *,
        category_id: int | None = None,
        city_id: int | None = None,
        region_id: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        currency: str = "UAH",
        condition: str | None = None,
        sort_by: str = "relevance",
        only_with_delivery: bool = False,
        offset: int = 0,
        limit: int = 40,
    ) -> dict[str, Any]:
        """Search public offers on `www.olx.ua/api/v1/offers/`."""
        params: dict[str, Any] = {
            "offset": max(0, int(offset)),
            "limit": max(1, min(50, int(limit))),
            "query": query.strip(),
            "sort_by": SORT_BY_MAP.get(sort_by, sort_by),
        }
        if category_id is not None:
            params["category_id"] = int(category_id)
        if city_id is not None:
            params["city_id"] = int(city_id)
        if region_id is not None:
            params["region_id"] = int(region_id)
        if min_price is not None:
            params["filter_float_price:from"] = int(min_price) if float(min_price).is_integer() else min_price
        if max_price is not None:
            params["filter_float_price:to"] = int(max_price) if float(max_price).is_integer() else max_price
        if currency:
            params["currency"] = currency.upper()
        if condition in ("used", "new"):
            params["filter_enum_state[0]"] = condition
        if only_with_delivery:
            params["delivery_scheduled"] = "true"

        raw = self.request_json("GET", "/api/v1/offers/", params=params)
        items = raw.get("data") or []
        for item in items:
            if isinstance(item, dict) and item.get("id") is not None:
                _RAW_OFFER_CACHE[int(item["id"])] = item
        offers = [parse_offer(item) for item in items if isinstance(item, dict)]
        metadata = raw.get("metadata") or {}
        total_elements = int(metadata.get("total_elements") or metadata.get("visible_total_count") or len(offers))
        return {
            "query": query,
            "total_available": total_elements,
            "returned_count": len(offers),
            "offers": offers,
        }

    def get_offer(self, offer_id: int) -> OfferSummary:
        """Fetch full details for a single offer ID via `/api/v1/offers/{id}/` (or search cache)."""
        oid = int(offer_id)
        if oid in _RAW_OFFER_CACHE:
            return parse_offer(_RAW_OFFER_CACHE[oid], max_desc_len=2500)
        raw = self.request_json("GET", f"/api/v1/offers/{oid}/")
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        if isinstance(data, dict) and data.get("id") is not None:
            _RAW_OFFER_CACHE[int(data["id"])] = data
        return parse_offer(data, max_desc_len=2500)

    def resolve_location(self, city_query: str) -> dict[str, Any]:
        """Resolve a Ukrainian/English city name to OLX `city_id` and `region_id`."""
        q = city_query.strip().lower()
        if q in OLX_UA_CITIES:
            return {"matched": True, "source": "builtin_dictionary", **OLX_UA_CITIES[q]}
        for key, info in OLX_UA_CITIES.items():
            if q in key or key in q:
                return {"matched": True, "source": "builtin_dictionary_partial", **info}

        # Fallback: sample 5 offers with the city name to extract exact location IDs from OLX response
        res = self.request_json("GET", "/api/v1/offers/", params={"query": city_query, "limit": 10})
        seen_cities: list[dict[str, Any]] = []
        for item in res.get("data") or []:
            loc = item.get("location") or {}
            city = loc.get("city") or {}
            reg = loc.get("region") or {}
            if city.get("id") and city.get("name"):
                entry = {
                    "city_id": city.get("id"),
                    "name": city.get("name"),
                    "region_id": reg.get("id"),
                    "region_name": reg.get("name"),
                }
                if entry not in seen_cities:
                    seen_cities.append(entry)
                if q in str(city.get("name", "")).lower():
                    return {"matched": True, "source": "live_location_extract", **entry}
        return {"matched": False, "query": city_query, "candidates": seen_cities[:5]}
