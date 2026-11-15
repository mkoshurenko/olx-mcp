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


if __name__ == "__main__":
    unittest.main()

