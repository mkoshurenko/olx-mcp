"""SQLite CRM for OLX Buying Campaigns, Candidate Deals & Multi-Turn Negotiations.

Tracks:
1. `campaigns`: Smart search filters + target budget & negotiation anchors.
2. `deals`: Individual OLX offers tied to a campaign, tracking price drops and negotiation stages
   (`new` -> `shortlisted` -> `contacted` -> `negotiating` -> `agreed` / `rejected`).
3. `negotiation_log`: Every message sent/received per offer so the agent never double-messages
   a seller and always knows the latest counter-offer.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olx_mcp import DEFAULT_DB_PATH
from olx_mcp.models import OfferSummary

VALID_DEAL_STAGES: tuple[str, ...] = (
    "new",
    "shortlisted",
    "contacted",
    "negotiating",
    "agreed",
    "rejected",
    "ignored",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BuyerStore:
    """SQLite state store for OLX buying campaigns, deals, price history, negotiation logs, and per-lot handoff dossiers."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH, lots_dir: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lots_dir = Path(lots_dir) if lots_dir is not None else (self.db_path.parent / "lots")
        self.lots_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    query TEXT NOT NULL,
                    category_id INTEGER,
                    city_id INTEGER,
                    region_id INTEGER,
                    min_price REAL,
                    max_price REAL,
                    target_buy_price REAL,
                    currency TEXT NOT NULL DEFAULT 'UAH',
                    condition TEXT,
                    only_with_delivery INTEGER NOT NULL DEFAULT 0,
                    exclude_keywords_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    last_scanned_at TEXT
                );

                CREATE TABLE IF NOT EXISTS deals (
                    offer_id INTEGER PRIMARY KEY,
                    campaign_id INTEGER,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    seller_id INTEGER,
                    seller_name TEXT,
                    city TEXT,
                    condition TEXT,
                    is_negotiable INTEGER NOT NULL DEFAULT 0,
                    has_delivery INTEGER NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'UAH',
                    initial_price REAL,
                    current_price REAL,
                    our_last_offer REAL,
                    seller_counter_price REAL,
                    agreed_price REAL,
                    deal_score REAL NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'new',
                    thread_id TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id INTEGER NOT NULL,
                    price REAL NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY (offer_id) REFERENCES deals(offer_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS negotiation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id INTEGER NOT NULL,
                    direction TEXT NOT NULL, -- 'outbound' | 'inbound' | 'note'
                    offered_price REAL,
                    message_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (offer_id) REFERENCES deals(offer_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS lot_handoffs (
                    offer_id INTEGER PRIMARY KEY,
                    build_slot TEXT NOT NULL DEFAULT '',
                    why_shortlisted TEXT NOT NULL DEFAULT '',
                    target_price REAL,
                    walk_away_price REAL,
                    risks_and_questions TEXT NOT NULL DEFAULT '',
                    next_step TEXT NOT NULL DEFAULT '',
                    handoff_path TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (offer_id) REFERENCES deals(offer_id) ON DELETE CASCADE
                );
                """
            )

    def create_campaign(
        self,
        *,
        name: str,
        query: str,
        category_id: int | None = None,
        city_id: int | None = None,
        region_id: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        target_buy_price: float | None = None,
        currency: str = "UAH",
        condition: str | None = None,
        only_with_delivery: bool = False,
        exclude_keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        excl_json = json.dumps(exclude_keywords or [], ensure_ascii=False)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO campaigns (
                    name, query, category_id, city_id, region_id,
                    min_price, max_price, target_buy_price, currency,
                    condition, only_with_delivery, exclude_keywords_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    name.strip(),
                    query.strip(),
                    category_id,
                    city_id,
                    region_id,
                    min_price,
                    max_price,
                    target_buy_price,
                    currency.upper(),
                    condition,
                    1 if only_with_delivery else 0,
                    excl_json,
                    now,
                ),
            )
            cid = int(cur.lastrowid or 0)
        return self.get_campaign(cid)

    def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (int(campaign_id),)).fetchone()
            if not row:
                raise KeyError(f"Campaign #{campaign_id} not found")
            return self._row_to_campaign(row)

    def list_campaigns(self, *, status: str | None = "active") -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status and status != "all":
                rows = conn.execute(
                    "SELECT * FROM campaigns WHERE status = ? ORDER BY id DESC", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
            return [self._row_to_campaign(r) for r in rows]

    def delete_campaign(self, campaign_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            dropped = conn.execute(
                "DELETE FROM deals WHERE campaign_id = ? AND stage = 'new'", (int(campaign_id),)
            ).rowcount
            conn.execute("DELETE FROM campaigns WHERE id = ?", (int(campaign_id),))
        return {"deleted_campaign_id": int(campaign_id), "dropped_new_deals": dropped}

    def purge_orphan_new_deals(self) -> int:
        """Drop untouched `new` deals whose campaign no longer exists. Deals past `new` are kept."""
        with self._connect() as conn:
            return conn.execute(
                "DELETE FROM deals WHERE stage = 'new' "
                "AND (campaign_id IS NULL OR campaign_id NOT IN (SELECT id FROM campaigns))"
            ).rowcount

    def _row_to_campaign(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["only_with_delivery"] = bool(d.get("only_with_delivery"))
        try:
            d["exclude_keywords"] = json.loads(d.pop("exclude_keywords_json", "[]"))
        except Exception:
            d["exclude_keywords"] = []
        return d

    def upsert_deals_from_scan(
        self,
        campaign_id: int | None,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Record candidate offers from a market scan, detecting brand-new listings and price drops."""
        now = _utc_now()
        new_deals: list[dict[str, Any]] = []
        price_drops: list[dict[str, Any]] = []
        updated_count = 0

        with self._connect() as conn:
            if campaign_id is not None:
                conn.execute(
                    "UPDATE campaigns SET last_scanned_at = ? WHERE id = ?",
                    (now, int(campaign_id)),
                )

            for cand in candidates:
                oid = int(cand["id"])
                price = cand.get("price")
                score = float(cand.get("deal_score") or 0.0)
                existing = conn.execute(
                    "SELECT offer_id, initial_price, current_price, stage FROM deals WHERE offer_id = ?",
                    (oid,),
                ).fetchone()

                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO deals (
                            offer_id, campaign_id, title, url, seller_id, seller_name,
                            city, condition, is_negotiable, has_delivery, currency,
                            initial_price, current_price, deal_score, stage,
                            first_seen_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                        """,
                        (
                            oid,
                            campaign_id,
                            cand.get("title") or "",
                            cand.get("url") or "",
                            cand.get("seller_id"),
                            cand.get("seller_name") or "",
                            cand.get("city") or "",
                            cand.get("condition") or "unknown",
                            1 if cand.get("is_negotiable") else 0,
                            1 if cand.get("has_delivery") else 0,
                            cand.get("currency") or "UAH",
                            price,
                            price,
                            score,
                            now,
                            now,
                        ),
                    )
                    if price is not None:
                        conn.execute(
                            "INSERT INTO price_history (offer_id, price, recorded_at) VALUES (?, ?, ?)",
                            (oid, float(price), now),
                        )
                    new_deals.append(cand)
                else:
                    old_price = existing["current_price"]
                    if price is not None and old_price is not None and float(price) < float(old_price):
                        price_drops.append(
                            {
                                "id": oid,
                                "title": cand.get("title"),
                                "old_price": old_price,
                                "new_price": price,
                                "drop_amount": round(float(old_price) - float(price), 2),
                                "url": cand.get("url"),
                            }
                        )
                    if price is not None and old_price != price:
                        conn.execute(
                            "INSERT INTO price_history (offer_id, price, recorded_at) VALUES (?, ?, ?)",
                            (oid, float(price), now),
                        )
                    conn.execute(
                        """
                        UPDATE deals
                           SET campaign_id = COALESCE(?, campaign_id),
                               current_price = COALESCE(?, current_price),
                               deal_score = ?,
                               updated_at = ?
                         WHERE offer_id = ?
                        """,
                        (campaign_id, price, score, now, oid),
                    )
                    updated_count += 1

        return {
            "new_count": len(new_deals),
            "price_drop_count": len(price_drops),
            "updated_existing_count": updated_count,
            "new_deals": new_deals[:10],
            "price_drops": price_drops,
        }

    def ensure_deal_from_offer(self, offer: OfferSummary, campaign_id: int | None = None) -> dict[str, Any]:
        """Ensure an `OfferSummary` exists in `deals` before logging messages or stage updates."""
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM deals WHERE offer_id = ?", (offer.id,)).fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO deals (
                        offer_id, campaign_id, title, url, seller_id, seller_name,
                        city, condition, is_negotiable, has_delivery, currency,
                        initial_price, current_price, deal_score, stage,
                        first_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 50.0, 'shortlisted', ?, ?)
                    """,
                    (
                        offer.id,
                        campaign_id,
                        offer.title,
                        offer.url,
                        offer.seller_id,
                        offer.seller_name,
                        offer.city,
                        offer.condition,
                        1 if offer.is_negotiable else 0,
                        1 if offer.has_delivery else 0,
                        offer.currency,
                        offer.price_value,
                        offer.price_value,
                        now,
                        now,
                    ),
                )
        return self.get_deal(offer.id)

    def update_deal_negotiation(
        self,
        offer_id: int,
        *,
        stage: str | None = None,
        our_last_offer: float | None = None,
        seller_counter_price: float | None = None,
        agreed_price: float | None = None,
        thread_id: str | None = None,
        notes: str | None = None,
        log_direction: str | None = None,
        log_message: str | None = None,
    ) -> dict[str, Any]:
        """Update a deal's negotiation status, prices, and conversation log."""
        if stage is not None and stage not in VALID_DEAL_STAGES:
            raise ValueError(f"Invalid stage '{stage}'. Valid stages: {VALID_DEAL_STAGES}")

        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM deals WHERE offer_id = ?", (int(offer_id),)).fetchone()
            if not row:
                raise KeyError(
                    f"Offer #{offer_id} is not tracked yet. Call `get_offer` or `scan_campaign_deals` first."
                )

            conn.execute(
                """
                UPDATE deals
                   SET stage = COALESCE(?, stage),
                       our_last_offer = COALESCE(?, our_last_offer),
                       seller_counter_price = COALESCE(?, seller_counter_price),
                       agreed_price = COALESCE(?, agreed_price),
                       thread_id = COALESCE(?, thread_id),
                       notes = CASE WHEN ? IS NOT NULL THEN ? ELSE notes END,
                       updated_at = ?
                 WHERE offer_id = ?
                """,
                (
                    stage,
                    our_last_offer,
                    seller_counter_price,
                    agreed_price,
                    thread_id,
                    notes,
                    notes,
                    now,
                    int(offer_id),
                ),
            )

            if log_message:
                direction = log_direction or ("outbound" if our_last_offer else "note")
                offered_val = our_last_offer if direction == "outbound" else seller_counter_price
                conn.execute(
                    """
                    INSERT INTO negotiation_log (offer_id, direction, offered_price, message_text, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (int(offer_id), direction, offered_val, log_message.strip(), now),
                )

        self._sync_lot_handoff_if_present(int(offer_id))
        return self.get_deal(int(offer_id))

    def get_deal(self, offer_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM deals WHERE offer_id = ?", (int(offer_id),)).fetchone()
            if not row:
                raise KeyError(f"Deal for offer #{offer_id} not found")
            logs = conn.execute(
                "SELECT direction, offered_price, message_text, created_at FROM negotiation_log WHERE offer_id = ? ORDER BY id ASC",
                (int(offer_id),),
            ).fetchall()
            history = conn.execute(
                "SELECT price, recorded_at FROM price_history WHERE offer_id = ? ORDER BY id ASC",
                (int(offer_id),),
            ).fetchall()
            h_row = conn.execute(
                "SELECT * FROM lot_handoffs WHERE offer_id = ?",
                (int(offer_id),),
            ).fetchone()
            d = dict(row)
            d["is_negotiable"] = bool(d.get("is_negotiable"))
            d["has_delivery"] = bool(d.get("has_delivery"))
            d["negotiation_log"] = [dict(x) for x in logs]
            d["price_history"] = [dict(x) for x in history]
            d["handoff"] = dict(h_row) if h_row else None
            return d

    def _write_atomic_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _sync_lot_handoff_if_present(self, offer_id: int) -> None:
        with self._connect() as conn:
            h_row = conn.execute(
                "SELECT offer_id FROM lot_handoffs WHERE offer_id = ?",
                (int(offer_id),),
            ).fetchone()
        if h_row is not None:
            self.save_lot_handoff(int(offer_id))

    def save_lot_handoff(
        self,
        offer_id: int,
        *,
        build_slot: str | None = None,
        why_shortlisted: str | None = None,
        target_price: float | None = None,
        walk_away_price: float | None = None,
        risks_and_questions: str | None = None,
        next_step: str | None = None,
        stage: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Persist a per-lot handoff dossier in SQLite + `~/.config/olx-mcp/lots/lot-<offer_id>.md` + `lots/INDEX.md`."""
        oid = int(offer_id)
        if stage is not None or notes is not None:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE deals
                       SET stage = COALESCE(?, stage),
                           notes = CASE WHEN ? IS NOT NULL THEN ? ELSE notes END,
                           updated_at = ?
                     WHERE offer_id = ?
                    """,
                    (stage, notes, notes, _utc_now(), oid),
                )

        deal = self.get_deal(oid)
        now = _utc_now()
        lot_md_path = self.lots_dir / f"lot-{oid}.md"

        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM lot_handoffs WHERE offer_id = ?", (oid,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO lot_handoffs (
                        offer_id, build_slot, why_shortlisted, target_price,
                        walk_away_price, risks_and_questions, next_step,
                        handoff_path, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        oid,
                        (build_slot or "").strip(),
                        (why_shortlisted or "").strip(),
                        target_price,
                        walk_away_price,
                        (risks_and_questions or "").strip(),
                        (next_step or "").strip(),
                        str(lot_md_path),
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE lot_handoffs
                       SET build_slot = CASE WHEN ? IS NOT NULL THEN ? ELSE build_slot END,
                           why_shortlisted = CASE WHEN ? IS NOT NULL THEN ? ELSE why_shortlisted END,
                           target_price = COALESCE(?, target_price),
                           walk_away_price = COALESCE(?, walk_away_price),
                           risks_and_questions = CASE WHEN ? IS NOT NULL THEN ? ELSE risks_and_questions END,
                           next_step = CASE WHEN ? IS NOT NULL THEN ? ELSE next_step END,
                           handoff_path = ?,
                           updated_at = ?
                     WHERE offer_id = ?
                    """,
                    (
                        build_slot,
                        (build_slot or "").strip(),
                        why_shortlisted,
                        (why_shortlisted or "").strip(),
                        target_price,
                        walk_away_price,
                        risks_and_questions,
                        (risks_and_questions or "").strip(),
                        next_step,
                        (next_step or "").strip(),
                        str(lot_md_path),
                        now,
                        oid,
                    ),
                )

        deal = self.get_deal(oid)
        handoff = deal.get("handoff") or {}
        md_text = self._render_lot_handoff_md(deal, handoff)
        self._write_atomic_text(lot_md_path, md_text)
        self._refresh_lots_index_md()

        return {
            "offer_id": oid,
            "handoff_path": str(lot_md_path),
            "index_path": str(self.lots_dir / "INDEX.md"),
            "handoff": handoff,
            "deal": deal,
            "markdown": md_text,
        }

    def get_lot_handoff(self, offer_id: int) -> dict[str, Any]:
        """Read the persistent handoff dossier for a single offer ID."""
        oid = int(offer_id)
        deal = self.get_deal(oid)
        handoff = deal.get("handoff")
        lot_md_path = self.lots_dir / f"lot-{oid}.md"
        if handoff is None or not lot_md_path.exists():
            return self.save_lot_handoff(oid)
        return {
            "offer_id": oid,
            "handoff_path": str(lot_md_path),
            "index_path": str(self.lots_dir / "INDEX.md"),
            "handoff": handoff,
            "deal": deal,
            "markdown": lot_md_path.read_text(encoding="utf-8"),
        }

    def list_lot_handoffs(self, *, campaign_id: int | None = None, stage: str | None = None) -> dict[str, Any]:
        """List all saved per-lot handoff dossiers and return the master `INDEX.md` content."""
        clauses: list[str] = []
        params: list[Any] = []
        if campaign_id is not None:
            clauses.append("d.campaign_id = ?")
            params.append(int(campaign_id))
        if stage and stage != "all":
            clauses.append("d.stage = ?")
            params.append(stage)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT d.offer_id, d.campaign_id, d.title, d.url, d.seller_name, d.city,
                       d.stage, d.initial_price, d.current_price, d.our_last_offer,
                       d.seller_counter_price, d.agreed_price, d.currency, d.thread_id,
                       h.build_slot, h.why_shortlisted, h.target_price, h.walk_away_price,
                       h.risks_and_questions, h.next_step, h.handoff_path, h.updated_at
                  FROM lot_handoffs h
                  JOIN deals d ON d.offer_id = h.offer_id
                  {where_sql}
                 ORDER BY h.updated_at DESC
                """,
                params,
            ).fetchall()

        index_path = self.lots_dir / "INDEX.md"
        if not index_path.exists():
            self._refresh_lots_index_md()

        return {
            "count": len(rows),
            "index_path": str(index_path),
            "index_markdown": index_path.read_text(encoding="utf-8") if index_path.exists() else "",
            "lots": [dict(r) for r in rows],
        }

    def _render_lot_handoff_md(self, deal: dict[str, Any], handoff: dict[str, Any]) -> str:
        oid = deal["offer_id"]
        cur = deal.get("currency") or "UAH"
        lines: list[str] = [
            f"# Lot Handoff #{oid} — {deal.get('title')}",
            "",
            f"- **Updated At**: `{handoff.get('updated_at') or deal.get('updated_at')}`",
            f"- **Build Slot / Role**: {handoff.get('build_slot') or 'General'}",
            f"- **Stage**: `{deal.get('stage')}`",
            f"- **OLX URL**: {deal.get('url')}",
            f"- **Seller**: {deal.get('seller_name') or 'Unknown'} ({deal.get('city') or 'Україна'})",
            f"- **OLX Delivery**: {'Yes' if deal.get('has_delivery') else 'No'} | **Negotiable ('Торг')**: {'Yes' if deal.get('is_negotiable') else 'No'}",
            f"- **Chat Thread ID**: `{deal.get('thread_id') or 'not started'}`",
            "",
            "## Pricing & Haggling Anchors",
            f"- **Initial Asking Price**: `{deal.get('initial_price')} {cur}`",
            f"- **Current Asking Price**: `{deal.get('current_price')} {cur}`",
            f"- **Our Target Buy Price**: `{handoff.get('target_price') or '—'} {cur}`",
            f"- **Walk-Away Ceiling**: `{handoff.get('walk_away_price') or '—'} {cur}`",
            f"- **Our Last Offer Sent**: `{deal.get('our_last_offer') or '—'} {cur}`",
            f"- **Seller Counter-Offer**: `{deal.get('seller_counter_price') or '—'} {cur}`",
            f"- **Agreed Final Price**: `{deal.get('agreed_price') or '—'} {cur}`",
            "",
            "## Context & Evaluation",
            f"- **Why Shortlisted**: {handoff.get('why_shortlisted') or deal.get('notes') or '—'}",
            f"- **Risks / Questions to Verify**: {handoff.get('risks_and_questions') or '—'}",
            f"- **Next Step**: {handoff.get('next_step') or '—'}",
            "",
            "## Negotiation Log",
        ]
        logs = deal.get("negotiation_log") or []
        if not logs:
            lines.append("- *(No messages sent or received yet)*")
        else:
            for entry in logs:
                p_tag = f" [price: {entry['offered_price']} {cur}]" if entry.get("offered_price") is not None else ""
                lines.append(
                    f"- `{entry.get('created_at')}` **{str(entry.get('direction', '')).upper()}**{p_tag}: {entry.get('message_text')}"
                )

        lines.extend(["", "## Price History"])
        for ph in deal.get("price_history") or []:
            lines.append(f"- `{ph.get('recorded_at')}`: `{ph.get('price')} {cur}`")
        lines.append("")
        return "\n".join(lines)

    def _refresh_lots_index_md(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.offer_id, d.title, d.stage, d.current_price, d.our_last_offer,
                       d.seller_counter_price, d.agreed_price, d.currency, d.url,
                       h.build_slot, h.target_price, h.next_step, h.handoff_path, h.updated_at
                  FROM lot_handoffs h
                  JOIN deals d ON d.offer_id = h.offer_id
                 ORDER BY h.updated_at DESC
                """
            ).fetchall()

        lines: list[str] = [
            "# OLX Tracked Lots — Handoff Index",
            "",
            "| Offer ID | Slot | Title | Stage | Current | Our Offer | Seller Counter | Agreed | Next Step | Dossier |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for r in rows:
            cur = r["currency"] or "UAH"
            oid = r["offer_id"]
            lines.append(
                f"| `{oid}` | {r['build_slot'] or '—'} | [{r['title'][:40]}]({r['url']}) | `{r['stage']}` | "
                f"{r['current_price']} {cur} | {r['our_last_offer'] or '—'} | {r['seller_counter_price'] or '—'} | "
                f"**{r['agreed_price'] or '—'}** | {r['next_step'] or '—'} | [lot-{oid}.md](file://{r['handoff_path']}) |"
            )
        lines.append("")
        self._write_atomic_text(self.lots_dir / "INDEX.md", "\n".join(lines))

    def get_negotiation_board(
        self,
        *,
        campaign_id: int | None = None,
        stage: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Return a Kanban-style overview of all tracked deals and active negotiations."""
        clauses: list[str] = []
        params: list[Any] = []
        if campaign_id is not None:
            clauses.append("campaign_id = ?")
            params.append(int(campaign_id))
        if stage and stage != "all":
            clauses.append("stage = ?")
            params.append(stage)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM deals {where_sql} ORDER BY deal_score DESC, updated_at DESC LIMIT ?"
        params.append(max(1, min(100, int(limit))))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            stage_counts_rows = conn.execute(
                "SELECT stage, COUNT(*) as cnt FROM deals GROUP BY stage"
            ).fetchall()

        stage_counts = {r["stage"]: r["cnt"] for r in stage_counts_rows}
        deals: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["is_negotiable"] = bool(item.get("is_negotiable"))
            item["has_delivery"] = bool(item.get("has_delivery"))
            deals.append(item)

        return {
            "total_tracked": sum(stage_counts.values()),
            "stage_counts": stage_counts,
            "deals": deals,
        }
