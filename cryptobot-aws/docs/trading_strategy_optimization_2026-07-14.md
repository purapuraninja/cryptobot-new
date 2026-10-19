# Trading Strategy Audit and Optimization — 2026-07-14

Source: read-only analysis of `data/trading_bot.db`. The database was not rewritten.

## Correct performance baseline

The store contains 651 exit events, but TP1/TP2 partial exits are not independent trades. Grouping by symbol, side, and open time produces 396 completed position lifecycles (plus two incomplete lifecycles).

| Metric | Completed lifecycles |
|---|---:|
| Positions | 396 |
| Wins / losses / flats | 206 / 187 / 3 |
| Economic win rate | 52.02% |
| Gross realized PnL | +1,147.47 USDT |
| Profit factor | 1.325 |
| Expectancy | +2.90 USDT/position |
| Maximum realized drawdown | 812.24 USDT |

The old event-level view was materially biased: partial exits made 651 events look like 651 trades. The final residual-close legs alone sum to about -338.92 USDT, while partial-exit legs sum to about +1,491.86 USDT. All reporting, cooldowns, learning records, and risk cohorts must therefore use whole-position PnL.

The edge is not yet statistically robust. A lifecycle bootstrap placed the 95% interval for expectancy at approximately -0.99 to +6.84 USDT, which includes zero.

## Costs and drift

Historical PnL was gross. A conservative reconstruction using the configured 0.055% taker fee per side gives about +514.41 USDT for all completed positions. Adding 0.02% adverse slippage per fill reduces the estimate to about +284.14 USDT. These are estimates, not exchange-accounting replacements.

Recent results are weaker:

| Completed cohort | Gross | Fee-only estimate | Fee + slippage estimate |
|---|---:|---:|---:|
| 2026-07 | +66.06 | -0.76 | -25.05 |
| From 2026-06-18 | +47.73 | -27.65 | -55.06 |

Most historical edge came from early/mid June, so live promotion should be based on a new cost-adjusted forward sample rather than the full-history headline.

## Cohort findings used

- `volume_trend=falling` was negative in every recent chronological slice; in July it had 5 positions, 20% win rate, and -26.25 USDT gross. It is now a hard deterministic rejection.
- `late_chase` was weak in July (21 positions, -44.15 USDT, PF about 0.59), but not uniformly bad over the full history. It is downsized to 25%, not hard-blocked.
- HIGH-risk positions lost about 275.67 USDT, and the high-conviction cohort lost about 388.05 USDT. HIGH remains blocked; high score no longer receives a size-up.
- Leverage 10x was the most stable profitable band. The 13–20x cohort had PF about 0.85 and >20x about 0.22. Every deterministic layer now caps leverage at 10x.
- After two consecutive losses on the same symbol and side, the next-position cohort was strongly negative (9 observations, about -126 USDT, PF about 0.15). A 72-hour reset is now required.
- Partial exits are economically important. The paper allocation is changed to 15% of initial at TP1, then 24% of the remainder at TP2, leaving about 64.6% as the runner.

## Applied paper configuration

- Keep `DRY_RUN=true`; include 0.055% taker fees and 0.02% slippage per fill.
- Use `SL_MODE=atr`; disable DCA.
- Risk the tighter of 10 USDT or 0.5% of equity per trade, including modeled costs.
- Cap leverage at 10x and block HIGH-risk signals.
- Reject falling-volume entries; size late-chase entries at 0.25x.
- Use one TP owner (the local tracker); exchange conditional TP duplicates are disabled.
- Use TP1 15%, TP2 24% of remaining, then the runner.
- Disable legacy AI learning and symbol-memory sizing until the residual-only historical memory is rebuilt from lifecycle totals.
- Tag every new entry, exit, memory result, and decision audit with `STRATEGY_VERSION` plus a non-secret parameter fingerprint so forward cohorts cannot mix configurations silently.

## Promotion gate

Do not enable live trading from this audit alone. First collect at least 100 new completed dry-run lifecycles under one configuration version. Promote only if the cost-adjusted sample has positive expectancy, PF above 1.15, bounded drawdown within the account risk budget, and no single short period contributing most of the profit.

The sanitized `env.example` contains the optimized paper defaults. Credentials previously present in that template should be rotated if they were real.
