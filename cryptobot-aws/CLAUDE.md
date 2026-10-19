# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Crypto perpetual futures trading bot. Async Python. Pipes market data → AI signal → exchange execution, then tracks PnL and learns from outcomes.

- Primary exchange: Bybit USDT-perp (CEX). Hyperliquid is also wired up (DEX) but Bybit is the live path.
- Trading mode `swing` (4H) or `intraday` (15M) — selected via `TRADING_MODE` in `.env`. Many parameters auto-adjust per mode (see `config.py` "Per-Mode Auto-Tuning").

## Common commands

```powershell
# Install deps
pip install -r requirements.txt

# Run the bot (reads .env)
python main.py

# Operator CLI — single front door for all non-bot tooling
python admin.py --help

# Launchers (delegate to dashboard.py / mcp_server.py)
python admin.py dashboard                          # Flask web dashboard, port 5050
python admin.py mcp                                # MCP server, stdio (Cursor/Codex)
python admin.py mcp --transport sse --port 8080    # MCP server, SSE (http://127.0.0.1:8080/sse)

# Maintenance (mutate pnl_store.json — always backed up first; use --dry-run to preview, --yes to skip confirm)
python admin.py recount-stats                      # rebuild stats from closed_trades
python admin.py rollback-close SYM [SYM ...]       # undo mistaken close(s), then rebuild stats
python admin.py inject-sltp                         # attach ATR SL/TP to open positions missing them
python admin.py fix-pnl SYMBOL --real-pnl N        # correct a recorded trade's PnL
python admin.py fix-pnl SYMBOL --delete            # delete a mis-recorded trade

# dashboard.py / mcp_server.py still work as direct entry points if preferred.
# The former one-off scripts (recount_stats.py, rollback_false_close.py, inject_sl_tp.py,
# fix_dash_pnl.py, fix_siren.py) are consolidated into admin.py and archived under archive/.
```

There is no test suite. There are no lint/format commands configured. Always test changes by running `main.py` in `DRY_RUN=true` first.

Logs land in `logs/` (rotated daily). The agent prints a colored dashboard to stdout each scan.

## Architecture

Single-process async loop in `main.py::run_agent`. Each scan iterates:

1. **Refresh symbol universe** (`Config.AUTO_SYMBOLS`) — top-N USDT-perps by 24h turnover, filtered by `SYMBOL_BLACKLIST`. Open positions are always retained.
2. **Fetch market data** for all scan symbols in parallel via `data.market_feed.MarketFeed`. Each result carries OHLCV, EMA/RSI/ATR, multi-TF context (Daily/4H/1H/15M), funding, OI, SMC fields (`swept_prev_high/low`, `bos_bullish/bearish`, `long_retest_zone`, `near_support/resistance`, etc.), and risk flags (`spread_pct`, `wick_warning`, `funding_zscore`, `wash_suspicion`).
3. **Live-sync positions** (`_sync_live_positions`) — only in live mode. Reconciles `pnl_store.json` against actual Bybit positions and attaches a ROI-based SL to any orphan position (`needs_sl_attach`).
4. **Check SL/TP/trailing/time-exit** (`PnLTracker.check_sl_tp`). Returns events: full close, partial close (TP1/TP2), BE/trail milestones, or "requires_user_decision" for time-based exits — those prompt the user via Telegram (`notifier.ask_yes_no`).
5. **Process Telegram commands** (`close SYMBOL PCT`, `positions`, `perf`, `help`).
6. **Quick-Exit on reversal** (`_check_quick_exit`) and **DCA prompt** (`_check_dca`) for open positions.
7. **AI scan** — skipped when slots full or daily-loss circuit breaker tripped. Sends market snapshots + learning context + adaptive params + on-chain regime to the AI; parses signals.
8. **Per-signal filter gauntlet** before `executor.execute_signal`:
   - Symbol blacklist / data-only / memory blacklist (auto-blocks symbols with WR<40% over ≥3 trades)
   - 4 risk filters: spread, wick, funding anomaly, wash trading
   - `_passes_smc_retest_filter` — chase / late-entry rejection (modes: `off`/`soft`/`strict`)
   - Daily loss circuit breaker (6h rolling window — see `_daily_realized_loss`)
   - Score + confidence floors (`MIN_SCORE_HARD`, `MIN_CONFIDENCE_HARD`) override adaptive minimum
   - TP direction validated against **actual market price**, not the AI's stated entry (AI sometimes hallucinates entry to make TPs look valid)
   - Weighted R/R using `TP1/TP2/TP3` close-weights from config
   - Anti-overtrading: `MAX_TRADES_PER_SYMBOL_DAY`, `SYMBOL_LOSS_COOLDOWN_H`
   - `_apply_adaptive_rules` — widens SL to ATR-aware minimum, applies leverage floor/cap per mode, penalizes size for poor-WR symbols

### Key invariants

- **Close on Bybit first, then update tracker.** The full-close handler in `run_agent` calls `executor.partial_close` before `pnl.record_close`. If Bybit fails (and not dry-run), the tracker is NOT updated so the next scan retries. Preserve this order.
- **`pnl._store["open_positions"]` is the source of truth** for what's open (after sync). Don't iterate `Config.SYMBOLS` to find positions.
- **Exit price priority**: actual VWAP fill from `executor.partial_close` → Bybit ticker `lastPrice` → entry price (last resort, flagged `*_unreconciled` for later reconciliation by `admin.py recount-stats`).
- **UTC everywhere.** All trade timestamps use `_now_str()` (UTC) in `pnl_tracker.py`. Don't introduce local-time `datetime.now()` for trade times — it breaks the 6h rolling loss window and daily caps.
- **Data-only symbols** (default `BTCUSDT`) are fetched for AI context (global bias anchor) but rejected at entry — see `Config.DATA_ONLY_SYMBOLS`.
- **`SL_MODE=roi`** (default) places SL near liquidation (-100% ROI), then DCAs at `DCA_ROI_TRIGGER` (-75% ROI) and re-anchors SL after DCA. There's a hard price-distance cap (`SL_MAX_PRICE_MOVE_PCT`) that overrides this when ROI math would allow too-wide a price move.

### Multi-tier AI fallback

`SignalEngine` accepts a `fallback_chain` built by `Config.get_tier_chain()`. Tier-1 = `AI_PROVIDER`. Tier-2 = `AI_PROVIDER_FALLBACK` (with optional `AI_TIER2_*` overrides). Tier-3 = `AI_PROVIDER_TIER3`. Tier-2 can reuse Tier-1's provider only if `AI_TIER2_MODEL` differs. Each signal is tagged with `_ai_tier` so PnL breakdown can attribute trades to the tier that generated them.

### State files (all JSON in project root, auto-created)

| File | Purpose |
|---|---|
| `pnl_store.json` | Open positions + closed trades + aggregate stats. Source of truth for tracker. |
| `trade_memory.json` | AI learning: per-symbol/setup stats, streak, daily PnL, strategy feedback from MCP. |
| `.env` | Secrets + all tunables. `env.example` is the template. |

### MCP integration

`mcp_server.py` exposes ~18 read-mostly tools (positions, PnL, bad-trade review, strategy feedback). Strategy feedback flows: external agent submits → stored in `trade_memory.json` → bot's AI scan reviews next cycle → response stored as `bot_review`. The AI in this loop is the trading-bot LLM, not the monitoring agent — the monitoring agent is forbidden from executing trades.

## Conventions

- Indonesian comments are common in `main.py` and `config.py` (mixed with English). Don't translate them — they're load-bearing context the user reads.
- The bot rejects signals defensively. If the AI returns LONG/SHORT but a filter rejects it, that's expected — don't "fix" filters to let more trades through without understanding why they exist (most filters trace back to specific past losses).
- Files named `* opus.py` or `main og.py` are backups — ignore them unless explicitly diffing.
- Windows-first environment. Console encoding fix at top of `main.py` is required for the box-drawing dashboard.
