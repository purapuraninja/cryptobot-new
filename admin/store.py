"""
admin/store.py — shared pnl_store access for admin tooling.

Respects Config.STORAGE_BACKEND ("json" | "sqlite"): when sqlite, all reads
and writes go through storage.sqlite_backend (atomic transactions); backups
copy the .db file. When json, behaviour is identical to before.

Centralising load/save/backup here removes the copy-pasted boilerplate that
used to live in each one-off script.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Project root = parent of this package's parent (admin/ -> root).
ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / "pnl_store.json"
BACKUP_DIR = ROOT / "archive" / "pnl_store_backups"
_DB_PATH = ROOT / "data" / "trading_bot.db"


def _use_sqlite() -> bool:
    try:
        from config import Config
        return Config.STORAGE_BACKEND == "sqlite"
    except Exception:
        return False


def store_path() -> Path:
    return STORE_PATH


def load_store(path: Path | None = None) -> Dict[str, Any]:
    """Load pnl_store. Uses sqlite backend when STORAGE_BACKEND=sqlite."""
    if path is None and _use_sqlite():
        from storage import sqlite_backend
        return sqlite_backend.load_store()
    p = path or STORE_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"{p.name} not found at {p}. Run admin commands from the project root."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def backup_store(path: Path | None = None) -> Path:
    """Copy the active store to archive/pnl_store_backups/ with a UTC timestamp."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # When sqlite, back up the .db; otherwise the JSON file.
    if path is None and _use_sqlite() and _DB_PATH.exists():
        src = _DB_PATH
        dst = BACKUP_DIR / f"trading_bot_backup_{ts}.db"
    else:
        src = path or STORE_PATH
        dst = BACKUP_DIR / f"pnl_store_backup_{ts}.json"
    shutil.copy2(src, dst)
    return dst


def save_store(data: Dict[str, Any], path: Path | None = None) -> None:
    """Write the store. Uses sqlite backend when STORAGE_BACKEND=sqlite."""
    if path is None and _use_sqlite():
        from storage import sqlite_backend
        sqlite_backend.save_store(data)
        return
    p = path or STORE_PATH
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
