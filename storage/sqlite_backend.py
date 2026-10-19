"""
storage/sqlite_backend.py — SQLite persistence for pnl_store and trade_memory.

Public functions mirror the JSON helpers they replace:
    load_store()  -> dict     save_store(dict)
    load_memory() -> dict     save_memory(dict)

The canonical document is stored as JSON text in kv_store, so the returned
dicts are byte-identical to what the JSON backend returns. save_store also
refreshes the closed_trades read model inside the same transaction, so the
audit projection can never drift from the document.

All writes use a single transaction: either the whole document lands or none
of it does. No more half-written-file corruption when a reader (dashboard,
MCP) races the bot mid-save.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from storage.schema import (
    DOC_PNL_STORE,
    DOC_TRADE_MEMORY,
    apply_schema,
)

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "trading_bot.db"


# Default document shapes (match the JSON backends' empty state exactly).
def _empty_store() -> Dict[str, Any]:
    return {
        "open_positions": {},
        "closed_trades": [],
        "stats": {
            "total_realized": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "best_symbol": "",
            "worst_symbol": "",
        },
    }


def _empty_memory() -> Dict[str, Any]:
    return {
        "trades": [],
        "symbol_stats": {},
        "setup_stats": {},
        "daily_pnl": {},
        "streak": {"current": 0, "type": "none"},
        "version": 2,
        "pending_entries": {},
        "strategy_feedback": [],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect(
    db_path: Optional[Path] = None,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    p = db_path or DB_PATH
    if read_only:
        if not p.exists():
            raise FileNotFoundError(p)
        conn = sqlite3.connect(
            f"file:{p.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=30,
        )
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    if not read_only:
        # WAL: readers (dashboard/MCP) don't block the writer, and vice versa.
        conn.execute("PRAGMA journal_mode=WAL;")
        apply_schema(conn)
    return conn


# ── Generic document get/put ────────────────────────────────────────────────

def _get_doc(key: str, default: Dict[str, Any], db_path: Optional[Path] = None) -> Dict[str, Any]:
    p = db_path or DB_PATH
    if not p.exists():
        return default
    conn = _connect(db_path, read_only=True)
    try:
        row = conn.execute("SELECT doc FROM kv_store WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["doc"])
    finally:
        conn.close()


def _put_doc(key: str, doc: Dict[str, Any], db_path: Optional[Path] = None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO kv_store (key, doc, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET doc = excluded.doc, updated_at = excluded.updated_at",
            (key, json.dumps(doc, ensure_ascii=False), _now_iso()),
        )
        if key == DOC_PNL_STORE:
            _rebuild_closed_trades(conn, doc)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ── closed_trades read model ────────────────────────────────────────────────

def _rebuild_closed_trades(conn: sqlite3.Connection, store: Dict[str, Any]) -> None:
    """Refresh the exploded closed_trades table from the pnl_store document.

    Runs inside the caller's transaction. The document stays the source of
    truth; this table is a derived, queryable projection.
    """
    conn.execute("DELETE FROM closed_trades")
    rows = []
    for seq, t in enumerate(store.get("closed_trades", [])):
        rows.append((
            seq,
            t.get("symbol"),
            t.get("action"),
            _num(t.get("entry_price")),
            _num(t.get("exit_price")),
            _int(t.get("leverage")),
            _num(t.get("notional")),
            _num(t.get("pnl_usdt")),
            _num(t.get("pnl_pct")),
            t.get("reason"),
            t.get("ai_tier"),
            _num(t.get("ai_score")),
            _num(t.get("ai_confidence")),
            1 if t.get("high_conviction") else 0,
            1 if t.get("partial") else 0,
            t.get("open_time"),
            t.get("close_time"),
        ))
    if rows:
        conn.executemany(
            "INSERT INTO closed_trades ("
            "seq, symbol, action, entry_price, exit_price, leverage, notional, "
            "pnl_usdt, pnl_pct, reason, ai_tier, ai_score, ai_confidence, "
            "high_conviction, partial, open_time, close_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── Public API (mirrors the JSON helpers) ───────────────────────────────────

def load_store(db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _get_doc(DOC_PNL_STORE, _empty_store(), db_path)


def save_store(store: Dict[str, Any], db_path: Optional[Path] = None) -> None:
    _put_doc(DOC_PNL_STORE, store, db_path)


def load_memory(db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _get_doc(DOC_TRADE_MEMORY, _empty_memory(), db_path)


def save_memory(memory: Dict[str, Any], db_path: Optional[Path] = None) -> None:
    _put_doc(DOC_TRADE_MEMORY, memory, db_path)


def db_exists(db_path: Optional[Path] = None) -> bool:
    p = db_path or DB_PATH
    if not p.exists():
        return False
    conn = _connect(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM kv_store WHERE key IN (?, ?)",
            (DOC_PNL_STORE, DOC_TRADE_MEMORY),
        ).fetchone()
        return bool(row and row["n"] > 0)
    finally:
        conn.close()
