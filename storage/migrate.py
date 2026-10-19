"""
storage/migrate.py — JSON -> SQLite migration and equivalence verification.

migrate_json_to_sqlite()
    Reads pnl_store.json and trade_memory.json and writes them into the SQLite
    kv_store as-is. Non-destructive: the JSON files are left untouched.

verify_equivalence()
    Loads each document from BOTH backends and asserts they are deeply equal.
    This is the gate before any cutover — if the SQLite copy is not identical
    to the JSON source, we must not switch the bot over.

Run via: python admin.py storage migrate  /  python admin.py storage verify
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from storage import sqlite_backend as sq

ROOT = Path(__file__).resolve().parents[1]
PNL_JSON = ROOT / "pnl_store.json"
MEMORY_JSON = ROOT / "trade_memory.json"
EXPORT_DIR = ROOT / "archive"


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def migrate_json_to_sqlite(db_path: Path | None = None) -> Dict[str, Any]:
    """Copy both JSON documents into SQLite. Returns a summary dict."""
    summary: Dict[str, Any] = {"migrated": [], "skipped": []}

    store = _read_json(PNL_JSON)
    if store:
        sq.save_store(store, db_path)
        summary["migrated"].append(
            f"pnl_store: {len(store.get('closed_trades', []))} closed, "
            f"{len(store.get('open_positions', {}))} open"
        )
    else:
        summary["skipped"].append("pnl_store.json (missing/empty)")

    memory = _read_json(MEMORY_JSON)
    if memory:
        sq.save_memory(memory, db_path)
        summary["migrated"].append(
            f"trade_memory: {len(memory.get('trades', []))} trades, "
            f"{len(memory.get('symbol_stats', {}))} symbols"
        )
    else:
        summary["skipped"].append("trade_memory.json (missing/empty)")

    return summary


# ── Deep equivalence ────────────────────────────────────────────────────────

def _diff(a: Any, b: Any, path: str = "") -> List[str]:
    """Return a list of human-readable difference paths between a and b."""
    diffs: List[str] = []
    if type(a) is not type(b):
        # Allow int/float equivalence (JSON round-trips ints as ints).
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if float(a) != float(b):
                diffs.append(f"{path}: {a!r} != {b!r}")
            return diffs
        diffs.append(f"{path}: type {type(a).__name__} != {type(b).__name__}")
        return diffs
    if isinstance(a, dict):
        for k in set(a) | set(b):
            if k not in a:
                diffs.append(f"{path}.{k}: missing in A")
            elif k not in b:
                diffs.append(f"{path}.{k}: missing in B")
            else:
                diffs += _diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            diffs.append(f"{path}: len {len(a)} != {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diffs += _diff(x, y, f"{path}[{i}]")
    else:
        if a != b:
            diffs.append(f"{path}: {a!r} != {b!r}")
    return diffs


def verify_equivalence(db_path: Path | None = None) -> Tuple[bool, Dict[str, List[str]]]:
    """Compare JSON source vs SQLite for both documents.

    Returns (ok, {doc: [diffs]}). ok is True only when both documents match
    exactly (empty diff lists).
    """
    report: Dict[str, List[str]] = {}

    json_store = _read_json(PNL_JSON)
    sqlite_store = sq.load_store(db_path)
    report["pnl_store"] = _diff(json_store, sqlite_store, "pnl_store")

    json_mem = _read_json(MEMORY_JSON)
    sqlite_mem = sq.load_memory(db_path)
    report["trade_memory"] = _diff(json_mem, sqlite_mem, "trade_memory")

    ok = all(len(v) == 0 for v in report.values())
    return ok, report


def export_sqlite_to_json(db_path: Path | None = None) -> Dict[str, Any]:
    """Export both documents from SQLite to timestamped JSON files in archive/.

    This is the reverse of migrate_json_to_sqlite — the escape hatch that
    makes cutover to sqlite reversible. JSON files are written to archive/,
    not to the root (which would risk confusing the bot if STORAGE_BACKEND
    is still json).
    """
    from datetime import datetime, timezone

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    summary: Dict[str, Any] = {"exported": [], "db_path": str(db_path or sq.DB_PATH)}

    store = sq.load_store(db_path)
    if store:
        out = EXPORT_DIR / f"pnl_store_export_{ts}.json"
        out.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
        summary["exported"].append(str(out.name))
        summary["pnl_store"] = {
            "closed_trades": len(store.get("closed_trades", [])),
            "open_positions": len(store.get("open_positions", {})),
        }

    memory = sq.load_memory(db_path)
    if memory:
        out = EXPORT_DIR / f"trade_memory_export_{ts}.json"
        out.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
        summary["exported"].append(str(out.name))
        summary["trade_memory"] = {
            "trades": len(memory.get("trades", [])),
            "symbols": len(memory.get("symbol_stats", {})),
        }

    return summary
