"""Storage safety tests for the canonical SQLite backend."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage.sqlite_backend import load_store, save_store


class SQLiteBackendTests(unittest.TestCase):
    def test_missing_database_read_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "missing.db"

            store = load_store(db_path)

            self.assertFalse(db_path.exists())
            self.assertEqual(store["closed_trades"], [])

    def test_saved_document_and_projection_round_trip_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "trading.db"
            store = {
                "open_positions": {},
                "closed_trades": [
                    {
                        "symbol": "BTCUSDT",
                        "action": "LONG",
                        "entry_price": 100.0,
                        "exit_price": 101.0,
                        "leverage": 5,
                        "notional": 100.0,
                        "pnl_usdt": 1.0,
                        "pnl_pct": 5.0,
                        "open_time": "2026-07-14 00:00:00",
                        "close_time": "2026-07-14 00:05:00",
                    }
                ],
                "stats": {"total_realized": 1.0},
            }
            save_store(store, db_path)
            before = db_path.stat().st_mtime_ns

            loaded = load_store(db_path)
            after = db_path.stat().st_mtime_ns

            self.assertEqual(loaded, store)
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
