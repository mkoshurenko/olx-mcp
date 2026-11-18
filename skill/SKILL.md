---
name: olx-buyer
description: Searches, benchmarks market prices, maintains per-lot handoff dossiers, and negotiates deals on olx.ua via the olx-mcp server. Use when buying an item or assembling a multi-part PC/hardware build on OLX, benchmarking Ukrainian market prices, haggling with sellers, polling seller chat replies, or resuming OLX lot handoffs across sessions.
---

# OLX Buyer & Deal Negotiator (`olx-mcp`)

Drive single-item purchases and multi-component hardware builds on `olx.ua` with statistical price benchmarking, human-approved haggling, and persistent per-lot handoff dossiers.

## State & File Locations

| Path | Purpose |
| :--- | :--- |
| `~/.config/olx-mcp/lots/INDEX.md` | Master table of all tracked lots, current stage, prices, and links to per-lot dossiers |
| `~/.config/olx-mcp/lots/lot-<OFFER_ID>.md` | Per-lot handoff dossier: build slot, shortlist rationale, target/walk-away prices, seller questions, chat log, price history |
| `~/.config/olx-mcp/olx_buyer.sqlite3` | SQLite CRM (`campaigns`, `deals`, `price_history`, `negotiation_log`, `lot_handoffs`) |
| `~/.config/olx-mcp/auth.json` | Mode `0600` vault storing Cognito `access_token`, session cookies (`SID`, `IDPState`, `auth0...`), and optional `google_maps_api_key` |
| `~/.config/olx-mcp/geocache.json` | Persistent geocode cache for Google Maps Geocoding API & OpenStreetMap Nominatim |
| `src/olx_mcp/server.py` (this repo) | MCP server implementation (23 tools) |
| `src/olx_mcp/geo.py` (this repo) | Street/ЖК/metro extractor, Google Maps + Nominatim geocoder, and Haversine radius filter |

## Resuming Context in a New Session

1. Call `get_lot_handoff` with no arguments first — reads `~/.config/olx-mcp/lots/INDEX.md` and returns all active lot dossiers.
2. Call `sync_seller_replies` to poll `https://api.chat.olx.ua/api/conversations`, match incoming seller replies to tracked deals, extract counter-offer prices, and auto-update `lot-<OFFER_ID>.md`.
3. Call `present_ready_deals` to summarize agreed deals, counter-offers, and exact UAH/percentage discounts.

## 4-Step Buying & Negotiation Workflow

### Step 1: Decompose & Benchmark (`create_buy_campaign` + `benchmark_market_price`)
- Single item → create one campaign with `create_buy_campaign(query=..., city=..., target_buy_price=...)`.
- Real estate / local radius search (e.g., apartment within 1 km of a metro station or in a specific district) → pass `near_address="метро Осокорки, Київ"`, `radius_km=1.0`, and/or `district="Дарницький"` to `search_offers` or `benchmark_market_price`. `geo.py` extracts street/ЖК names from the listing title/description, geocodes them via Google Maps API (or OpenStreetMap fallback), and computes exact `distance_km` and `walking_minutes_est`.
- Multi-component build (e.g., dual-socket AI PC) → create one campaign per component slot (`Motherboard`, `2x CPU`, `ECC RAM`, `GPU`, `PSU`) **plus** one bundle campaign (`комплект X99 dual / 2x Xeon`) to compare bundle vs parts total.
- Always run `benchmark_market_price` before evaluating any listing — strips cheap cases/boxes/broken-parts noise (`<22%` of rough median) and computes `p10`, `p25`, `median`, `p75`, and per-offer `leverage_score`.
- **Never trust `stats` from a bare query.** The raw search mixes in whole PCs, accessories, other models and broken cards. A first RTX 3090 run with only `min_valid_price` gave a median of 95 250 UAH (real used 3090s: about 16 500–20 000), and the wrong conclusion "the market starts at 70 000" came from it. Always set:
  - `min_valid_price` (drops accessories) and `max_price` (drops whole systems and overpriced lots)
  - `exclude_keywords`: `пк, комплект, ноутбук, майнінг, дефект, несправна, нерабочая, не рабочая, не працює, ремонт, на запчастини`, plus accessory words (`nvlink, блок живлення, водоблок, охолодження, кабель`) and neighbouring models (`4060`)
  - Then read the full price list (`olx_probe.py --all`), not just the summary. A sample under about 10 valid lots gives no real percentiles: say so, do not quote `p10/p25` as a market.
- A budget far below the cleaned `p10` (target 10 000 vs a cheapest working lot of 16 500) will not be met by haggling. Say so, then offer: broken cards for repair (risky), haggle the cheapest working lot to about 14–15 000, a lower tier of the part, or a watch campaign.

### Step 2: Persist Lot Dossier & Pitch to User (`save_lot_handoff` — Mandatory Pre-Haggle Gate)
- For each top candidate (`1..3` per slot), call `save_lot_handoff` with:
  - `build_slot`: component role in the build
  - `why_shortlisted`: why this offer beats the market (`vs_median_pct`, `has_delivery`, seller tenure)
  - `target_price` and `walk_away_price`
  - `risks_and_questions`: technical checks to ask the seller (socket pins, working RAM channels, SMART/furmark, OLX Delivery)
  - `next_step`: `"Await user approval to send opening offer"`
- Present the shortlist to the user **before sending any message**:
  - Current asking price vs market median (`stats.median_market`) and `p25` (`stats.p25_good_deal`)
  - Forecasted discount (`500–1000 UAH` step-down on hardware or `8%–16%` via `haggling.suggested_opening_offer` and `haggling.suggested_target_price`)
  - Exact Ukrainian draft message and direct OLX URL
- **Never call `send_seller_message` with `dry_run=False` for cold outreach until the user approves the candidate lot(s).**

### Step 3: Outreach & Haggling (`send_seller_message`)
- After user approval, call `send_seller_message(offer_id=..., offered_price=..., text=...)`.
- Write polite, specific Ukrainian messages combining one technical verification question with a concrete immediate purchase commitment via OLX Delivery at `offered_price`.
- `send_seller_message` automatically updates the deal stage to `negotiating`, appends to `negotiation_log`, and re-renders `lots/lot-<OFFER_ID>.md` and `lots/INDEX.md`.

### Step 4: Recurring Polling & Deal Closing (`schedule` + `sync_seller_replies` + `present_ready_deals`)
- When monitoring active negotiations or waiting for new listings, schedule a recurring check every 15–30 minutes (`schedule` tool with `CronExpression="*/20 * * * *"` or recommend `/schedule`).
- On each trigger:
  1. Run `sync_seller_replies` to capture new seller messages and counter-offers.
  2. Run `scan_campaign_deals` on active campaigns to detect brand-new listings (`new_deals`) and seller price reductions (`price_drops`).
  3. Stay quiet if nothing changed; notify the user immediately when a seller replies, agrees, or drops the price.
- When a seller agrees or reaches target price, call `present_ready_deals` and report the final pitch (`pitch_uk`: item, initial vs final price, discount in UAH and `%`, seller quote, and OLX link).

## Scripts (when the `olx-mcp` tools are not connected)

The olx tools are not always loaded in a session. `scripts/olx_probe.py` drives `OlxBuyerService` directly. It never sends a message to a seller.

`python3 scripts/olx_probe.py "<query>" [--city X] [--min-valid N] [--max-price N] [--exclude "a,b,c"] [--category ID] [--condition used|new] [--sample N] [--all]`

Read-only. Prints `auth_status`, the benchmark stats and the candidates; `--all` lists every candidate and every excluded lot with its reason.

## Known Issues & Fixes

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `HTTP 401 {"error":"Unauthorized"}` or `HTTP 403` on expired session | OLX AWS Cognito `access_token` has a 15-minute TTL, and `SID`/`aws-waf-token` eventually expire | `AuthVault.build_client()` and `OlxClient` (`on_auth_expired`) first try silent headless refresh (`--headless=new`), and if that fails, automatically launch a visible Chrome login window (`interactive_browser_login(headless=False)`) so the user can re-login and the request retries automatically. Set `OLX_MCP_NO_INTERACTIVE_LOGIN=1` in background jobs. |
| `HTTP 403 The request could not be satisfied` from CloudFront on `www.olx.ua` | CloudFront WAF blocks Python `urllib` / `curl` JA3 TLS fingerprints or US VPN exit IPs | `OlxClient.request_json` automatically falls back to a shared headless Chrome CDP session (`_get_or_start_shared_cdp`). If still 403, disconnect US VPN so traffic exits via UA/EU. |
| `blocked_reason: already_contacted_offer` on `send_seller_message` | Anti-duplicate guard prevented sending a second cold outreach to the same `offer_id` | Pass `thread_id` (from `list_threads` or `deal.thread_id`) to reply inside the existing conversation, or `force=True` if explicitly requested by the user. |
