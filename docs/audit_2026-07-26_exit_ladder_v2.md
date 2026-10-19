# Comprehensive Audit & Fixes — 2026-07-26 (exit-ladder-v2)

Multi-agent audit: 7 subsystem auditors + adversarial verification (23 confirmed,
12 partial, 3 refuted). Data basis: 969 closed-trade events in `data/trading_bot.db`,
forward sample under `lifecycle-cost-v1-20260714`.

## Root cause of the July "WR up, profit flat" signature

Per-event WR was 74% but avg win $1.88 vs avg loss -$4.43 (payoff 0.42).
Confirmed mechanism, three independent sources:

**The BE profit-lock landed AT or ABOVE TP1.** Intraday lock = 5% ROI / leverage
= 0.5-1.0% price at lev 5-10, while the AI's median TP1 distance was 0.46-0.52%.
The tracker then capped `be_sl = price` — placing the stop exactly at market, so
the 85% runner died on the next downtick. DB proof: 79/101 tp1+trailing positions
exited within 0.15% of the TP1 price; those exits were recorded as
`trailing_stop`, hiding the mechanism.

## Fixes applied (code)

| # | Fix | File |
|---|---|---|
| 1 | BE lock capped at 0.5×TP1 distance, floored at round-trip cost, NEVER set to market price | `utils/pnl_tracker.py` |
| 2 | `TP2_TRAIL_TIGHTEN_MULT` wired (was dead config — docstring promised tighten, code never did) | `utils/pnl_tracker.py` |
| 3 | Pre-TP1 trailing arm honoring the AI's `ts.ap` activation price (stored but never read) | `utils/pnl_tracker.py` |
| 4 | TP1 partial gated on `TP1_PARTIAL_MIN_R` (0.5R) — July's 126 partials at ~0.3-0.5R avg +$0.55 were fee generators | `utils/pnl_tracker.py`, `config.py` |
| 5 | Honest SL exit attribution: `sl_kind` → `stop_loss` / `be_lock_stop` / `tp2_lock_stop` / `trailing_stop` | `utils/pnl_tracker.py`, `main.py` |
| 6 | Dry-run gap honesty: SL fills at worse-of(stop, observed), not always exactly at stop | `utils/pnl_tracker.py` |
| 7 | Admission RR now cost-adjusted (net of 0.15% round trip, both sides) + `RR_TP2_FLOOR` valuing the runner at TP2 (TP3 reached only ~11% of the time) | `risk/decision_engine.py`, `main.py`, `config.py` |
| 8 | Whale-boost score leak closed: SMC soft-bypass, medium-wash gate, high-conviction tag, and `ai_score` attribution all read `_ai_score_raw` | `agent/filters.py`, `main.py`, `utils/pnl_tracker.py` |
| 9 | Crash-proofing: run_agent auto-restarts with backoff (trailing stops are local-only — a dead process = no trail enforcement); `_safe_float` + non-dict guards in signal parsing; `_save_store` retries once; quick-exit close wrapped | `main.py`, `analyzer/signal_engine.py`, `utils/pnl_tracker.py`, `agent/positions.py` |
| 10 | Slots-full scan integrity: if the universe was fetched slots-full (1-3 symbols, no BTC anchor), the AI scan defers to the next cycle instead of running on crippled data | `main.py` |
| 11 | Data degeneracy: intraday 15M limit 200→300 (ema200 was ALWAYS == last close via the `_ema` short-data guard); 4H limit 60→120 (`h1_structure` was ALWAYS "sideways" — the super-HTF field the prompt makes decision-critical); S/R side-classification now uses the same closed-candle frame as SMC nearness | `data/market_feed.py` |
| 12 | Funding fetch failure returns None (was 0.0 sentinel → fake z=-5 hard rejects) | `data/market_feed.py` |
| 13 | Prompt de-gaming: exact reject thresholds removed (model pinned sc>=8/cf>=0.70 by construction); score = honest rubric measurement, cf = calibrated P(TP2 before SL); SL rule in prompt now states the REAL enforced stop (1.0-2.0×ATR) and demands TP1>=1R / TP2>=2R against it | `analyzer/signal_engine.py` |
| 14 | Live fill integrity: `execFee` captured and booked (live PnL was recorded gross); partial-close confirm-timeout verifies position delta before allowing retry (double-close bug); entry confirm-timeout checks the position and still attaches SL (was: real position left untracked with NO stop); dry-run entries re-priced at a fresh ticker (stale pre-AI snapshot inflated edge) | `execution/cex_executor.py`, `utils/pnl_tracker.py`, `main.py` |
| 15 | Learning decoupled: TradeMemory ALWAYS records (needed to ever rebuild); `AI_LEARNING` now gates only consumption (prompt context, adaptive params, MEM-BLACKLIST — blacklist replay showed it would have blocked +$23.5 of net winners, keep off until rebuilt) | `main.py` |
| 16 | GOOD_SETUP requires positive total PnL, not WR alone (was teaching the model to maximize winrate over expectancy) | `utils/trade_memory.py` |

## Config changes (.env)

- `STRATEGY_VERSION=exit-ladder-v2-20260726` (new forward cohort — do not mix with v1)
- `SCORE_HIGH_SIZE_MULT=1.0`, `SCORE_GE_95_SIZE_MULT=1.0` (score>=9.0 was July's BEST bucket: n=62, +$0.82/pos — the 0.75 penalty was inverted)
- `RR_TP2_FLOOR=1.0`, `TP1_PARTIAL_MIN_R=0.5` (new knobs)

## Deliberately NOT changed

- `MAX_LOSS_USDT_PER_TRADE=10` — do NOT raise until the new ladder shows
  per-position payoff > 1.3 over ~100 completed lifecycles; scaling a payoff-1.0
  system just scales variance. Then raise to 30-50 (binding constraint becomes
  0.3-0.5% equity).
- Defensive entry filters — untouched (house rule; several findings proposing
  loosening were refuted by counterfactual replay).
- Memory blacklist thresholds — inert while AI_LEARNING=false; must be made
  regime-relative (R-based, not absolute USDT) before re-enabling.

## Known remaining items (next audit)

- BE/trail SL advances are never mirrored to the exchange in live mode
  (restart reverts winners to the original wide SL) — needs milestone → 
  `update_sl` calls.
- `update_sl_roi` ignores `SL_MAX_PRICE_MOVE_PCT`/ATR floors (DCA + orphan path).
- Telegram time-exit/DCA prompts block the scan loop up to 120s per position.
- Manual Telegram partial close (<100%) reduces Bybit but is never booked.
- `min_notional` cached but not enforced pre-order.
- OKX-fallback funding compares a foreign venue's rate against Bybit history;
  OI fetch fails open on fallback venues.
- Loss clustering is temporal (00-03 & 16-19 UTC opens: -$91.7 vs +$192 rest) —
  collect 2-4 more weeks under v2, then consider REGIME_MARGINAL bumps for those
  windows.

## Promotion gate (unchanged from 2026-07-14, restarted for v2)

Collect >= 100 completed dry-run lifecycles under `exit-ladder-v2-20260726`.
Promote to live only if cost-adjusted expectancy > 0, PF > 1.15, bounded
drawdown, no single short period contributing most of the profit.
