"""Quick check: what's in data/trading_bot.db?"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "trading_bot.db"

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row

print("=== Tables in data/trading_bot.db ===")
for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    t = row["name"]
    n = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    print(f"  {t:25s} -> {n} rows")

print("\n=== Check: JSON files still exist? ===")
from pathlib import Path
for f in ["pnl_store.json", "trade_memory.json"]:
    p = Path(f)
    if p.exists():
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
        if "closed_trades" in data:
            print(f"  {f}: {len(data['closed_trades'])} closed trades")
        elif "trades" in data:
            print(f"  {f}: {len(data['trades'])} memory trades")
    else:
        print(f"  {f}: does not exist")

print("\n=== SQLite kv_store contents ===")
for row in conn.execute("SELECT key, length(doc), updated_at FROM kv_store"):
    print(f"  {row['key']}: {row[1]} chars, updated {row[2]}")

print("\n=== SQLite closed_trades (read model) ===")
n = conn.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
print(f"  {n} rows")

# Check legacy MCP tables
print("\n=== Legacy MCP 'trades' table ===")
n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
print(f"  {n} rows (empty = never populated, now deprecated)")

conn.close()
