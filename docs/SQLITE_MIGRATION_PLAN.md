# Migration Plan — Unify all state onto a single SQLite database

**Status:** proposal, ready to execute
**Author:** handoff document for the implementing agent/AI
**Last updated:** 2026-07-05

---

## 0. Read this first

This document is a **self-contained, phase-by-phase plan** to migrate the
trading bot from its current mixed storage (two JSON files + a partly-used
SQLite DB) to **one SQLite database as the single source of truth**, read and
written by every component (bot, dashboard, MCP server, admin CLI).

Hard rules for whoever executes this:

- **The bot trades real money.** `pnl_store`'s `open_positions` is the source
  of truth for what is open. Never let a phase leave the bot unable to load or
  save this correctly.
- **Every phase must be independently shippable and reversible.** After each
  phase the bot must still run in `DRY_RUN=true` with no behavior change unless
  the phase explicitly flips a flag.
- **Verify before cutover.** The equivalence verifier
  (`storage/migrate.py::verify_equivalence`) must report `EQUIVALENT: True`
  before `STORAGE_BACKEND` is switched.
- **Default stays `json` until Phase 4.** Do not change the default backend
  until the write path is proven.
- Work in `DRY_RUN=true` for all testing. Only touch live after the full plan
  is verified.

---

## 1. Current state (as-is)

### 1.1 Data locations today

| Store | File | Shape | Written by | Read by |
|---|---|---|---|---|
| PnL / positions | `pnl_store.json` | dict: `open_positions`, `closed_trades`, `stats` | `utils/pnl_tracker.py` (`_save_store`) | pnl_tracker, dashboard, mcp_server, admin, report_generator, eval_bot |
| AI learning | `trade_memory.json` | dict: `trades`, `symbol_stats`, `setup_stats`, `daily_pnl`, `streak`, `pending_entries`, `strategy_feedback`, `version` | `utils/trade_memory.py` (`_save_memory`), mcp_server (`_save_trade_memory`) | trade_memory, dashboard, mcp_server |
| MCP analytics DB | `data/trading_bot.db` | SQLite tables: `trades`, `performance_snapshots`, `whale_data_cache`, `signal_history` | `mcp_server.py::DatabaseManager` (`insert_trade`, etc.) | mcp_server only |

**Key problem:** three data worlds. `data/trading_bot.db` (`trades` table) is
**disconnected** from `pnl_store.json` — the MCP server reads positions/history
from JSON but writes some analytics to its own SQLite tables. They can and do
diverge.

### 1.2 What already exists (done in prior work — do not redo)

The `storage/` package is already built and tested:

- `storage/schema.py` — SQLite DDL.
  - `kv_store(key, doc, updated_at)` — one JSON document per key
    (`pnl_store`, `trade_memory`). Document-preserving: the stored JSON is
    byte-identical to the old files.
  - `closed_trades(...)` — exploded read model of `pnl_store["closed_trades"]`,
    rebuilt on every save inside the same transaction. Indexed on
    `symbol`, `close_time`, `reason`.
- `storage/sqlite_backend.py` — `load_store` / `save_store` /
  `load_memory` / `save_memory`, atomic transactions, WAL mode. Returns dicts
  identical to the JSON backend. `db_path` defaults to `data/trading_bot.db`.
- `storage/migrate.py` — `migrate_json_to_sqlite()` (one-shot copy, JSON left
  intact) and `verify_equivalence()` (deep diff JSON vs SQLite; returns
  `(ok, report)`).
- `config.py` — `Config.STORAGE_BACKEND` (`"json"` default | `"sqlite"`).
- `utils/pnl_tracker.py` and `utils/trade_memory.py` — `_load_*`/`_save_*` now
  dispatch on `STORAGE_BACKEND`, with automatic fallback to JSON on any SQLite
  error. **This means the bot's own read/write path is already SQLite-ready.**
- `admin.py storage migrate` and `admin.py storage verify` — CLI wrappers.
- `.gitignore` — covers `data/trading_bot.db`, `*.db-wal`, `*.db-shm`.

Verified: migrating the real data (542 closed trades, 377 memory trades)
produces **0 diffs**, and a live tracker save/load round-trip under
`STORAGE_BACKEND=sqlite` works.

### 1.3 What is NOT done yet (the scope of this plan)

1. `mcp_server.py` still reads/writes `pnl_store.json` and `trade_memory.json`
   directly, and maintains its own separate `trades`/`signal_history`/etc.
   SQLite tables. It must be pointed at the unified `storage` backend.
2. `dashboard.py` reads the two JSON files directly. It must read via `storage`.
3. `utils/report_generator.py` and `utils/eval_bot.py` read `pnl_store.json`
   directly. They must read via `storage`.
4. `admin/store.py` (maintenance commands) reads/writes `pnl_store.json`
   directly. It must go through `storage`.
5. The MCP-only tables (`performance_snapshots`, `whale_data_cache`,
   `signal_history`, and the legacy `trades` table) need a decision:
   keep as MCP-owned analytics, or fold in. See Phase 3.
6. The default backend flip and JSON retirement. See Phase 4–5.

---

## 2. Target state (to-be)

```
                    data/trading_bot.db  (single source of truth)
                    ├── kv_store
                    │     ├─ "pnl_store"     → {open_positions, closed_trades, stats}
                    │     └─ "trade_memory"  → {trades, symbol_stats, ...}
                    ├── closed_trades        (read model, rebuilt on save)
                    └── [MCP analytics tables: performance_snapshots,
                         whale_data_cache, signal_history]  (optional, Phase 3)

   bot (main.py) ─────────┐
   dashboard.py ──────────┤
   mcp_server.py ─────────┼──→ storage/ (load_store/save_store/load_memory/save_memory)
   admin.py ──────────────┤
   report_generator ──────┘
```

Every component imports from `storage/`; nobody reads `*.json` at runtime.
JSON files remain only as archived backups.

---

## 3. Phases

> Each phase lists: **Goal**, **Changes**, **Verification** (must pass before
> moving on), and **Rollback**.

---

### Phase 0 — Baseline & safety net

**Goal:** capture a known-good baseline so any later phase can be compared and
reverted.

**Changes:**
- No code changes.
- Take a manual backup: copy `pnl_store.json`, `trade_memory.json`, and
  `data/trading_bot.db` into `archive/premigration_baseline_<UTC>/`.
- Record current aggregate numbers for later comparison:
  `python admin.py recount-stats --dry-run` → note total_trades, wins, losses,
  total_realized. Save this output to the baseline folder.

**Verification:**
- Baseline folder exists with all three files + the recount output.
- `python main.py` still starts in `DRY_RUN=true` (import + first scan) — no
  changes made, this just confirms the starting point runs.

**Rollback:** N/A (no changes).

---

### Phase 1 — Establish SQLite as a verified mirror (no cutover)

**Goal:** SQLite contains an exact copy of both documents, continuously
verifiable. Bot still runs on JSON.

**Changes:**
- Run `python admin.py storage migrate`.
- Run `python admin.py storage verify` → must print `EQUIVALENT: True`.
- Do **not** change `STORAGE_BACKEND` (stays `json`).

**Verification:**
- `verify` reports `EQUIVALENT: True` for both `pnl_store` and `trade_memory`.
- `closed_trades` read-model row count equals
  `len(pnl_store["closed_trades"])`:
  ```sql
  SELECT COUNT(*) FROM closed_trades;   -- must equal JSON closed_trades length
  ```
- SQL audit sanity check matches `recount-stats`:
  ```sql
  SELECT COUNT(*) AS trades,
         SUM(CASE WHEN pnl_usdt >= 0 THEN 1 ELSE 0 END) AS wins
  FROM closed_trades WHERE partial = 0;
  ```

**Rollback:** delete `data/trading_bot.db` (JSON is untouched and authoritative).

---

### Phase 2 — Readers go through `storage` (dashboard, reports, admin, MCP reads)

**Goal:** every **read** path can source from the active backend, so when the
backend flips later, all readers follow automatically. Still no write cutover.

This phase is done in small, independently-testable steps. After each step,
the component must behave identically with `STORAGE_BACKEND=json`.

**2a. `admin/store.py`** (maintenance CLI)
- Replace direct `pnl_store.json` read/write with a dispatch:
  when `Config.STORAGE_BACKEND == "sqlite"`, use
  `storage.sqlite_backend.load_store/save_store`; else current JSON behavior.
- Keep `backup_store()` meaningful for both: for sqlite, back up the `.db`
  file (copy `data/trading_bot.db` to `archive/pnl_store_backups/`) instead of
  the JSON.
- Verify: `python admin.py recount-stats --dry-run` gives identical output
  under both `STORAGE_BACKEND=json` and `=sqlite` (after a fresh migrate).

**2b. `utils/report_generator.py` and `utils/eval_bot.py`**
- Replace their local `_load_store()` (direct JSON read) with
  `storage`-aware loading (reuse `pnl_tracker._load_store` or call
  `storage.sqlite_backend.load_store` when the flag is set).
- Verify: generate `bot_performance.html` under both backends; the HTML
  content (trade counts, PnL) must match.

**2c. `dashboard.py`**
- Replace `_load_json(PNL_STORE)` / `_load_json(TRADE_MEMORY)` with a
  storage-aware loader that respects `STORAGE_BACKEND`.
- Keep `dashboard.html` file serving as-is (that's a static asset, unrelated).
- Verify: start dashboard under `=json` and `=sqlite` (post-migrate); the
  `/api/data` JSON payload must be equivalent (same positions, same stats).

**2d. `mcp_server.py` read tools**
- Replace `_load_pnl_store()` and `_load_trade_memory()` (direct JSON reads)
  with calls into `storage`. Respect `STORAGE_BACKEND`.
- Do **not** yet touch `DatabaseManager` or the MCP write tools — Phase 3.
- Verify: MCP read tools (positions, PnL history, bad-trade review) return the
  same data under both backends.

**Verification (whole phase):**
- With `STORAGE_BACKEND=json`: every reader behaves exactly as before Phase 2
  (diff HTML/API output against Phase 0 baseline).
- With `STORAGE_BACKEND=sqlite` (after migrate + verify): every reader returns
  data equivalent to the JSON path.

**Rollback:** revert the per-file edits (each step is a small isolated change);
JSON remains authoritative throughout.

---

### Phase 3 — Unify the MCP write path and analytics tables

**Goal:** eliminate the disconnected data world in `data/trading_bot.db`. The
MCP server must write to the same unified state, and its analytics tables must
either be sourced from the unified data or clearly owned as derived caches.

**Decisions to make (document the choice in code comments):**

1. **`trade_memory` writes from MCP** (`_save_trade_memory`, used by the
   strategy-feedback tool):
   - Route through `storage.sqlite_backend.save_memory` when
     `STORAGE_BACKEND=sqlite`. This is the important one — external agents
     submit strategy feedback that the bot's next scan reads. Both sides must
     use the same store.
   - Verify: submit a feedback item via MCP, confirm the bot's
     `TradeMemory._load_memory()` sees it under sqlite.

2. **Legacy `trades` table** (`DatabaseManager.insert_trade`/`get_trade_history`):
   - This overlaps with the new `closed_trades` read model but has a different
     schema (`side`/`quantity`/`entry_time`/`exit_time` vs
     `action`/`notional`/`open_time`/`close_time`).
   - Recommended: **deprecate the legacy `trades` table**. Point
     `get_trade_history` at the `closed_trades` read model (map column names).
     Stop calling `insert_trade` (the read model is rebuilt from the document
     automatically, so manual inserts are redundant and a divergence risk).
   - Verify: `get_trade_history` returns rows equivalent to the old behavior,
     sourced from `closed_trades`.

3. **`performance_snapshots`, `whale_data_cache`, `signal_history`:**
   - These are genuine MCP-owned analytics/caches with no JSON equivalent.
   - Recommended: **keep them**, in the same `data/trading_bot.db`. They are
     already SQLite and already isolated. Just confirm they coexist with
     `kv_store`/`closed_trades` in one file (they do — same DB path).
   - No migration needed; document them as "MCP analytics, derived/cache".

**Verification (whole phase):**
- Only one `.db` file exists and it holds `kv_store`, `closed_trades`, and the
  MCP analytics tables together.
- No code path writes `trade_memory.json` or `pnl_store.json` when
  `STORAGE_BACKEND=sqlite` (grep for `_write_json_file`, `.write_text` on those
  paths; all must be behind the json branch).
- MCP feedback round-trip works bot↔MCP under sqlite.

**Rollback:** revert MCP edits; with `STORAGE_BACKEND=json` the MCP server
still works against JSON as before.

---

### Phase 4 — Cutover: make SQLite the default

**Goal:** new data is written directly to SQLite by default.

**Pre-cutover checklist (all must pass):**
- [ ] Phases 1–3 complete and verified.
- [ ] `python admin.py storage migrate` run against the **latest** JSON state.
- [ ] `python admin.py storage verify` → `EQUIVALENT: True`.
- [ ] Bot runs several scans in `DRY_RUN=true` with `STORAGE_BACKEND=sqlite`,
      opening/closing simulated positions; `verify`-style spot checks pass.

**Changes:**
- Set `STORAGE_BACKEND=sqlite` in `.env` (not the code default yet — env first,
  so it's trivially reversible).
- Run the bot in `DRY_RUN=true` for a full session. Confirm:
  - positions open/close correctly,
  - `closed_trades` read model grows,
  - dashboard + MCP reflect changes,
  - no `*.json` writes occur (the files stop changing).

**IMPORTANT timing note:** `migrate` is a one-shot copy, not continuous sync.
Run `migrate` **immediately before** flipping the flag so no JSON-era data
written in the gap is lost. Ideally do the flip while the bot is stopped.

**Verification:**
- After a DRY_RUN session on sqlite, stop the bot; `pnl_store.json` mtime is
  unchanged from before the session (proves no JSON writes), while
  `data/trading_bot.db` mtime is fresh.
- `admin.py recount-stats --dry-run` (now reading sqlite via Phase 2a) reflects
  the new trades.

**Rollback:** set `STORAGE_BACKEND=json`. But note: any trades made while on
sqlite exist only in the DB. To go back cleanly, add and run a reverse export
(`sqlite → json`) — see Phase 5 "reverse sync" note. Until that exists,
treat the cutover as a soft commit point.

---

### Phase 5 — Retire JSON as a runtime dependency

**Goal:** JSON files are no longer read or written at runtime; they exist only
as archival exports.

**Changes:**
- Flip the code default: `STORAGE_BACKEND` default `"json"` → `"sqlite"` in
  `config.py` (env can still override to `"json"` for emergency).
- Add an **export** command for safety/backup and reverse-migration:
  `python admin.py storage export` → writes current SQLite state back out to
  timestamped `pnl_store_export_<UTC>.json` / `trade_memory_export_<UTC>.json`
  in `archive/`. This is the escape hatch that makes Phase 4 rollback clean and
  gives human-readable backups.
- Move the now-inert root `pnl_store.json` / `trade_memory.json` into
  `archive/` (keep them; do not delete — they are the last JSON-era snapshot).
- Update `CLAUDE.md`: state files section now documents SQLite as the source of
  truth, JSON only via `storage export`.

**Verification:**
- Fresh clone + `STORAGE_BACKEND` unset → bot uses sqlite by default.
- With root JSON files absent, bot/dashboard/MCP all still work (proves nothing
  depends on the JSON files at runtime).
- `admin.py storage export` produces valid JSON that `verify_equivalence`-style
  comparison confirms matches the DB.

**Rollback:** `STORAGE_BACKEND=json` + restore JSON files from `archive/` (use
`storage export` output, which is current).

---

## 4. Cross-cutting requirements

### 4.1 Concurrency
Multiple processes hit the DB (bot writes; dashboard + MCP read). SQLite is in
WAL mode (`storage/sqlite_backend.py` sets `PRAGMA journal_mode=WAL`), so
readers don't block the writer. Keep every write inside the existing single
transaction in `_put_doc`. Do not open long-lived write transactions.

### 4.2 The `_store` / `_mem` in-memory pattern
`PnLTracker` and `TradeMemory` hold the whole document in memory (`self._store`,
`self._mem`) and call `_save_*` to persist. This pattern is preserved — the
backend only changes where `_save_*`/`_load_*` read/write. **Do not** try to
make individual field updates hit the DB directly; that would require rewriting
the ~10 mutation sites and is explicitly out of scope (high regression risk on
the money path).

### 4.3 Verifier is the gate
Never cut over a write path without `verify_equivalence` passing. Extend the
verifier if new documents/tables are added.

### 4.4 Backups
Every mutating admin command already backs up before writing. Ensure Phase 2a
keeps this true for the sqlite branch (back up the `.db`).

---

## 5. File-by-file change checklist

| File | Phase | Change |
|---|---|---|
| `admin/store.py` | 2a | dispatch load/save on `STORAGE_BACKEND`; sqlite-aware backup |
| `utils/report_generator.py` | 2b | load store via `storage` |
| `utils/eval_bot.py` | 2b | load store via `storage` |
| `dashboard.py` | 2c | load pnl+memory via `storage` |
| `mcp_server.py` (`_load_pnl_store`, `_load_trade_memory`) | 2d | read via `storage` |
| `mcp_server.py` (`_save_trade_memory`) | 3 | write via `storage` |
| `mcp_server.py` (`DatabaseManager`) | 3 | point `get_trade_history` at `closed_trades`; stop `insert_trade` |
| `.env` | 4 | `STORAGE_BACKEND=sqlite` |
| `config.py` | 5 | default `STORAGE_BACKEND=sqlite` |
| `admin/cli.py` + `storage/migrate.py` | 5 | add `storage export` (sqlite→json) |
| `CLAUDE.md` | 5 | document SQLite as source of truth |
| root `*.json` | 5 | move to `archive/` |

---

## 6. Test commands reference

```powershell
# migrate + verify (Phases 1, 4)
python admin.py storage migrate
python admin.py storage verify          # must print EQUIVALENT: True

# backend-parity check (Phase 2): run a reader under both backends and diff
$env:STORAGE_BACKEND="json";   python admin.py recount-stats --dry-run > json.txt
$env:STORAGE_BACKEND="sqlite"; python admin.py recount-stats --dry-run > sqlite.txt
# json.txt and sqlite.txt must be identical (after a fresh migrate)

# SQL audit sanity
# sqlite3 data/trading_bot.db "SELECT COUNT(*), SUM(CASE WHEN pnl_usdt>=0 THEN 1 ELSE 0 END) FROM closed_trades WHERE partial=0;"

# always test the bot in dry-run first
# set DRY_RUN=true in .env, then: python main.py
```

---

## 7. Definition of done

- Bot, dashboard, MCP server, admin CLI, and report generator all read/write
  through `storage/` with `STORAGE_BACKEND=sqlite` as the default.
- No runtime code reads or writes `pnl_store.json` / `trade_memory.json`.
- `data/trading_bot.db` is the single source of truth and also holds MCP
  analytics tables.
- `admin.py storage export` can reproduce JSON snapshots on demand.
- All verification steps in Phases 1–5 pass.
- `CLAUDE.md` updated.
