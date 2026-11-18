---
description: Search, benchmark prices, manage per-lot handoff dossiers, and haggle with sellers on olx.ua
argument-hint: "[status | sync | <item or PC build description>]"
tags: [mcp, ops]
uses: [olx-buyer]
---

# /olx-buyer

Load the `olx-buyer` skill and execute the OLX buyer & negotiation workflow for **"$ARGUMENTS"**.

## Routing by `$ARGUMENTS`

1. **No arguments, `status`, or `sync` (`/olx-buyer` | `/olx-buyer sync`)**
   - Call `get_lot_handoff` (no arguments) on `olx-mcp` to load `~/.config/olx-mcp/lots/INDEX.md` and all active per-lot handoff dossiers.
   - Call `sync_seller_replies` to poll `https://api.chat.olx.ua/api/conversations` for new seller messages or counter-offers.
   - Call `present_ready_deals` and print the active negotiation board and any ready-to-buy deals in Ukrainian.

2. **Specific Offer ID (`/olx-buyer <offer_id> [instruction...]`)**
   - Call `get_lot_handoff(offer_id=<offer_id>)` to restore the full dossier (`~/.config/olx-mcp/lots/lot-<offer_id>.md`).
   - Execute the requested action (e.g. send an approved counter-offer via `send_seller_message` or update the dossier via `save_lot_handoff`).

3. **New Item, Real-Estate Radius Search, or Multi-Component Build (`/olx-buyer <item, apartment near metro/district, or build description>`)**
   - Decompose into component slots (plus a bundle campaign if assembling hardware) and call `create_buy_campaign` / `benchmark_market_price` (pass `near_address`, `radius_km`, and `district` when filtering by metro station, street, or neighborhood).
   - Round all opening offers and target prices to multiples of `50 UAH` (ending in `00` or `50`).
   - For the top 1–3 candidates per slot, save a per-lot handoff dossier via `save_lot_handoff`.
   - Present the Pre-Haggle Pitch (current price vs market median, forecasted discount rounded to `50`/`100 UAH`, distance/walking time if geo-filtered, draft Ukrainian message, and OLX URL) and wait for the user's approval before calling `send_seller_message`.
