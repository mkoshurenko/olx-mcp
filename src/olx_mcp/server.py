#!/usr/bin/env python3
"""MCP Server (`olx-mcp`) — OLX Ukraine AI Buyer & Deal Negotiator.

Exposes 16 tools over stdio JSON-RPC 2.0 (and CLI `--self-test`):
1. Discovery & Market Price Benchmarking:
   - `search_offers`
   - `get_offer`
   - `benchmark_market_price`
   - `resolve_location`
   - `list_categories`
2. Buying Campaigns & Negotiation CRM (SQLite):
   - `create_buy_campaign`
   - `list_buy_campaigns`
   - `delete_buy_campaign`
   - `scan_campaign_deals`
   - `update_deal_negotiation`
   - `get_negotiation_board`
3. Seller Chat, Haggling & Personal Account (Auth):
   - `set_auth_credentials`
   - `auth_status`
   - `list_threads`
   - `get_thread_messages`
   - `send_seller_message`
   - `list_my_offers`
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import tempfile
import termios
import uuid
from pathlib import Path
from typing import Any

SRC_ROOT = Path(__file__).resolve().parent.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from olx_mcp import DEFAULT_AUTH_PATH, DEFAULT_DB_PATH
from olx_mcp.auth import AuthVault
from olx_mcp.client import OLX_UA_CATEGORIES, OlxApiError, OlxClient
from olx_mcp.models import parse_offer
from olx_mcp.pricing import benchmark_offers, compute_haggling_leverage
from olx_mcp.store import BuyerStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("olx_mcp")


def ensure_tty_onlcr() -> None:
    for s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if s.isatty():
                a = termios.tcgetattr(s.fileno())
                if not (a[1] & termios.ONLCR):
                    a[1] |= termios.ONLCR
                    termios.tcsetattr(s.fileno(), termios.TCSANOW, a)
        except Exception:
            pass


class OlxBuyerService:
    """Core service coordinating OLX API client, Price Benchmarking, Auth Vault, and SQLite CRM."""

    def __init__(
        self,
        *,
        db_path: Path = DEFAULT_DB_PATH,
        auth_path: Path = DEFAULT_AUTH_PATH,
        client: OlxClient | None = None,
    ) -> None:
        self.store = BuyerStore(db_path=db_path)
        self.auth = AuthVault(auth_path=auth_path)
        self._override_client = client

    def _client(self) -> OlxClient:
        if self._override_client is not None:
            return self._override_client
        return self.auth.build_client()

    # --- 1. Discovery & Price Benchmarking ---

    def search_offers(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query and args.get("category_id") is None:
            raise ValueError("Provide `query` or `category_id`.")

        city_id = args.get("city_id")
        region_id = args.get("region_id")
        if args.get("city") and city_id is None:
            loc = self._client().resolve_location(str(args["city"]))
            if loc.get("matched"):
                city_id = loc.get("city_id")
                region_id = region_id or loc.get("region_id")

        res = self._client().search_offers(
            query=query,
            category_id=int(args["category_id"]) if args.get("category_id") is not None else None,
            city_id=int(city_id) if city_id is not None else None,
            region_id=int(region_id) if region_id is not None else None,
            min_price=float(args["min_price"]) if args.get("min_price") is not None else None,
            max_price=float(args["max_price"]) if args.get("max_price") is not None else None,
            currency=str(args.get("currency") or "UAH"),
            condition=args.get("condition"),
            sort_by=str(args.get("sort_by") or "relevance"),
            only_with_delivery=bool(args.get("only_with_delivery", False)),
            offset=int(args.get("offset") or 0),
            limit=int(args.get("limit") or 25),
        )
        return {
            "query": res["query"],
            "total_available": res["total_available"],
            "returned_count": res["returned_count"],
            "offers": [o.to_dict() for o in res["offers"]],
        }

    def get_offer(self, args: dict[str, Any]) -> dict[str, Any]:
        offer_id = int(args["offer_id"])
        offer = self._client().get_offer(offer_id)
        if args.get("track_in_board", True):
            self.store.ensure_deal_from_offer(offer, campaign_id=args.get("campaign_id"))
        price = offer.price_value or 0.0
        haggling = compute_haggling_leverage(offer, market_median=price, market_p25=price * 0.9)
        return {
            "offer": offer.to_dict(),
            "haggling": haggling,
        }

    def benchmark_market_price(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("`query` is required for `benchmark_market_price`.")

        city_id = args.get("city_id")
        region_id = args.get("region_id")
        if args.get("city") and city_id is None:
            loc = self._client().resolve_location(str(args["city"]))
            if loc.get("matched"):
                city_id = loc.get("city_id")
                region_id = region_id or loc.get("region_id")

        res = self._client().search_offers(
            query=query,
            category_id=int(args["category_id"]) if args.get("category_id") is not None else None,
            city_id=int(city_id) if city_id is not None else None,
            region_id=int(region_id) if region_id is not None else None,
            min_price=float(args["min_price"]) if args.get("min_price") is not None else None,
            max_price=float(args["max_price"]) if args.get("max_price") is not None else None,
            currency=str(args.get("currency") or "UAH"),
            condition=args.get("condition"),
            sort_by="relevance",
            only_with_delivery=bool(args.get("only_with_delivery", False)),
            limit=int(args.get("sample_size") or 40),
        )
        return benchmark_offers(
            res["offers"],
            query=query,
            target_currency=str(args.get("currency") or "UAH"),
            exclude_keywords=args.get("exclude_keywords"),
            min_valid_price=float(args["min_valid_price"]) if args.get("min_valid_price") is not None else None,
        )

    def resolve_location(self, args: dict[str, Any]) -> dict[str, Any]:
        city = str(args.get("city") or "").strip()
        if not city:
            raise ValueError("`city` is required.")
        return self._client().resolve_location(city)

    def list_categories(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "categories": [
                {"id": cid, "name": name} for cid, name in sorted(OLX_UA_CATEGORIES.items())
            ]
        }

    # --- 2. Buying Campaigns & Negotiation CRM ---

    def create_buy_campaign(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("`query` is required.")
        name = str(args.get("name") or query).strip()

        city_id = args.get("city_id")
        region_id = args.get("region_id")
        if args.get("city") and city_id is None:
            loc = self._client().resolve_location(str(args["city"]))
            if loc.get("matched"):
                city_id = loc.get("city_id")
                region_id = region_id or loc.get("region_id")

        campaign = self.store.create_campaign(
            name=name,
            query=query,
            category_id=int(args["category_id"]) if args.get("category_id") is not None else None,
            city_id=int(city_id) if city_id is not None else None,
            region_id=int(region_id) if region_id is not None else None,
            min_price=float(args["min_price"]) if args.get("min_price") is not None else None,
            max_price=float(args["max_price"]) if args.get("max_price") is not None else None,
            target_buy_price=float(args["target_buy_price"]) if args.get("target_buy_price") is not None else None,
            currency=str(args.get("currency") or "UAH"),
            condition=args.get("condition"),
            only_with_delivery=bool(args.get("only_with_delivery", False)),
            exclude_keywords=list(args.get("exclude_keywords") or []),
        )
        if args.get("auto_scan", True):
            scan = self.scan_campaign_deals({"campaign_id": campaign["id"]})
            return {"campaign": campaign, "initial_scan": scan}
        return {"campaign": campaign}

    def list_buy_campaigns(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        params = args or {}
        rows = self.store.list_campaigns(status=params.get("status", "active"))
        return {"count": len(rows), "campaigns": rows}

    def delete_buy_campaign(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.store.delete_campaign(int(args["campaign_id"]))

    def scan_campaign_deals(self, args: dict[str, Any]) -> dict[str, Any]:
        cid = int(args["campaign_id"])
        camp = self.store.get_campaign(cid)
        bench = self.benchmark_market_price(
            {
                "query": camp["query"],
                "category_id": camp.get("category_id"),
                "city_id": camp.get("city_id"),
                "region_id": camp.get("region_id"),
                "min_price": camp.get("min_price"),
                "max_price": camp.get("max_price"),
                "currency": camp.get("currency") or "UAH",
                "condition": camp.get("condition"),
                "only_with_delivery": camp.get("only_with_delivery", False),
                "exclude_keywords": camp.get("exclude_keywords") or None,
                "sample_size": int(args.get("limit") or 40),
            }
        )
        diff = self.store.upsert_deals_from_scan(cid, bench.get("top_candidates") or [])
        return {
            "campaign": camp,
            "market_stats": bench.get("stats"),
            "haggling_strategy": bench.get("haggling_strategy"),
            "scan_delta": diff,
            "top_candidates": (bench.get("top_candidates") or [])[:10],
        }

    def update_deal_negotiation(self, args: dict[str, Any]) -> dict[str, Any]:
        oid = int(args["offer_id"])
        try:
            self.store.get_deal(oid)
        except KeyError:
            off = self._client().get_offer(oid)
            self.store.ensure_deal_from_offer(off, campaign_id=args.get("campaign_id"))

        return self.store.update_deal_negotiation(
            oid,
            stage=args.get("stage"),
            our_last_offer=float(args["our_last_offer"]) if args.get("our_last_offer") is not None else None,
            seller_counter_price=float(args["seller_counter_price"]) if args.get("seller_counter_price") is not None else None,
            agreed_price=float(args["agreed_price"]) if args.get("agreed_price") is not None else None,
            thread_id=args.get("thread_id"),
            notes=args.get("notes"),
            log_direction=args.get("log_direction"),
            log_message=args.get("log_message"),
        )

    def get_negotiation_board(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        params = args or {}
        return self.store.get_negotiation_board(
            campaign_id=int(params["campaign_id"]) if params.get("campaign_id") is not None else None,
            stage=params.get("stage"),
            limit=int(params.get("limit") or 30),
        )

    def save_lot_handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        """Save or update a persistent per-lot handoff dossier (`lots/lot-<offer_id>.md` + SQLite + `lots/INDEX.md`)."""
        oid = int(args["offer_id"])
        try:
            self.store.get_deal(oid)
        except KeyError:
            off = self._client().get_offer(oid)
            self.store.ensure_deal_from_offer(off, campaign_id=args.get("campaign_id"))

        return self.store.save_lot_handoff(
            oid,
            build_slot=args.get("build_slot"),
            why_shortlisted=args.get("why_shortlisted"),
            target_price=float(args["target_price"]) if args.get("target_price") is not None else None,
            walk_away_price=float(args["walk_away_price"]) if args.get("walk_away_price") is not None else None,
            risks_and_questions=args.get("risks_and_questions"),
            next_step=args.get("next_step"),
            stage=args.get("stage"),
            notes=args.get("notes"),
        )

    def get_lot_handoff(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read a single lot's handoff dossier (`offer_id`) or list all active lot handoffs + master `INDEX.md`."""
        params = args or {}
        if params.get("offer_id") is not None:
            return self.store.get_lot_handoff(int(params["offer_id"]))
        return self.store.list_lot_handoffs(
            campaign_id=int(params["campaign_id"]) if params.get("campaign_id") is not None else None,
            stage=params.get("stage"),
        )

    # --- 3. Auth, Seller Chats & Haggling Outreach ---

    def set_auth_credentials(self, args: dict[str, Any]) -> dict[str, Any]:
        self.auth.save(
            access_token=args.get("access_token"),
            cookie=args.get("cookie"),
            refresh_token=args.get("refresh_token"),
            raw_curl_or_headers=args.get("raw_curl_or_headers"),
            note=args.get("note"),
        )
        return self.auth.status_summary(
            verify_live=bool(args.get("verify_live", True)),
            client=self._override_client,
        )

    def browser_login(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        params = args or {}
        return self.auth.interactive_browser_login(
            timeout_seconds=int(params.get("timeout_seconds") or 180),
            login_url=str(params.get("login_url") or "https://www.olx.ua/uk/myaccount/"),
        )

    def auth_status(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        params = args or {}
        if params.get("launch_browser_login"):
            return self.auth.interactive_browser_login(
                timeout_seconds=int(params.get("timeout_seconds") or 180),
                login_url=str(params.get("login_url") or "https://www.olx.ua/uk/myaccount/"),
            )
        return self.auth.status_summary(
            verify_live=bool(params.get("verify_live", True)),
            client=self._override_client,
        )

    def list_threads(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        params = args or {}
        limit = max(1, min(50, int(params.get("limit") or 20)))
        offset = max(0, int(params.get("offset") or 0))
        q_params: dict[str, Any] = {"limit": limit, "offset": offset}
        if params.get("offer_id") is not None:
            q_params["ad_id"] = int(params["offer_id"])

        raw = self._client().request_chat_json("GET", "/api/conversations", params=q_params)
        items = raw.get("data") or []
        threads: list[dict[str, Any]] = []
        for t in items:
            if not isinstance(t, dict):
                continue
            ad = t.get("ad") if isinstance(t.get("ad"), dict) else {}
            resp_user = t.get("respondent") if isinstance(t.get("respondent"), dict) else {}
            msgs = t.get("messages") if isinstance(t.get("messages"), list) else []
            last_msg = msgs[-1] if msgs and isinstance(msgs[-1], dict) else (t.get("last_message") or t.get("message"))
            threads.append(
                {
                    "thread_id": t.get("id"),
                    "offer_id": t.get("ad_id") or ad.get("id"),
                    "offer_title": ad.get("title"),
                    "interlocutor_id": t.get("interlocutor_id") or resp_user.get("id"),
                    "interlocutor_name": resp_user.get("name"),
                    "interlocutor_role": resp_user.get("type"),
                    "unread_count": t.get("unread_count", 0),
                    "last_message": (
                        {
                            "id": last_msg.get("id"),
                            "text": last_msg.get("text"),
                            "user_id": last_msg.get("user_id"),
                            "created_at": last_msg.get("created_at"),
                        }
                        if isinstance(last_msg, dict)
                        else last_msg
                    ),
                }
            )
        return {"count": len(threads), "threads": threads}

    def get_thread_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(args.get("thread_id") or "").strip()
        if not thread_id:
            raise ValueError("`thread_id` is required.")
        limit = max(1, min(50, int(args.get("limit") or 25)))
        raw = self._client().request_chat_json(
            "GET",
            f"/api/conversations/{thread_id}",
        )
        conv = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        my_user_id = conv.get("user_id") if isinstance(conv, dict) else None
        items = (conv.get("messages") if isinstance(conv, dict) else None) or (raw.get("data") if isinstance(raw.get("data"), list) else [])
        messages: list[dict[str, Any]] = []
        for m in items[-limit:]:
            if not isinstance(m, dict):
                continue
            direction = m.get("type") or m.get("direction")
            if not direction and my_user_id is not None and m.get("user_id") is not None:
                direction = "outbound" if int(m["user_id"]) == int(my_user_id) else "inbound"
            messages.append(
                {
                    "id": m.get("id"),
                    "thread_id": m.get("thread_id") or thread_id,
                    "user_id": m.get("user_id"),
                    "type": direction or "inbound",
                    "text": m.get("text") or m.get("body") or "",
                    "created_at": m.get("created_at"),
                    "is_read": m.get("is_read"),
                }
            )
        return {"thread_id": thread_id, "count": len(messages), "messages": messages}

    def send_seller_message(self, args: dict[str, Any]) -> dict[str, Any]:
        """Send an outreach or counter-offer message to a seller on OLX.

        Enforces anti-duplicate protection: if we already sent an outbound message on this
        `offer_id` and `force=False`, blocks duplicate cold outreach unless `thread_id` is
        provided for an ongoing reply. Supports `dry_run=True` to preview and log drafts safely.
        """
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("`text` is required.")

        offer_id = int(args["offer_id"]) if args.get("offer_id") is not None else None
        thread_id = str(args.get("thread_id") or "").strip() or None
        offered_price = float(args["offered_price"]) if args.get("offered_price") is not None else None
        dry_run = bool(args.get("dry_run", False))
        force = bool(args.get("force", False))

        if offer_id is None and thread_id is None:
            raise ValueError("Provide `offer_id` (to message a listing) or `thread_id` (to reply in an existing chat).")

        deal: dict[str, Any] | None = None
        if offer_id is not None:
            try:
                deal = self.store.get_deal(offer_id)
            except KeyError:
                off = self._client().get_offer(offer_id)
                deal = self.store.ensure_deal_from_offer(off)

            if not thread_id and deal.get("thread_id"):
                thread_id = str(deal["thread_id"])

            outbound_logs = [
                entry for entry in (deal.get("negotiation_log") or []) if entry.get("direction") == "outbound"
            ]
            if outbound_logs and not thread_id and not force and not dry_run:
                return {
                    "sent": False,
                    "blocked_reason": "already_contacted_offer",
                    "message": (
                        f"Offer #{offer_id} already has {len(outbound_logs)} outbound message(s) logged. "
                        "Pass `thread_id` to reply in the thread or `force=True` to send another message."
                    ),
                    "deal": deal,
                }

        if dry_run:
            return {
                "sent": False,
                "dry_run": True,
                "offer_id": offer_id,
                "thread_id": thread_id,
                "offered_price": offered_price,
                "preview_text": text,
                "deal": deal,
            }

        msg_envelope = {
            "text": text,
            "message_id": str(uuid.uuid4()),
            "attachments": [],
        }

        # Dispatch message via OLX Ukraine Chat API (`https://api.chat.olx.ua`)
        if thread_id:
            resp = self._client().request_chat_json(
                "POST",
                f"/api/conversations/{thread_id}/messages",
                json_body={"message": msg_envelope},
            )
        else:
            # Look up existing conversation for `offer_id` (`ad_id`) first, or start a new conversation
            try:
                threads_res = self.list_threads({"offer_id": offer_id, "limit": 5})
                existing_threads = [
                    t for t in (threads_res.get("threads") or [])
                    if offer_id is not None and int(t.get("offer_id") or 0) == int(offer_id)
                ]
                if existing_threads and existing_threads[0].get("thread_id"):
                    thread_id = str(existing_threads[0]["thread_id"])
            except Exception:
                pass

            if thread_id:
                resp = self._client().request_chat_json(
                    "POST",
                    f"/api/conversations/{thread_id}/messages",
                    json_body={"message": msg_envelope},
                )
            else:
                resp = self._client().request_chat_json(
                    "POST",
                    "/api/conversations",
                    json_body={"ad_id": offer_id, "message": msg_envelope},
                )
                resp_data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
                if isinstance(resp_data, dict) and (resp_data.get("id") or resp_data.get("conversation_id")):
                    thread_id = str(resp_data.get("id") or resp_data.get("conversation_id"))

        updated_deal = None
        if offer_id is not None:
            next_stage = "negotiating" if offered_price is not None else "contacted"
            updated_deal = self.store.update_deal_negotiation(
                offer_id,
                stage=next_stage,
                our_last_offer=offered_price,
                thread_id=thread_id,
                log_direction="outbound",
                log_message=text,
            )

        return {
            "sent": True,
            "offer_id": offer_id,
            "thread_id": thread_id,
            "offered_price": offered_price,
            "text": text,
            "api_response": resp,
            "deal": updated_deal,
        }

    def list_my_offers(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        params = args or {}
        raw = self._client().request_json(
            "GET",
            "/api/v1/users/me/offers/",
            params={"limit": int(params.get("limit") or 25), "offset": int(params.get("offset") or 0)},
            require_auth=True,
        )
        items = raw.get("data") or []
        offers = [parse_offer(i).to_dict() for i in items if isinstance(i, dict)]
        return {"count": len(offers), "offers": offers}

    def sync_seller_replies(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Poll OLX chat threads, match incoming seller replies to tracked deals, extract counter-offer prices, and update SQLite."""
        params = args or {}
        board = self.store.get_negotiation_board(
            campaign_id=int(params["campaign_id"]) if params.get("campaign_id") is not None else None,
            limit=50,
        )
        active_deals = [
            d for d in (board.get("deals") or [])
            if d.get("stage") in ("contacted", "negotiating", "shortlisted") or d.get("thread_id")
        ]
        threads_res = self.list_threads({"limit": int(params.get("limit") or 30)})
        threads_by_id = {str(t["thread_id"]): t for t in (threads_res.get("threads") or []) if t.get("thread_id")}
        threads_by_offer = {int(t["offer_id"]): t for t in (threads_res.get("threads") or []) if t.get("offer_id")}

        price_re = re.compile(r"\b([1-9]\d{2,5})\s*(?:грн|uah|₴|забер|віддам|давайте|за)?\b", re.IGNORECASE)
        agree_words = ("згоден", "згодна", "домовились", "домовилися", "ок,", "окей", "відправлю", "відправляю", "оформлюйте", "забирайте")

        synced_updates: list[dict[str, Any]] = []
        for deal in active_deals:
            oid = int(deal["offer_id"])
            tid = str(deal.get("thread_id") or "")
            if not tid and oid in threads_by_offer:
                tid = str(threads_by_offer[oid]["thread_id"])
            if not tid or (tid not in threads_by_id and not deal.get("thread_id")):
                continue

            msg_res = self.get_thread_messages({"thread_id": tid, "limit": 15})
            full_deal = self.store.get_deal(oid)
            logged_texts = {
                entry.get("message_text", "").strip()
                for entry in (full_deal.get("negotiation_log") or [])
                if entry.get("direction") == "inbound"
            }

            for m in msg_res.get("messages") or []:
                m_type = str(m.get("type") or "").lower()
                if m_type in ("sent", "outbound", "my"):
                    continue
                txt = str(m.get("text") or "").strip()
                if not txt or txt in logged_texts:
                    continue

                # Extract numeric counter-offer if present and plausible (within 35%..120% of asking price)
                asking = float(full_deal.get("current_price") or full_deal.get("initial_price") or 0)
                detected_price: float | None = None
                for match in price_re.finditer(txt):
                    val = float(match.group(1))
                    if asking <= 0 or (asking * 0.35 <= val <= asking * 1.2):
                        detected_price = val
                        break

                txt_lower = txt.lower()
                is_agreed = any(w in txt_lower for w in agree_words)
                next_stage = "agreed" if is_agreed else "negotiating"
                agreed_val = (
                    (detected_price or full_deal.get("our_last_offer") or asking)
                    if is_agreed
                    else None
                )

                updated = self.store.update_deal_negotiation(
                    oid,
                    stage=next_stage,
                    seller_counter_price=detected_price,
                    agreed_price=agreed_val,
                    thread_id=tid,
                    log_direction="inbound",
                    log_message=txt,
                )
                logged_texts.add(txt)
                synced_updates.append(
                    {
                        "offer_id": oid,
                        "title": updated.get("title"),
                        "seller_name": updated.get("seller_name"),
                        "seller_reply": txt,
                        "detected_counter_price": detected_price,
                        "agreed_price": updated.get("agreed_price"),
                        "stage": updated.get("stage"),
                        "url": updated.get("url"),
                    }
                )

        return {
            "synced_count": len(synced_updates),
            "updates": synced_updates,
        }

    def present_ready_deals(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Generate the final user-facing pitch of negotiated / ready-to-buy deals ('Ось товар і фінальна ціна')."""
        params = args or {}
        campaign_id = int(params["campaign_id"]) if params.get("campaign_id") is not None else None
        board = self.store.get_negotiation_board(campaign_id=campaign_id, limit=50)
        all_deals = board.get("deals") or []

        ready_deals: list[dict[str, Any]] = []
        for d in all_deals:
            oid = int(d["offer_id"])
            full = self.store.get_deal(oid)
            initial_price = float(full.get("initial_price") or full.get("current_price") or 0)
            final_price = (
                full.get("agreed_price")
                or full.get("seller_counter_price")
                or full.get("current_price")
                or initial_price
            )
            final_price_f = float(final_price or 0)
            savings = round(initial_price - final_price_f, 2) if initial_price > 0 and final_price_f > 0 else 0.0
            savings_pct = round((savings / initial_price) * 100.0, 1) if initial_price > 0 else 0.0

            inbound_msgs = [
                entry for entry in (full.get("negotiation_log") or []) if entry.get("direction") == "inbound"
            ]
            last_seller_quote = inbound_msgs[-1]["message_text"] if inbound_msgs else ""

            is_actionable = full.get("stage") in ("agreed", "negotiating") or savings > 0
            if not params.get("include_all", False) and not is_actionable and len(all_deals) > 3:
                continue

            currency = full.get("currency") or "UAH"
            summary_uk = (
                f"Товар: {full.get('title')} ({full.get('city') or 'Україна'}) — "
                f"фінальна ціна: {int(final_price_f)} {currency} "
                f"(була {int(initial_price)} {currency}, знижка -{int(savings)} {currency} / -{savings_pct}%). "
                f"Статус: {full.get('stage')}. "
                + (f"Відповідь продавця ({full.get('seller_name')}): «{last_seller_quote}». " if last_seller_quote else "")
                + f"Посилання: {full.get('url')}"
            )
            ready_deals.append(
                {
                    "offer_id": oid,
                    "title": full.get("title"),
                    "city": full.get("city"),
                    "seller_name": full.get("seller_name"),
                    "stage": full.get("stage"),
                    "initial_price": initial_price,
                    "our_last_offer": full.get("our_last_offer"),
                    "seller_counter_price": full.get("seller_counter_price"),
                    "agreed_price": full.get("agreed_price"),
                    "final_buy_price": final_price_f,
                    "currency": currency,
                    "savings_amount": savings,
                    "savings_pct": savings_pct,
                    "has_delivery": full.get("has_delivery"),
                    "last_seller_reply": last_seller_quote,
                    "url": full.get("url"),
                    "pitch_uk": summary_uk,
                }
            )

        ready_deals.sort(
            key=lambda x: (
                0 if x["stage"] == "agreed" else (1 if x["stage"] == "negotiating" else 2),
                x["final_buy_price"] or 999999999,
            )
        )
        return {
            "ready_count": len(ready_deals),
            "best_deal": ready_deals[0] if ready_deals else None,
            "deals": ready_deals,
        }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_offers",
        "description": "Search public listings on olx.ua with query, category, city, price range, condition, and OLX Delivery filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (e.g. 'MacBook Pro M2 16 512', 'iPhone 15 Pro')."},
                "category_id": {"type": "integer", "description": "Optional OLX category ID (call `list_categories` to see IDs)."},
                "city": {"type": "string", "description": "Optional city name in Ukrainian or English (e.g. 'Київ', 'Львів')."},
                "city_id": {"type": "integer"},
                "region_id": {"type": "integer"},
                "min_price": {"type": "number"},
                "max_price": {"type": "number"},
                "currency": {"type": "string", "default": "UAH", "enum": ["UAH", "USD", "EUR"]},
                "condition": {"type": "string", "enum": ["used", "new"]},
                "sort_by": {"type": "string", "default": "relevance", "enum": ["relevance", "newest", "cheapest", "most_expensive"]},
                "only_with_delivery": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 25},
                "offset": {"type": "integer", "default": 0},
            },
        },
    },
    {
        "name": "get_offer",
        "description": "Fetch full details of a specific OLX offer by ID (full description, seller tenure, negotiable flag, haggling leverage score).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer", "description": "Numeric OLX offer ID."},
                "track_in_board": {"type": "boolean", "default": True, "description": "Automatically add this offer to the SQLite negotiation board."},
                "campaign_id": {"type": "integer"},
            },
            "required": ["offer_id"],
        },
    },
    {
        "name": "benchmark_market_price",
        "description": (
            "Analyze real market prices on olx.ua for an item: strips cheap accessories/cases/broken parts outliers, "
            "computes min/p10/p25/median/p75/max prices, generates a haggling strategy (recommended opening offer & target price), "
            "and ranks the best negotiable candidate deals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Item to benchmark (e.g. 'Sony PlayStation 5 Slim', 'AirPods Pro 2')."},
                "category_id": {"type": "integer"},
                "city": {"type": "string"},
                "min_price": {"type": "number"},
                "max_price": {"type": "number"},
                "min_valid_price": {"type": "number", "description": "Optional floor price to exclude cheap accessories."},
                "currency": {"type": "string", "default": "UAH"},
                "condition": {"type": "string", "enum": ["used", "new"]},
                "only_with_delivery": {"type": "boolean", "default": False},
                "exclude_keywords": {"type": "array", "items": {"type": "string"}},
                "sample_size": {"type": "integer", "default": 40},
            },
            "required": ["query"],
        },
    },
    {
        "name": "resolve_location",
        "description": "Resolve a Ukrainian/English city name to OLX `city_id` and `region_id`.",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "list_categories",
        "description": "List primary OLX.ua root category IDs and names.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_buy_campaign",
        "description": "Create a persistent Buying Campaign (saved filter + target budget) in SQLite and run an initial deal scan & market benchmark.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-friendly campaign title."},
                "query": {"type": "string", "description": "Search query on olx.ua."},
                "category_id": {"type": "integer"},
                "city": {"type": "string"},
                "min_price": {"type": "number"},
                "max_price": {"type": "number"},
                "target_buy_price": {"type": "number", "description": "Your desired purchase price after haggling."},
                "currency": {"type": "string", "default": "UAH"},
                "condition": {"type": "string", "enum": ["used", "new"]},
                "only_with_delivery": {"type": "boolean", "default": False},
                "exclude_keywords": {"type": "array", "items": {"type": "string"}},
                "auto_scan": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_buy_campaigns",
        "description": "List saved Buying Campaigns in SQLite.",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string", "default": "active"}},
        },
    },
    {
        "name": "delete_buy_campaign",
        "description": "Delete a Buying Campaign by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"campaign_id": {"type": "integer"}},
            "required": ["campaign_id"],
        },
    },
    {
        "name": "scan_campaign_deals",
        "description": "Scan olx.ua for a saved Buying Campaign, benchmark prices, detect new listings and price drops, and update the SQLite deal pipeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 40},
            },
            "required": ["campaign_id"],
        },
    },
    {
        "name": "update_deal_negotiation",
        "description": (
            "Update a deal's negotiation stage (`new`, `shortlisted`, `contacted`, `negotiating`, `agreed`, `rejected`, `ignored`), "
            "record our last offer, the seller's counter-offer, agreed price, and append to the negotiation log."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer"},
                "stage": {"type": "string", "enum": ["new", "shortlisted", "contacted", "negotiating", "agreed", "rejected", "ignored"]},
                "our_last_offer": {"type": "number"},
                "seller_counter_price": {"type": "number"},
                "agreed_price": {"type": "number"},
                "thread_id": {"type": "string"},
                "notes": {"type": "string"},
                "log_direction": {"type": "string", "enum": ["outbound", "inbound", "note"]},
                "log_message": {"type": "string"},
            },
            "required": ["offer_id"],
        },
    },
    {
        "name": "get_negotiation_board",
        "description": "View the Kanban negotiation board of all tracked offers, their current vs initial prices, our last offer, and seller counter-offers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "stage": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "browser_login",
        "description": (
            "Open a Google Chrome window at `https://www.olx.ua/uk/myaccount/` so the user can click "
            "'Продовжити через Google' (or log in once). Automatically captures `access_token`, `refresh_token`, "
            "and session cookies via Chrome DevTools Protocol and saves them into `~/.config/olx-mcp/auth.json` (`0600`)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "integer", "default": 180, "description": "How long to wait for the user to finish Google/OLX login."},
                "login_url": {"type": "string", "default": "https://www.olx.ua/uk/myaccount/"},
            },
        },
    },
    {
        "name": "set_auth_credentials",
        "description": (
            "Save OLX.ua authentication credentials into `~/.config/olx-mcp/auth.json` (`0600`). "
            "Accepts a pasted 'Copy as cURL' command (`raw_curl_or_headers`), a Bearer `access_token`, or a `cookie` string."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "raw_curl_or_headers": {"type": "string", "description": "Paste a 'Copy as cURL' from Chrome DevTools or raw headers/cookies."},
                "access_token": {"type": "string", "description": "Bearer access token from OLX API requests or `access_token` cookie."},
                "cookie": {"type": "string", "description": "Full Cookie header from an authenticated olx.ua browser session."},
                "refresh_token": {"type": "string"},
                "note": {"type": "string"},
                "verify_live": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "auth_status",
        "description": "Check whether OLX authentication credentials are configured and valid.",
        "inputSchema": {
            "type": "object",
            "properties": {"verify_live": {"type": "boolean", "default": True}},
        },
    },
    {
        "name": "list_threads",
        "description": "List OLX chat threads (optionally filtered by `offer_id`) to see seller replies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 20},
                "offset": {"type": "integer", "default": 0},
            },
        },
    },
    {
        "name": "get_thread_messages",
        "description": "Read messages inside a specific OLX chat `thread_id`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "limit": {"type": "integer", "default": 25},
            },
            "required": ["thread_id"],
        },
    },
    {
        "name": "send_seller_message",
        "description": (
            "Send a message or price counter-offer to an OLX seller (by `offer_id` or `thread_id`), automatically logging "
            "the message and offered price in the SQLite negotiation board. Includes duplicate-outreach protection and `dry_run` preview."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer", "description": "Target OLX offer ID."},
                "thread_id": {"type": "string", "description": "Existing chat thread ID (if replying)."},
                "text": {"type": "string", "description": "Message text in Ukrainian to send to the seller."},
                "offered_price": {"type": "number", "description": "Numeric price we are proposing in this message (for negotiation tracking)."},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview without sending over network."},
                "force": {"type": "boolean", "default": False, "description": "Allow sending even if an initial message was already logged."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_my_offers",
        "description": "List the authenticated user's own active/inactive OLX offers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 25},
                "offset": {"type": "integer", "default": 0},
            },
        },
    },
    {
        "name": "sync_seller_replies",
        "description": (
            "Poll OLX chat threads, automatically match incoming seller messages to tracked offers, "
            "extract numeric counter-offer/agreed prices in UAH, and update the SQLite negotiation board."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "present_ready_deals",
        "description": (
            "Generate the user-facing summary of negotiated / ready-to-buy deals ('Ось товар і фінальна ціна'): "
            "ranks agreed & counter-offered items, computes exact discount vs initial asking price, quotes the seller's reply, "
            "and provides the direct OLX link to buy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "include_all": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "save_lot_handoff",
        "description": (
            "Save or update a persistent per-lot handoff dossier (`~/.config/olx-mcp/lots/lot-<offer_id>.md` + SQLite + `lots/INDEX.md`) "
            "so context is never lost across sessions. Stores build slot/role, shortlist rationale, target & walk-away prices, "
            "risks/questions for the seller, next step, and full negotiation & price history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer", "description": "OLX offer ID."},
                "build_slot": {"type": "string", "description": "Component slot or role (e.g. 'Dual-Socket Motherboard X99', '2x Xeon CPU', 'GPU 24GB VRAM')."},
                "why_shortlisted": {"type": "string", "description": "Why this lot was selected (e.g. 'Bottom of market, negotiable, OLX Delivery')."},
                "target_price": {"type": "number", "description": "Our target purchase price after haggling."},
                "walk_away_price": {"type": "number", "description": "Maximum price we are willing to pay."},
                "risks_and_questions": {"type": "string", "description": "Technical things to verify with the seller (e.g. 'All 8 RAM slots working? Socket pins straight?')."},
                "next_step": {"type": "string", "description": "Exact next action (e.g. 'Await user OK to offer 6700 UAH' or 'Wait for seller reply')."},
                "stage": {"type": "string", "enum": ["new", "shortlisted", "contacted", "negotiating", "agreed", "rejected", "ignored"]},
                "notes": {"type": "string"},
            },
            "required": ["offer_id"],
        },
    },
    {
        "name": "get_lot_handoff",
        "description": (
            "Read a lot's persistent handoff dossier (`offer_id`) or list all active lot handoffs + master `lots/INDEX.md` "
            "(when `offer_id` is omitted) to restore full context in a new session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer", "description": "Optional specific offer ID. Omit to list all lot handoffs and master INDEX.md."},
                "campaign_id": {"type": "integer"},
                "stage": {"type": "string"},
            },
        },
    },
]

_DEFAULT_SERVICE: OlxBuyerService | None = None


def get_default_service() -> OlxBuyerService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = OlxBuyerService()
    return _DEFAULT_SERVICE


def _text_result(req_id: Any, payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


async def handle_request(request: dict[str, Any], service: OlxBuyerService | None = None) -> dict[str, Any]:
    """Handle one MCP JSON-RPC 2.0 request."""
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "olx-mcp", "version": "1.0.0"},
            },
        }
    if method in ("ping", "shutdown"):
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        arguments = dict(params.get("arguments") or {})
        srv = service or get_default_service()
        dispatch = {
            "search_offers": srv.search_offers,
            "get_offer": srv.get_offer,
            "benchmark_market_price": srv.benchmark_market_price,
            "resolve_location": srv.resolve_location,
            "list_categories": srv.list_categories,
            "create_buy_campaign": srv.create_buy_campaign,
            "list_buy_campaigns": srv.list_buy_campaigns,
            "delete_buy_campaign": srv.delete_buy_campaign,
            "scan_campaign_deals": srv.scan_campaign_deals,
            "update_deal_negotiation": srv.update_deal_negotiation,
            "get_negotiation_board": srv.get_negotiation_board,
            "browser_login": srv.browser_login,
            "set_auth_credentials": srv.set_auth_credentials,
            "auth_status": srv.auth_status,
            "list_threads": srv.list_threads,
            "get_thread_messages": srv.get_thread_messages,
            "send_seller_message": srv.send_seller_message,
            "list_my_offers": srv.list_my_offers,
            "sync_seller_replies": srv.sync_seller_replies,
            "present_ready_deals": srv.present_ready_deals,
            "save_lot_handoff": srv.save_lot_handoff,
            "get_lot_handoff": srv.get_lot_handoff,
        }
        handler = dispatch.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Tool '{tool_name}' not found."},
            }
        try:
            payload = await asyncio.to_thread(handler, arguments)
            return _text_result(req_id, payload)
        except Exception as exc:
            return _text_result(req_id, f"{type(exc).__name__}: {exc}", is_error=True)

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method '{method}' not found."},
    }


async def run_stdio_mcp_server(service: OlxBuyerService | None = None) -> None:
    """Run the stdio JSON-RPC 2.0 loop for MCP clients."""
    logger.info("OLX Buyer MCP Server starting on stdio...")
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    try:
        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            req_id = None
            try:
                request = json.loads(line)
                method = request.get("method")
                if method == "exit":
                    break
                if (
                    "id" not in request
                    or request.get("id") is None
                    or (isinstance(method, str) and method.startswith("notifications/"))
                ):
                    continue
                req_id = request.get("id")
                response = await handle_request(request, service=service)
            except Exception as exc:
                logger.error("Error processing JSON-RPC request: %s", type(exc).__name__)
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    finally:
        logger.info("OLX Buyer MCP Server terminated.")


def _sample_olx_offers_payload() -> dict[str, Any]:
    """Realistic OLX.ua `/api/v1/offers/` payload (Sony WH-1000XM5 + accessory noise) for offline/VPN self-test."""
    return {
        "metadata": {"total_elements": 7},
        "data": [
            {
                "id": 850101,
                "title": "Навушники Sony WH-1000XM5 Black в ідеальному стані, повний комплект",
                "url": "https://www.olx.ua/d/uk/obyavlenie/sony-wh-1000xm5-black-ID850101.html",
                "created_time": "2026-10-25T10:00:00+02:00",
                "last_refresh_time": "2026-11-10T10:00:00+02:00",
                "business": False,
                "description": "<p>Продам власні <b>Sony WH-1000XM5</b>. Купувались у Розетці, є чек та коробка.</p>",
                "params": [
                    {"key": "price", "name": "Ціна", "value": {"value": 9800, "currency": "UAH", "negotiable": True, "type": "price"}},
                    {"key": "state", "name": "Стан", "value": {"key": "used", "label": "Вживане"}},
                ],
                "location": {"city": {"id": 268, "name": "Київ"}, "region": {"id": 25, "name": "Київська область"}},
                "user": {"id": 5001, "name": "Олександр", "created": "2019-04-12T00:00:00+02:00"},
                "delivery": {"rock": {"active": True}},
                "promotion": {"highlighted": False, "top_ad": False},
                "photos": [{"link": "https://ireland.apollo.olxcdn.com/v1/files/sample1/image;s={width}x{height}"}],
            },
            {
                "id": 850102,
                "title": "Sony WH-1000XM5 Silver (як нові, на гарантії)",
                "url": "https://www.olx.ua/d/uk/obyavlenie/sony-wh-1000xm5-silver-ID850102.html",
                "created_time": "2026-11-01T14:20:00+02:00",
                "last_refresh_time": "2026-11-11T14:20:00+02:00",
                "business": False,
                "description": "Стан нових, одягались 5 разів.",
                "params": [
                    {"key": "price", "name": "Ціна", "value": {"value": 10500, "currency": "UAH", "negotiable": True, "type": "price"}},
                    {"key": "state", "name": "Стан", "value": {"key": "used", "label": "Вживане"}},
                ],
                "location": {"city": {"id": 302, "name": "Львів"}, "region": {"id": 14, "name": "Львівська область"}},
                "user": {"id": 5002, "name": "Марта", "created": "2021-08-01T00:00:00+02:00"},
                "delivery": {"rock": {"active": True}},
            },
            {
                "id": 850103,
                "title": "Навушники Sony WH-1000XM5 чорні",
                "url": "https://www.olx.ua/d/uk/obyavlenie/sony-wh-1000xm5-ID850103.html",
                "created_time": "2026-11-05T09:00:00+02:00",
                "business": False,
                "description": "Без нюансів, батарея тримає 30 годин.",
                "params": [
                    {"key": "price", "name": "Ціна", "value": {"value": 9200, "currency": "UAH", "negotiable": True, "type": "price"}},
                    {"key": "state", "name": "Стан", "value": {"key": "used", "label": "Вживане"}},
                ],
                "location": {"city": {"id": 268, "name": "Київ"}, "region": {"id": 25, "name": "Київська область"}},
                "user": {"id": 5003, "name": "Дмитро", "created": "2018-01-10T00:00:00+02:00"},
                "delivery": {"rock": {"active": True}},
            },
            {
                "id": 850104,
                "title": "Нові Sony WH-1000XM5 запаковані з Європи",
                "url": "https://www.olx.ua/d/uk/obyavlenie/sony-wh-1000xm5-new-ID850104.html",
                "created_time": "2026-11-09T12:00:00+02:00",
                "business": True,
                "description": "Нові, пломба не відкривалась.",
                "params": [
                    {"key": "price", "name": "Ціна", "value": {"value": 12800, "currency": "UAH", "negotiable": False, "type": "price"}},
                    {"key": "state", "name": "Стан", "value": {"key": "new", "label": "Нове"}},
                ],
                "location": {"city": {"id": 268, "name": "Київ"}, "region": {"id": 25, "name": "Київська область"}},
                "user": {"id": 5004, "name": "AudioShop", "created": "2020-05-05T00:00:00+02:00"},
                "delivery": {"rock": {"active": True}},
            },
            {
                "id": 850105,
                "title": "Sony WH-1000XM5 Midnight Blue оригінал",
                "url": "https://www.olx.ua/d/uk/obyavlenie/sony-wh-1000xm5-blue-ID850105.html",
                "created_time": "2026-10-18T11:00:00+02:00",
                "business": False,
                "description": "Повний комплект з кейсом і кабелями.",
                "params": [
                    {"key": "price", "name": "Ціна", "value": {"value": 11200, "currency": "UAH", "negotiable": True, "type": "price"}},
                    {"key": "state", "name": "Стан", "value": {"key": "used", "label": "Вживане"}},
                ],
                "location": {"city": {"id": 317, "name": "Одеса"}, "region": {"id": 16, "name": "Одеська область"}},
                "user": {"id": 5005, "name": "Ігор", "created": "2017-11-11T00:00:00+02:00"},
                "delivery": {"rock": {"active": True}},
            },
            {
                "id": 850198,
                "title": "Чохол / кейс для Sony WH-1000XM5",
                "url": "https://www.olx.ua/d/uk/obyavlenie/case-sony-xm5-ID850198.html",
                "created_time": "2026-11-10T11:00:00+02:00",
                "business": True,
                "description": "Твердий захисний чохол.",
                "params": [
                    {"key": "price", "name": "Ціна", "value": {"value": 450, "currency": "UAH", "negotiable": False, "type": "price"}},
                    {"key": "state", "name": "Стан", "value": {"key": "new", "label": "Нове"}},
                ],
                "location": {"city": {"id": 268, "name": "Київ"}, "region": {"id": 25, "name": "Київська область"}},
                "user": {"id": 5098, "name": "CaseStore", "created": "2022-01-01T00:00:00+02:00"},
            },
            {
                "id": 850199,
                "title": "Коробка та кабель від Sony WH-1000XM5 на запчастини",
                "url": "https://www.olx.ua/d/uk/obyavlenie/box-sony-xm5-ID850199.html",
                "created_time": "2026-11-10T11:00:00+02:00",
                "business": False,
                "description": "Тільки коробка.",
                "params": [
                    {"key": "price", "name": "Ціна", "value": {"value": 300, "currency": "UAH", "negotiable": False, "type": "price"}},
                    {"key": "state", "name": "Стан", "value": {"key": "used", "label": "Вживане"}},
                ],
                "location": {"city": {"id": 268, "name": "Київ"}, "region": {"id": 25, "name": "Київська область"}},
                "user": {"id": 5099, "name": "Влад", "created": "2023-01-01T00:00:00+02:00"},
            },
        ],
    }


def run_self_test() -> int:
    """Deterministic self-test verifying Stages 1, 2, and 3 (live OLX API or CloudFront-safe fixture + SQLite CRM + Auth/Chat)."""
    ensure_tty_onlcr()
    print("[1/4] Stage 1: Testing olx.ua search, location resolution & market price benchmarking...", flush=True)
    with tempfile.TemporaryDirectory(prefix="olx_mcp_test_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        srv = OlxBuyerService(
            db_path=tmp_path / "test_buyer.sqlite3",
            auth_path=tmp_path / "test_auth.json",
        )

        loc = srv.resolve_location({"city": "Київ"})
        assert loc["matched"] and loc["city_id"] == 268, f"Expected Kyiv city_id=268, got {loc}"
        print(f"  [PASS] resolve_location('Київ') -> city_id={loc['city_id']}, region_id={loc['region_id']}", flush=True)

        fixture_payload = _sample_olx_offers_payload()
        try:
            bench = srv.benchmark_market_price(
                {
                    "query": "Sony WH-1000XM5",
                    "currency": "UAH",
                    "sample_size": 25,
                }
            )
            mode_label = "live olx.ua"
        except OlxApiError as exc:
            if exc.status_code == 403 and "CloudFront" in exc.message:
                print("  [INFO] US VPN active (CloudFront 403 on www.olx.ua); using verified OLX.ua fixture for self-test", flush=True)

                class FixtureSearchClient(OlxClient):
                    def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
                        if path == "/api/v1/offers/":
                            return fixture_payload
                        if path.startswith("/api/v1/offers/"):
                            oid = int(path.strip("/").split("/")[-1])
                            for item in fixture_payload["data"]:
                                if int(item["id"]) == oid:
                                    return {"data": item}
                        return {}

                srv._override_client = FixtureSearchClient()
                bench = srv.benchmark_market_price(
                    {
                        "query": "Sony WH-1000XM5",
                        "currency": "UAH",
                        "sample_size": 25,
                    }
                )
                mode_label = "fixture (US VPN active)"
            else:
                raise

        assert bench["total_fetched"] > 0, "Expected offers"
        assert bench["valid_count"] > 0 and bench["stats"] is not None, f"Benchmark failed: {bench}"
        stats = bench["stats"]
        strat = bench["haggling_strategy"]
        top_cand = bench["top_candidates"][0]
        print(
            f"  [PASS] benchmark_market_price('Sony WH-1000XM5') [{mode_label}] -> fetched={bench['total_fetched']}, "
            f"valid={bench['valid_count']}, excluded_noise={bench['excluded_count']}, "
            f"p25={stats['p25_good_deal']} UAH, median={stats['median_market']} UAH, "
            f"opening_offer={strat['recommended_opening_offer']} UAH",
            flush=True,
        )

        single = srv.get_offer({"offer_id": top_cand["id"]})
        assert single["offer"]["id"] == top_cand["id"], "get_offer ID mismatch"
        print(
            f"  [PASS] get_offer({top_cand['id']}) -> '{single['offer']['title'][:45]}...' "
            f"({single['offer']['price_value']} {single['offer']['currency']}, "
            f"leverage={single['haggling']['leverage_score']}/100)",
            flush=True,
        )

        print("[2/4] Stage 2: Testing SQLite Buying Campaigns, price-drop detection & Negotiation CRM...", flush=True)
        camp_res = srv.create_buy_campaign(
            {
                "name": "Навушники Sony XM5",
                "query": "Sony WH-1000XM5",
                "city": "Київ",
                "target_buy_price": strat["target_buy_price"],
                "auto_scan": False,
            }
        )
        cid = camp_res["campaign"]["id"]
        scan_delta = srv.store.upsert_deals_from_scan(cid, bench["top_candidates"][:5])
        assert scan_delta["new_count"] >= 4, f"Expected new deals inserted, got {scan_delta}"

        # Simulate a seller dropping the price by 800 UAH on the top candidate
        dropped_candidate = dict(top_cand)
        dropped_candidate["price"] = max(100.0, float(top_cand["price"]) - 800.0)
        rescan_delta = srv.store.upsert_deals_from_scan(cid, [dropped_candidate])
        assert rescan_delta["price_drop_count"] == 1, f"Expected 1 price drop, got {rescan_delta}"
        print(
            f"  [PASS] Campaign #{cid} scan + price-drop detector verified "
            f"({top_cand['price']} -> {dropped_candidate['price']} UAH)",
            flush=True,
        )

        print("[3/4] Stage 3: Testing Auth Vault, cURL Parser, Seller Haggling Flow & Anti-Duplicate Guard...", flush=True)
        auth_saved = srv.set_auth_credentials(
            {
                "raw_curl_or_headers": (
                    "curl 'https://www.olx.ua/api/v1/users/me/' "
                    "-H 'authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.curl_payload.sig123' "
                    "-b 'olx_lang=uk; refresh_token=ref_tok_987654321'"
                ),
                "note": "Self-test cURL token",
                "verify_live": False,
            }
        )
        assert auth_saved["configured"] is True and "eyJh..." in auth_saved["access_token_masked"], (
            f"Auth vault failed: {auth_saved}"
        )
        assert "ref_..." in auth_saved["refresh_token_masked"], f"Refresh token parse failed: {auth_saved}"
        mode_bits = (tmp_path / "test_auth.json").stat().st_mode & 0o777
        assert mode_bits == 0o600, f"Expected 0600 on auth.json, got {oct(mode_bits)}"

        cand_open = top_cand["haggling"]["suggested_opening_offer"]
        cand_target = top_cand["haggling"]["suggested_target_price"]

        # Dry-run message preview
        dry = srv.send_seller_message(
            {
                "offer_id": top_cand["id"],
                "offered_price": cand_open,
                "text": f"Доброго дня! Готовий забрати сьогодні через OLX Доставку за {cand_open} грн. Домовимось?",
                "dry_run": True,
            }
        )
        assert dry["dry_run"] is True and dry["sent"] is False

        # Mock transport for live send + anti-duplicate check + sync_seller_replies
        class MockChatClient(OlxClient):
            def request_chat_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
                if path == "/api/conversations" and method == "GET":
                    if (kwargs.get("params") or {}).get("ad_id"):
                        return {"data": []}
                    return {"data": [{"id": "thread_998877", "ad": {"id": top_cand["id"]}, "unread_count": 1}]}
                if path == "/api/conversations" and method == "POST":
                    return {"data": {"id": "thread_998877", "ad": {"id": top_cand["id"]}}}
                if path.endswith("/messages") and method == "POST":
                    return {"data": {"id": "msg_1001", "thread_id": "thread_998877"}}
                if path == "/api/conversations/thread_998877" and method == "GET":
                    return {
                        "data": {
                            "id": "thread_998877",
                            "user_id": 100,
                            "messages": [
                                {
                                    "id": "msg_1002",
                                    "user_id": 200,
                                    "text": f"Домовились, забирайте за {int(cand_target)} грн через OLX доставку!",
                                    "created_at": "2026-11-12T20:55:00+02:00",
                                }
                            ],
                        }
                    }
                return {}

        srv._override_client = MockChatClient(access_token="test_token")
        sent = srv.send_seller_message(
            {
                "offer_id": top_cand["id"],
                "offered_price": cand_open,
                "text": f"Доброго дня! Заберу за {cand_open} грн.",
            }
        )
        assert sent["sent"] is True and sent["thread_id"] == "thread_998877", f"Unexpected send result: {sent}"
        assert sent["deal"]["stage"] == "negotiating"

        # Verify duplicate cold-outreach block when thread_id is omitted
        with srv.store._connect() as conn:
            conn.execute("UPDATE deals SET thread_id = NULL WHERE offer_id = ?", (top_cand["id"],))
        dup = srv.send_seller_message(
            {
                "offer_id": top_cand["id"],
                "text": "Дубль повідомлення без треду",
            }
        )
        assert dup["sent"] is False and dup["blocked_reason"] == "already_contacted_offer", (
            f"Expected duplicate guard to trigger, got {dup}"
        )

        # Verify per-lot handoff dossier (`save_lot_handoff` & `get_lot_handoff`)
        saved_handoff = srv.save_lot_handoff(
            {
                "offer_id": top_cand["id"],
                "build_slot": "Headphones / Audio",
                "why_shortlisted": "Bottom of market, negotiable, OLX Delivery active",
                "target_price": cand_target,
                "walk_away_price": stats["median_market"],
                "risks_and_questions": "Check earcup hinge and battery life",
                "next_step": "Await seller reply or buy at agreed price",
            }
        )
        assert Path(saved_handoff["handoff_path"]).exists(), f"Missing lot handoff file: {saved_handoff}"
        assert Path(saved_handoff["index_path"]).exists(), f"Missing lots INDEX.md: {saved_handoff}"

        # Auto-sync seller replies (`sync_seller_replies`) and build final user pitch (`present_ready_deals`)
        synced = srv.sync_seller_replies({"campaign_id": cid})
        assert synced["synced_count"] == 1 and synced["updates"][0]["stage"] == "agreed", f"Sync failed: {synced}"

        # Verify handoff markdown auto-updated after sync_seller_replies
        loaded_handoff = srv.get_lot_handoff({"offer_id": top_cand["id"]})
        assert "agreed" in loaded_handoff["markdown"] and "INBOUND" in loaded_handoff["markdown"], (
            f"Handoff markdown did not auto-sync: {loaded_handoff['markdown']}"
        )
        all_handoffs = srv.get_lot_handoff({})
        assert all_handoffs["count"] == 1, f"Expected 1 lot handoff in index, got {all_handoffs}"

        ready = srv.present_ready_deals({"campaign_id": cid})
        assert ready["ready_count"] >= 1 and ready["best_deal"]["stage"] == "agreed", f"Ready deals failed: {ready}"
        print(
            f"  [PASS] sync_seller_replies, lot handoff dossier & present_ready_deals verified:\n"
            f"         -> {ready['best_deal']['pitch_uk']}",
            flush=True,
        )

        print("[4/4] Verifying JSON-RPC 2.0 MCP dispatcher (`tools/list` & `tools/call`)...", flush=True)
        srv._override_client = None
        init_resp = asyncio.run(handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, service=srv))
        tools_resp = asyncio.run(handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, service=srv))
        tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
        assert len(tool_names) == 22, f"Expected 22 tools, got {len(tool_names)}: {tool_names}"
        assert init_resp["result"]["serverInfo"]["name"] == "olx-mcp"
        print(f"  [PASS] MCP JSON-RPC exposes {len(tool_names)} tools cleanly", flush=True)

    print("=== DONE (all 4 steps passed) ===", flush=True)
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(run_self_test())
    if "--login" in sys.argv:
        ensure_tty_onlcr()
        print("Launching Chrome for OLX Google/Browser Login (up to 180s)...", flush=True)
        res = get_default_service().browser_login({"timeout_seconds": 180})
        print(json.dumps(res, indent=2, ensure_ascii=False), flush=True)
        sys.exit(0 if res.get("authenticated") else 1)
    asyncio.run(run_stdio_mcp_server())
