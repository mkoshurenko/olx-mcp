"""Authentication vault (`auth.json`, mode `0600`), Interactive Google/Browser Login via Chrome CDP, & Token Auto-Refresh.

Supports 4 zero-friction ways to authenticate with `olx.ua`:
1. `browser_login` (Recommended): Launches `/Applications/Google Chrome.app` with a dedicated persistent profile
   (`~/.config/olx-mcp/chrome-profile`) and remote debugging on localhost. The user clicks "Продовжити через Google"
   (or logs in once), and `AuthVault` automatically reads `access_token`, `refresh_token`, and session cookies
   via Chrome DevTools Protocol (`Network.getAllCookies` + `Runtime.evaluate` localStorage) and saves them to `auth.json`.
2. `parse_curl_or_headers`: Paste any `Copy as cURL` from DevTools — automatically extracts `Authorization: Bearer ...` and `Cookie: ...`.
3. Direct `access_token` / `cookie` / `refresh_token` via `set_auth_credentials`.
4. Automatic token renewal (`refresh_access_token`) when `refresh_token` is stored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olx_mcp import DEFAULT_AUTH_PATH, DEFAULT_STATE_DIR
from olx_mcp.client import DEFAULT_USER_AGENT, OlxApiError, OlxClient

CHROME_MAC_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_COOKIE_TOKEN_RE = re.compile(r"(?:^|;\s*)(?:access_token|a_access_token)=([^;]+)")
_REFRESH_TOKEN_RE = re.compile(r"(?:^|;\s*)(?:refresh_token|r_refresh_token)=([^;]+)")
_BEARER_RE = re.compile(r"authorization['\"]?\s*[:=]\s*['\"]?Bearer\s+([A-Za-z0-9._\-]+)", re.IGNORECASE)
_CURL_COOKIE_RE = re.compile(r"(?:-b|--cookie|-H\s+['\"]cookie:)\s*['\"]?([^'\"]+)", re.IGNORECASE)


def mask_secret(val: str | None) -> str:
    if not val:
        return ""
    s = val.strip()
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-4:]} (len={len(s)})"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _ws_cdp_call(ws_url: str, method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
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

        # Read frames until id == 1
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


def parse_curl_or_raw_auth(raw_input: str) -> dict[str, str]:
    """Extract `access_token`, `refresh_token`, and `cookie` from a pasted cURL command, JSON, or raw string."""
    text = raw_input.strip()
    extracted: dict[str, str] = {}

    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                if obj.get("access_token"):
                    extracted["access_token"] = str(obj["access_token"]).strip()
                if obj.get("refresh_token"):
                    extracted["refresh_token"] = str(obj["refresh_token"]).strip()
                if obj.get("cookie"):
                    extracted["cookie"] = str(obj["cookie"]).strip()
                if extracted:
                    return extracted
        except Exception:
            pass

    m_bearer = _BEARER_RE.search(text)
    if m_bearer:
        extracted["access_token"] = m_bearer.group(1).strip()

    m_cookie = _CURL_COOKIE_RE.search(text)
    if m_cookie:
        extracted["cookie"] = m_cookie.group(1).strip()
    elif "=" in text and ";" in text and "curl " not in text.lower():
        extracted["cookie"] = text

    cookie_src = extracted.get("cookie") or text
    m_tok = _COOKIE_TOKEN_RE.search(cookie_src)
    if m_tok and "access_token" not in extracted:
        extracted["access_token"] = urllib.parse.unquote(m_tok.group(1).strip())

    m_ref = _REFRESH_TOKEN_RE.search(cookie_src)
    if m_ref and "refresh_token" not in extracted:
        extracted["refresh_token"] = urllib.parse.unquote(m_ref.group(1).strip())

    if not extracted and text and " " not in text and len(text) > 16:
        extracted["access_token"] = text

    return extracted


class AuthVault:
    """Secure file-backed store for OLX.ua tokens, session cookies, and Chrome CDP login."""

    def __init__(self, auth_path: Path = DEFAULT_AUTH_PATH) -> None:
        self.auth_path = Path(auth_path)
        self.chrome_profile_dir = self.auth_path.parent / "chrome-profile"

    def load(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.auth_path.exists():
            try:
                data = json.loads(self.auth_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        env_token = os.environ.get("OLX_ACCESS_TOKEN", "").strip()
        env_cookie = os.environ.get("OLX_COOKIE", "").strip()
        if env_token:
            data["access_token"] = env_token
        if env_cookie:
            data["cookie"] = env_cookie

        if not data.get("access_token") and data.get("cookie"):
            m = _COOKIE_TOKEN_RE.search(str(data["cookie"]))
            if m:
                data["access_token"] = urllib.parse.unquote(m.group(1).strip())

        if not data.get("refresh_token") and data.get("cookie"):
            m = _REFRESH_TOKEN_RE.search(str(data["cookie"]))
            if m:
                data["refresh_token"] = urllib.parse.unquote(m.group(1).strip())

        return data

    def save(
        self,
        *,
        access_token: str | None = None,
        cookie: str | None = None,
        refresh_token: str | None = None,
        raw_curl_or_headers: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        current = self.load()

        if raw_curl_or_headers:
            parsed = parse_curl_or_raw_auth(raw_curl_or_headers)
            access_token = access_token or parsed.get("access_token")
            cookie = cookie or parsed.get("cookie")
            refresh_token = refresh_token or parsed.get("refresh_token")

        if access_token is not None:
            tok = access_token.strip()
            if tok.lower().startswith("bearer "):
                tok = tok[7:].strip()
            current["access_token"] = tok
        if cookie is not None:
            current["cookie"] = cookie.strip()
            if not current.get("access_token"):
                m = _COOKIE_TOKEN_RE.search(current["cookie"])
                if m:
                    current["access_token"] = urllib.parse.unquote(m.group(1).strip())
            if not current.get("refresh_token"):
                m = _REFRESH_TOKEN_RE.search(current["cookie"])
                if m:
                    current["refresh_token"] = urllib.parse.unquote(m.group(1).strip())
        if refresh_token is not None:
            current["refresh_token"] = refresh_token.strip()
        if note is not None:
            current["note"] = note.strip()
        current["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.auth_path.parent, prefix=".auth.", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.auth_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

        return self.status_summary(verify_live=False)

    def refresh_access_token(self, *, base_url: str = "https://www.olx.ua") -> dict[str, Any]:
        """Refresh the OLX OAuth `access_token` using a stored `refresh_token` or persistent headless Chrome profile."""
        creds = self.load()
        refresh_tok = creds.get("refresh_token")
        if refresh_tok:
            url = f"{base_url.rstrip('/')}/api/open/oauth/token/"
            payload: dict[str, Any] = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_tok,
            }
            if creds.get("client_id"):
                payload["client_id"] = creds["client_id"]
            if creds.get("client_secret"):
                payload["client_secret"] = creds["client_secret"]

            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if creds.get("cookie"):
                headers["Cookie"] = str(creds["cookie"])

            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=12.0) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                new_access = data.get("access_token")
                new_refresh = data.get("refresh_token") or refresh_tok
                if new_access:
                    self.save(
                        access_token=str(new_access),
                        refresh_token=str(new_refresh),
                        note="Auto-refreshed via OAuth refresh_token",
                    )
                    return {"refreshed": True, "method": "oauth_refresh_token", "access_token_masked": mask_secret(str(new_access))}
            except Exception:
                pass

        if self.chrome_profile_dir.exists():
            res = self.interactive_browser_login(
                timeout_seconds=15,
                login_url=f"{base_url.rstrip('/')}/uk/myaccount/",
                headless=True,
                verify_after_save=False,
            )
            if res.get("authenticated"):
                return {
                    "refreshed": True,
                    "method": "headless_chrome_profile",
                    "access_token_masked": res.get("access_token_masked"),
                }

        return {"refreshed": False, "reason": "No valid refresh_token or active Chrome profile session"}

    def interactive_browser_login(
        self,
        *,
        timeout_seconds: int = 180,
        login_url: str = "https://www.olx.ua/uk/myaccount/",
        chrome_binary: str = CHROME_MAC_PATH,
        headless: bool = False,
        verify_after_save: bool = True,
    ) -> dict[str, Any]:
        """Launch Google Chrome at OLX login page (supports 'Continue with Google' or headless silent refresh), wait for fresh token, and save to `auth.json`."""
        if not Path(chrome_binary).exists():
            return {
                "authenticated": False,
                "error": f"Google Chrome not found at {chrome_binary}",
            }

        self.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_file = self.chrome_profile_dir / lock_name
            if lock_file.exists() or lock_file.is_symlink():
                try:
                    lock_file.unlink(missing_ok=True)
                except Exception:
                    pass

        port = _find_free_port()
        cmd = [
            chrome_binary,
            f"--user-data-dir={self.chrome_profile_dir}",
            f"--user-agent={DEFAULT_USER_AGENT}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if headless:
            cmd.append("--headless=new")
            cmd.append("https://www.olx.ua/robots.txt")
        else:
            cmd.append("--new-window")
            cmd.append(login_url)

        existing_creds = self.load()
        saved_cookie_pairs = [
            p.strip().split("=", 1)
            for p in str(existing_creds.get("cookie") or "").split(";")
            if "=" in p
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + max(8, int(timeout_seconds))
        harvested_token: str | None = None
        harvested_refresh: str | None = None
        harvested_cookie_str: str | None = None
        cookies_injected = False

        try:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2.0) as resp:
                        targets = json.loads(resp.read().decode("utf-8", errors="replace"))
                    page_targets = [
                        t for t in targets
                        if isinstance(t, dict)
                        and t.get("type") == "page"
                        and t.get("webSocketDebuggerUrl")
                    ]
                    for target in page_targets:
                        ws_url = str(target["webSocketDebuggerUrl"])
                        if headless and not cookies_injected:
                            cdp_cookies = []
                            for k, v in saved_cookie_pairs:
                                if k in ("access_token", "a_access_token", "olx-auth-access-token"):
                                    continue
                                for dom in (".olx.ua", "www.olx.ua", "login.olx.ua"):
                                    cdp_cookies.append({"name": k, "value": v, "domain": dom, "path": "/", "secure": True})
                            if cdp_cookies:
                                _ws_cdp_call(ws_url, "Network.setCookies", {"cookies": cdp_cookies})
                            _ws_cdp_call(ws_url, "Page.navigate", {"url": login_url})
                            cookies_injected = True
                            time.sleep(2.5)

                        cookie_res = _ws_cdp_call(ws_url, "Network.getAllCookies")
                        cookies = [
                            c for c in (cookie_res.get("cookies") or [])
                            if isinstance(c, dict) and "olx" in str(c.get("domain") or "").lower()
                        ]
                        merged_cookie_map = {k: v for k, v in saved_cookie_pairs}
                        for c in cookies:
                            if c.get("name") and c.get("value"):
                                merged_cookie_map[str(c["name"])] = str(c["value"])
                        cookie_map = {str(c.get("name")): str(c.get("value")) for c in cookies if c.get("name")}

                        ls_map: dict[str, Any] = {}
                        if "olx.ua" in str(target.get("url") or ""):
                            ls_res = _ws_cdp_call(
                                ws_url,
                                "Runtime.evaluate",
                                {
                                    "expression": "JSON.stringify(Object.assign({}, window.localStorage, window.sessionStorage))",
                                    "returnByValue": True,
                                },
                            )
                            ls_raw = ((ls_res.get("result") or {}).get("value")) or "{}"
                            try:
                                ls_map = json.loads(ls_raw) if isinstance(ls_raw, str) else {}
                            except Exception:
                                ls_map = {}

                        tok = (
                            cookie_map.get("access_token")
                            or cookie_map.get("a_access_token")
                            or cookie_map.get("olx-auth-access-token")
                            or ls_map.get("access_token")
                            or ls_map.get("a_access_token")
                        )
                        if not tok:
                            for k, v in ls_map.items():
                                if "token" in str(k).lower() and isinstance(v, str):
                                    if v.startswith("eyJ") and len(v) > 30:
                                        tok = v
                                        break
                                    if v.startswith("{") and "access_token" in v:
                                        try:
                                            inner = json.loads(v)
                                            if isinstance(inner, dict) and inner.get("access_token"):
                                                tok = str(inner["access_token"])
                                                break
                                        except Exception:
                                            pass

                        ref_tok = (
                            cookie_map.get("refresh_token")
                            or cookie_map.get("r_refresh_token")
                            or ls_map.get("refresh_token")
                            or ls_map.get("r_refresh_token")
                        )
                        if tok:
                            clean_tok = urllib.parse.unquote(str(tok))
                            if self._is_token_fresh(clean_tok):
                                harvested_token = clean_tok
                                if ref_tok:
                                    harvested_refresh = urllib.parse.unquote(str(ref_tok))
                                harvested_cookie_str = "; ".join(f"{k}={v}" for k, v in merged_cookie_map.items())
                                break
                    if harvested_token:
                        break
                except Exception:
                    pass
                time.sleep(1.2)
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if not harvested_token:
            return {
                "authenticated": False,
                "message": (
                    "Browser closed or timed out before a fresh authenticated OLX `access_token` was detected. "
                    "Make sure US VPN is off so olx.ua loads, log in via Google or phone/email, and wait 2 seconds on the My Account page."
                ),
            }

        self.save(
            access_token=harvested_token,
            cookie=harvested_cookie_str,
            refresh_token=harvested_refresh,
            note="Harvested automatically via Chrome CDP" + (" (silent refresh)" if headless else ""),
        )
        return {
            "authenticated": True,
            **self.status_summary(verify_live=verify_after_save),
        }

    def _is_token_fresh(self, token: str | None, min_ttl_seconds: int = 30) -> bool:
        if not token:
            return False
        claims = self._decode_jwt_claims(token)
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp) > (time.time() + min_ttl_seconds)
        return True

    def build_client(self, *, base_url: str = "https://www.olx.ua", auto_refresh: bool = True) -> OlxClient:
        creds = self.load()
        tok = creds.get("access_token")
        if auto_refresh and tok and not self._is_token_fresh(tok):
            ref = self.refresh_access_token(base_url=base_url)
            if ref.get("refreshed"):
                creds = self.load()
        return OlxClient(
            base_url=base_url,
            access_token=creds.get("access_token"),
            cookie=creds.get("cookie"),
        )

    def _decode_jwt_claims(self, token: str | None) -> dict[str, Any]:
        if not token or "." not in token:
            return {}
        try:
            parts = token.strip().split(".")
            if len(parts) < 2:
                return {}
            pad = "=" * (-len(parts[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8", errors="replace"))
        except Exception:
            return {}

    def status_summary(self, *, verify_live: bool = True, client: OlxClient | None = None) -> dict[str, Any]:
        if verify_live and client is None:
            cli = self.build_client(auto_refresh=True)
        else:
            cli = client

        creds = self.load()
        raw_tok = creds.get("access_token")
        has_token = bool(raw_tok)
        has_cookie = bool(creds.get("cookie"))
        claims = self._decode_jwt_claims(raw_tok)
        exp_ts = claims.get("exp")
        exp_iso = (
            datetime.fromtimestamp(int(exp_ts), tz=timezone.utc).isoformat(timespec="seconds")
            if isinstance(exp_ts, (int, float))
            else None
        )
        summary: dict[str, Any] = {
            "configured": has_token or has_cookie,
            "auth_path": str(self.auth_path),
            "access_token_masked": mask_secret(raw_tok),
            "refresh_token_masked": mask_secret(creds.get("refresh_token")),
            "cookie_masked": mask_secret(creds.get("cookie")),
            "user_uuid": claims.get("sub"),
            "token_expires_at": exp_iso,
            "updated_at": creds.get("updated_at"),
        }
        if not (has_token or has_cookie):
            summary["how_to_authenticate"] = (
                "Option 1 (Automatic Google / Browser Login): Call `browser_login` — it opens a Chrome window at olx.ua where you click 'Продовжити через Google', and automatically captures your token & cookies.\n"
                "Option 2 (Paste cURL / Cookie): Call `set_auth_credentials(raw_curl_or_headers='...')` with a 'Copy as cURL' from Chrome DevTools."
            )
            return summary

        if verify_live:
            assert cli is not None
            try:
                conv_res = cli.request_chat_json("GET", "/api/conversations", params={"offset": 0, "limit": 1})
                convs = conv_res.get("data") or []
                first_conv = convs[0] if convs and isinstance(convs[0], dict) else {}
                summary["live_check"] = "authenticated"
                summary["user"] = {
                    "id": first_conv.get("user_id"),
                    "uuid": claims.get("sub"),
                    "active_conversations_sample": len(convs),
                }
            except OlxApiError as exc:
                if exc.status_code == 401 and client is None:
                    ref_res = self.refresh_access_token(base_url=cli.base_url)
                    if ref_res.get("refreshed"):
                        cli = self.build_client(base_url=cli.base_url, auto_refresh=False)
                        conv_res = cli.request_chat_json("GET", "/api/conversations", params={"offset": 0, "limit": 1})
                        convs = conv_res.get("data") or []
                        first_conv = convs[0] if convs and isinstance(convs[0], dict) else {}
                        creds = self.load()
                        claims = self._decode_jwt_claims(creds.get("access_token"))
                        summary["access_token_masked"] = mask_secret(creds.get("access_token"))
                        summary["live_check"] = f"authenticated (auto-refreshed via {ref_res.get('method')})"
                        summary["user"] = {
                            "id": first_conv.get("user_id"),
                            "uuid": claims.get("sub"),
                        }
                        return summary
                summary["live_check"] = f"failed (HTTP {exc.status_code})"
                summary["error"] = exc.message
        return summary
