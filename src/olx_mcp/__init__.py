"""Stage 1..3 package for OLX Ukraine AI Buyer & Deal Negotiator MCP (`olx-mcp`)."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_STATE_DIR = Path(
    os.environ.get("OLX_MCP_STATE_DIR", str(Path.home() / ".config" / "olx-mcp"))
)
DEFAULT_DB_PATH = Path(
    os.environ.get("OLX_MCP_DB_PATH", str(DEFAULT_STATE_DIR / "olx_buyer.sqlite3"))
)
DEFAULT_AUTH_PATH = Path(
    os.environ.get("OLX_MCP_AUTH_PATH", str(DEFAULT_STATE_DIR / "auth.json"))
)
DEFAULT_LOTS_DIR = Path(
    os.environ.get("OLX_MCP_LOTS_DIR", str(DEFAULT_STATE_DIR / "lots"))
)
