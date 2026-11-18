# olx-mcp

An MCP server for buying on olx.ua: search, market price benchmarking, per-lot handoff dossiers and seller negotiation. Includes a Claude Code skill and slash command that drive it.

## Layout

- `src/olx_mcp/`: the MCP server (stdio, JSON-RPC 2.0, 23 tools) including:
  - `auth.py` & `client.py`: silent headless Chrome token auto-refresh + automatic interactive Chrome login window fallback when `SID` / `access_token` expires (`401`/`403`)
  - `geo.py`: street/ЖК/metro extractor from Ukrainian descriptions, zero-key OpenStreetMap Nominatim geocoder (with local cache `~/.config/olx-mcp/geocache.json`), and Haversine radius filtering (`near_address` + `radius_km` + `district`)
  - `pricing.py`: outlier stripping and haggling anchors rounded to clean `50 UAH` steps
- `tests/`: unit tests (`python3 -m unittest discover -s tests -v`)
- `scripts/olx_probe.py`: read-only price & geo-radius benchmark from the command line
- `skill/SKILL.md`, `skill/olx-buyer-command.md`: the Claude Code skill and `/olx-buyer` command

## Authentication

No credentials are in this repo. The server stores your own session in `~/.config/olx-mcp/auth.json` (mode `0600`), or reads `OLX_ACCESS_TOKEN` from the environment. Never commit either.

Sending a message to a seller always needs explicit approval in the skill workflow.
