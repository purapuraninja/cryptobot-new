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
    close_time  TEXT,
    -- Exit instrumentation. MFE/MAE make trail-width tuning measurable:
    -- capture_ratio = realized_move / mfe, so a low ratio on a large MFE
    -- is direct evidence the trail exited too early.
    mfe_pct           REAL,
    mae_pct           REAL,
    realized_move_pct REAL,
    capture_ratio     REAL
);

CREATE INDEX IF NOT EXISTS idx_closed_symbol ON closed_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_closed_close_time ON closed_trades(close_time);
CREATE INDEX IF NOT EXISTS idx_closed_reason ON closed_trades(reason);
"""


# Columns added to closed_trades after the table first shipped.
# CREATE TABLE IF NOT EXISTS never alters an existing table, so a database
# created before these columns existed needs them added explicitly.
# closed_trades is a read model rebuilt in full on every save, so the values
# backfill themselves on the next write — no data migration needed.
_CLOSED_TRADES_ADDED_COLUMNS = (
    ("mfe_pct", "REAL"),
    ("mae_pct", "REAL"),
    ("realized_move_pct", "REAL"),
    ("capture_ratio", "REAL"),
)


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if they don't already exist (idempotent)."""
    conn.executescript(SCHEMA_SQL)
    _add_missing_columns(conn)
    conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an older closed_trades table up to the current column set."""
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(closed_trades)")
    }
    if not existing:
        return
    for name, coltype in _CLOSED_TRADES_ADDED_COLUMNS:
        if name not in existing:
            conn.execute(
                f"ALTER TABLE closed_trades ADD COLUMN {name} {coltype}"
            )
