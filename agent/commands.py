"""
agent/commands.py — Telegram command processing.

Polls Telegram for user commands and executes them immediately against the
exchange + PnL tracker. Extracted verbatim from main.py.

Supported commands:
  close SYMBOL PCT  → partial/full close via Bybit market order
  positions         → show current open positions
  perf              → performance breakdown by AI tier & symbol
  help              → show command list
"""

import asyncio
from typing import Any


async def process_telegram_commands(
    notifier: Any, executor: Any, pnl: Any
) -> None:
    """
    Poll Telegram for user commands and execute them immediately.

    Supported:
      close SYMBOL PCT  → partial/full close via Bybit market order
      positions         → show current open positions
      help              → show command list
    """
    from utils.logger import logger

    commands = await notifier.check_commands()
    if not commands:
        return

    open_positions = pnl._store.get("open_positions", {})

    for cmd in commands:
        ctype = cmd.get("type")

        # ── help ──────────────────────────────────────────────
        if ctype == "help":
            await notifier.send_help()

        # ── positions ──────────────────────────────────────────
        elif ctype == "positions":
            await notifier.send_positions(open_positions)

        # ── perf — performance breakdown by AI tier & symbol ─────
        elif ctype == "perf":
            try:
                breakdown = pnl.get_performance_breakdown()
                lines = ["<b>Performance Breakdown</b>\n"]

                # By AI tier
                lines.append("<u>By AI Tier:</u>")
                tiers = sorted(
                    breakdown["by_tier"].items(),
                    key=lambda kv: -kv[1]["pnl"],
                )
                for tier, d in tiers:
                    lines.append(
                        f"  <code>{tier}</code>\n"
                        f"    Trades: {d['trades']}  WR: {d['win_rate']:.0f}%  "
                        f"PnL: <b>{d['pnl']:+.4f}</b> USDT\n"
                        f"    Best: {d['best']:+.4f}  Worst: {d['worst']:+.4f}"
                    )

                lines.append("\n<u>By Symbol (top 8 by PnL):</u>")
                syms = sorted(
                    breakdown["by_symbol"].items(),
                    key=lambda kv: -kv[1]["pnl"],
                )[:8]
                for sym, d in syms:
                    lines.append(
                        f"  <code>{sym}</code>: {d['trades']}t "
                        f"WR{d['win_rate']:.0f}% PnL <b>{d['pnl']:+.4f}</b>"
                    )

                lines.append("\n<u>Worst symbols (bottom 5):</u>")
                worst = sorted(
                    breakdown["by_symbol"].items(),
                    key=lambda kv: kv[1]["pnl"],
                )[:5]
                for sym, d in worst:
                    if d["pnl"] >= 0:
                        break
                    lines.append(
                        f"  <code>{sym}</code>: {d['trades']}t "
                        f"WR{d['win_rate']:.0f}% PnL {d['pnl']:+.4f}"
                    )

                await notifier.send("\n".join(lines))
            except Exception as exc:
                await notifier.send(f"perf error: {exc}")

        # ── close SYMBOL PCT ──────────────────────────────────
        elif ctype == "close":
            sym = cmd["symbol"]
            pct = cmd["pct"]

            # Validate: symbol must be in open positions
            if sym not in open_positions:
                # Try without USDT suffix match
                matches = [k for k in open_positions if sym in k]
                if len(matches) == 1:
                    sym = matches[0]
                else:
                    await notifier.send(
                        f"No open position for <code>{sym}</code>.\n"
                        f"Open: {', '.join(f'<code>{s}</code>' for s in open_positions) or 'none'}"
                    )
                    continue

            pos    = open_positions[sym]
            action = pos.get("action", "LONG")
            entry  = pos.get("entry_price", 0)
            lev    = pos.get("leverage", 1)

            await notifier.send(
                f"Closing <code>{sym}</code> {pct:.0f}%...\n"
                f"Side: {action}  Entry: {entry:.6f}  Lev: {lev}x"
            )

            if hasattr(executor, "partial_close"):
                result = await executor.partial_close(sym, action, pct)
            else:
                result = None

            if result and not result.get("dry_run"):
                closed_qty = result.get("closed_qty", "?")
                total_qty  = result.get("total_qty", "?")

                # Update pnl tracker: full close or mark partial
                if pct >= 99.9:
                    # Full close — exit price priority chain:
                    #   1. Executor's VWAP fill from /v5/execution/list (authoritative)
                    #   2. Bybit ticker lastPrice (approximation, slippage possible)
                    #   3. Entry price (last resort — flag for later reconciliation)
                    exit_price = float(result.get("price", 0) or 0)
                    src = "execution_list" if exit_price > 0 else None

                    if exit_price <= 0:
                        # Fallback: query last price from Bybit ticker
                        try:
                            tick = await asyncio.to_thread(
                                executor._get,
                                "/v5/market/tickers",
                                {"category": "linear", "symbol": executor._to_bybit(sym)},
                            )
                            if tick and tick.get("list"):
                                exit_price = float(tick["list"][0].get("lastPrice", 0))
                                if exit_price > 0:
                                    src = "ticker_lastprice"
                        except Exception as e:
                            logger.error(
                                f"[CMD] {sym} close: ticker fallback failed: {e}"
                            )

                    close_reason = "manual_close_100pct"
                    if exit_price <= 0:
                        exit_price = entry  # last-resort, will give $0 PnL
                        src = "entry_fallback"
                        # Mark reason so reconciliation script can find these later
                        close_reason = "manual_close_100pct_unreconciled"
                        logger.error(
                            f"[CMD] {sym} close: BOTH execution_list AND ticker failed. "
                            f"PnL recorded as $0 — check Bybit and run reconciliation."
                        )
                    else:
                        logger.info(
                            f"[CMD] {sym} close: exit_price={exit_price} (src={src})"
                        )
                    trade = pnl.record_close(sym, exit_price, reason=close_reason)
                    if trade:
                        await notifier.alert_pnl_close(trade)
                    else:
                        await notifier.send(
                            f"Closed <code>{sym}</code> {closed_qty}/{total_qty} qty\n"
                            f"(PnL tracked on Bybit — check app)"
                        )
                else:
                    await notifier.send(
                        f"Closed <code>{sym}</code> {pct:.0f}%\n"
                        f"Qty: {closed_qty} of {total_qty}\n"
                        f"Remaining position still open. Check Bybit for realized PnL."
                    )

                logger.info(
                    f"[CMD] Close {sym} {pct:.0f}%: "
                    f"qty={closed_qty}/{total_qty} action={action}"
                )

            elif result and result.get("dry_run"):
                await notifier.send(
                    f"[DRY RUN] Would close <code>{sym}</code> {pct:.0f}% — no real order sent."
                )
            else:
                await notifier.send(
                    f"Close <code>{sym}</code> {pct:.0f}% FAILED.\n"
                    f"Check logs for details."
                )
