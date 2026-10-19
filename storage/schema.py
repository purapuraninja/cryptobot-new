"""
storage/schema.py — SQLite schema for the trading bot's canonical state.

Design: document-preserving + queryable projection.

  kv_store            one row per logical document ("pnl_store", "trade_memory").
                      The `doc` column holds the exact JSON the bot expects, so
                      load_store()/load_memory() return byte-identical dicts and
                      the ~10 mutation sites in pnl_tracker/trade_memory need no
                      behavioral change. Writes are atomic (single transaction),
                      which fixes the half-written-file corruption risk that the
                      old "rewrite whole JSON file" approach had.

  closed_trades       exploded projection of pnl_store["closed_trades"], one row
                      per trade, for SQL auditing/analytics (the "mudah diaudit"
                      goal). Rebuilt from the document on every save — the
                      document remains the source of truth, this is a read model.

This intentionally reuses data/trading_bot.db so the bot and the MCP server
share one database file instead of two disconnected data worlds.
"""

from __future__ import annotations

import sqlite3

# Logical document keys stored in kv_store.
DOC_PNL_STORE = "pnl_store"
DOC_TRADE_MEMORY = "trade_memory"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kv_store (
    key         TEXT PRIMARY KEY,
    doc         TEXT NOT NULL,              -- canonical JSON document
    updated_at  TEXT NOT NULL               -- UTC ISO-ish timestamp
);

-- Read model: one row per closed trade, rebuilt from pnl_store on save.
CREATE TABLE IF NOT EXISTS closed_trades (
    rowid_pk    INTEGER PRIMARY KEY AUTOINCREMENT,
    seq         INTEGER,                    -- index within the source array
    symbol      TEXT,
    action      TEXT,
    entry_price REAL,
    exit_price  REAL,
    leverage    INTEGER,
    notional    REAL,
    pnl_usdt    REAL,
    pnl_pct     REAL,
    reason      TEXT,
    ai_tier     TEXT,
    ai_score    REAL,
    ai_confidence REAL,
    high_conviction INTEGER,
    partial     INTEGER,
    open_time   TEXT,
    close_time  TEXT
);

CREATE INDEX IF NOT EXISTS idx_closed_symbol ON closed_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_closed_close_time ON closed_trades(close_time);
CREATE INDEX IF NOT EXISTS idx_closed_reason ON closed_trades(reason);
"""


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if they don't already exist (idempotent)."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()
