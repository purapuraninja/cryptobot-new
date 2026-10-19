"""
agent/positions.py — open-position lifecycle management.

These coroutines run each scan against currently open positions. They may call
the exchange executor and the Telegram notifier, but they do NOT contain the
top-level scan loop and they do NOT open new positions (that's the entry
gauntlet in main.py). Extracted verbatim from main.py.

Contents:
  check_dca             — prompt user to average down at ROI/ATR triggers
  check_quick_exit      — failed-setup + reversal exits on open positions
  sync_live_positions   — reconcile pnl_store with actual Bybit positions
  refresh_symbols       — refresh Config.SYMBOLS by 24h turnover
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List


# ── DCA Logic ──────────────────────────────────────────────────────────────

async def check_dca(
    pnl: Any,
    market_data: List[Dict],
    executor: Any,
    notifier: Any,
    memory: Any,
    account_balance: float,
) -> None:
    """
    Check each open position for DCA opportunity.

    DCA conditions (SL_MODE=none):
    - ROI (leveraged PnL%) reaches DCA_ROI_TRIGGER (default -75%)
    - No more than 1 DCA per position

    DCA conditions (SL_MODE=atr, legacy mode):
    - Position unrealized price PnL < -2%
    - Price moved > 1x ATR from entry
    - HTF bias still supports direction

    If conditions met, send Telegram asking user y/n.
    """
    from config import Config
    from utils.logger import logger

    prices = {d["symbol"]: d for d in market_data}

    for sym, pos in list(pnl._store["open_positions"].items()):
        # Skip if already DCA'd
        if pos.get("dca_count", 0) >= 1:
            continue
        # Skip if user previously rejected DCA for this position (jawab N)
        # Timeout TIDAK set this flag, jadi tetap bisa ditanya ulang nanti.
        if pos.get("dca_declined", False):
            continue

        mkt = prices.get(sym)
        if mkt is None:
            continue

        entry = pos["entry_price"]
        action = pos["action"]
        leverage = float(pos.get("leverage", 1))
        current_price = mkt.get("current_price", 0)
        atr_pct = mkt.get("atr_pct", 0)
        htf_bias = mkt.get("htf_bias", "neutral")
        rsi = mkt.get("rsi", 50)

        if entry <= 0 or current_price <= 0:
            continue

        # Price change % (unleveraged)
        if action == "LONG":
            price_pct = (current_price - entry) / entry * 100
        else:
            price_pct = (entry - current_price) / entry * 100

        # ROI % = price change × leverage (what user actually sees on Bybit)
        roi_pct = price_pct * leverage

        # ── DCA trigger: ROI-based (SL_MODE=roi/none) or price-based (atr mode) ──
        if Config.SL_MODE in ("roi", "none"):
            # Trigger when ROI hits the configured threshold (default -75%)
            if roi_pct >= Config.DCA_ROI_TRIGGER:
                continue  # not losing enough yet

            dca_reasons = [
                f"ROI: {roi_pct:.1f}% (threshold: {Config.DCA_ROI_TRIGGER:.0f}%)",
                f"Price: {price_pct:.1f}% from entry ({leverage:.0f}x leverage)",
                f"HTF bias: {htf_bias}  |  RSI: {rsi:.0f}",
                f"Current: {current_price:,.4f}  |  Entry: {entry:,.4f}",
            ]
        else:
            # Legacy: price-based trigger
            # 1. Losing > 2% on price
            if price_pct >= -2.0:
                continue

            # 2. Price moved at least 1x ATR
            if atr_pct > 0:
                move_pct = abs((current_price - entry) / entry * 100)
                if move_pct < atr_pct:
                    continue

            # 3. HTF still supports direction
            if action == "LONG" and htf_bias == "bearish":
                continue
            if action == "SHORT" and htf_bias == "bullish":
                continue

            # 4. RSI not extreme
            if action == "LONG" and rsi < 20:
                continue
            if action == "SHORT" and rsi > 80:
                continue

            dca_reasons = [
                f"Price drop {price_pct:.1f}% from entry (ROI: {roi_pct:.1f}%)",
                f"HTF bias: {htf_bias}  |  RSI: {rsi:.0f}",
            ]
            if action == "LONG" and rsi < 40:
                dca_reasons.append("RSI oversold zone, potential reversal")
            elif action == "SHORT" and rsi > 60:
                dca_reasons.append("RSI overbought zone, potential reversal")
        dca_reasons.append(f"Current: {current_price:,.4f} | Entry: {entry:,.4f}")

        reason_text = "\n".join(dca_reasons)

        # Ask user via Telegram
        roi_display = f"{roi_pct:.1f}%"
        question = (
            f"⚠️ <b>DCA {action} {sym}?</b>\n\n"
            f"ROI: <b>{roi_display}</b> ({leverage:.0f}x leverage)\n"
            f"{reason_text}\n\n"
            f"DCA +{Config.DCA_SIZE_PCT}% "
            f"({account_balance * Config.DCA_SIZE_PCT / 100:,.2f} USDT)\n"
            f"→ akan menurunkan avg entry\n\n"
            f"Reply <b>y</b> atau <b>n</b> ({Config.DCA_TIMEOUT}s timeout)"
        )

        logger.info(f"[DCA] Asking user for {sym}: ROI={roi_pct:.1f}% price={price_pct:.1f}%")
        answer = await notifier.ask_yes_no(question, Config.DCA_TIMEOUT)

        if answer is True:
            # Execute DCA
            leverage = pos.get("leverage", 5)
            dca_result = await executor.execute_dca(
                sym, action, leverage, Config.DCA_SIZE_PCT
            )
            if dca_result:
                # Update position in pnl_tracker
                pos["dca_count"] = pos.get("dca_count", 0) + 1
                old_notional = pos["notional"]
                dca_notional = account_balance * (Config.DCA_SIZE_PCT / 100.0) * leverage
                new_notional = old_notional + dca_notional

                # Average entry price
                dca_price = dca_result.get("price", current_price)
                new_entry = (
                    (entry * old_notional + dca_price * dca_notional) / new_notional
                )
                pos["entry_price"] = round(new_entry, 8)
                pos["notional"] = round(new_notional, 2)

                from utils.pnl_tracker import _save_store
                _save_store(pnl._store)

                # Update SL to -100% ROI from new avg entry (SL_MODE=roi)
                new_sl_price = None
                if Config.SL_MODE == "roi" and hasattr(executor, "update_sl_roi"):
                    lev = float(pos.get("leverage", leverage))
                    new_sl_price = await executor.update_sl_roi(
                        sym, action, new_entry, lev
                    )
                    if new_sl_price:
                        pos["stop_loss"] = new_sl_price
                        pos["original_sl"] = new_sl_price
                        from utils.pnl_tracker import _save_store
                        _save_store(pnl._store)

                sl_line = (
                    f"New SL: <code>{new_sl_price:,.6f}</code> (-100% ROI)\n"
                    if new_sl_price else ""
                )
                await notifier.send(
                    f"<b>DCA EXECUTED</b> {action} {sym}\n"
                    f"Added: +{Config.DCA_SIZE_PCT}%\n"
                    f"New avg entry: <code>{new_entry:,.4f}</code>\n"
                    f"{sl_line}"
                    f"New notional: <code>{new_notional:,.2f}</code> USDT"
                )
                logger.info(
                    f"[DCA] Executed {sym}: new_entry={new_entry:,.4f} "
                    f"notional={new_notional:,.2f} new_sl={new_sl_price}"
                )
            else:
                await notifier.send(f"[DCA] Order failed for {sym}")
        elif answer is False:
            # User reply N — set flag so we don't ask again for this position
            pos["dca_declined"] = True
            from utils.pnl_tracker import _save_store
            _save_store(pnl._store)
            logger.info(f"[DCA] User rejected DCA for {sym} — won't ask again")
            await notifier.send(f"DCA {sym} skipped (won't ask again for this position).")
        else:
            # Timeout — DON'T set flag, let it ask again next cycle
            logger.info(f"[DCA] Timeout for {sym}, will retry next cycle")
            await notifier.send(f"DCA {sym} timeout — will ask again next scan.")


# ── P3: Quick-Exit on Reversal ─────────────────────────────────────────────

async def check_quick_exit(
    pnl: Any,
    market_data: List[Dict],
    executor: Any,
    notifier: Any,
    memory: Any,
) -> None:
    """
    Force close a position if technical signals reverse before SL/TP hit.

    Triggers (ALL must be true for safety):
      1. Position age >= QUICK_EXIT_MIN_HOURS (avoid premature exits)
      2. RSI flipped against the position direction:
           LONG  → RSI < QUICK_EXIT_RSI_LONG (e.g. < 35)
           SHORT → RSI > QUICK_EXIT_RSI_SHORT (e.g. > 65)
      3. Market structure flipped against position:
           LONG  → structure == "bearish"
           SHORT → structure == "bullish"
      4. Position is currently in PROFIT (avoid converting paper loss to real loss).
         This is a safety floor — quick-exit is meant to lock in gains, not panic.

    If all conditions met → close 100% via executor and record close.
    """
    from config import Config
    from utils.logger import logger

    if not getattr(Config, "QUICK_EXIT_ENABLED", False):
        return

    rsi_long_thr  = getattr(Config, "QUICK_EXIT_RSI_LONG", 35.0)
    rsi_short_thr = getattr(Config, "QUICK_EXIT_RSI_SHORT", 65.0)
    min_hours     = getattr(Config, "QUICK_EXIT_MIN_HOURS", 1.0)

    mkt_map = {d["symbol"]: d for d in market_data}

    for sym, pos in list(pnl._store.get("open_positions", {}).items()):
        mkt = mkt_map.get(sym)
        if not mkt:
            continue

        # 1. Age check (UTC-aware: open_time stored in UTC by _now_str)
        try:
            open_dt = datetime.strptime(pos.get("open_time", ""), "%Y-%m-%d %H:%M:%S")
            open_dt = open_dt.replace(tzinfo=timezone.utc)
            hours_held = (datetime.now(timezone.utc) - open_dt).total_seconds() / 3600.0
        except Exception:
            continue

        action = pos.get("action", "")
        entry  = float(pos.get("entry_price", 0))
        lev    = float(pos.get("leverage", 1))
        price  = float(mkt.get("current_price", 0))
        rsi    = float(mkt.get("rsi", 50))
        structure = str(mkt.get("market_structure", "")).lower()

        if entry <= 0 or price <= 0:
            continue

        if action == "LONG":
            price_pct = (price - entry) / entry * 100.0
        else:
            price_pct = (entry - price) / entry * 100.0
        roi_pct = price_pct * lev

        failed_setup_enabled = getattr(Config, "FAILED_SETUP_EXIT_ENABLED", True)
        failed_min_m = getattr(Config, "FAILED_SETUP_MIN_MINUTES", 5.0)
        failed_max_m = getattr(Config, "FAILED_SETUP_MAX_MINUTES", 30.0)
        failed_roi = getattr(Config, "FAILED_SETUP_MAX_ROI_LOSS_PCT", -10.0)
        minutes_held = hours_held * 60.0
        structure_supports = (
            (action == "LONG" and structure in ("uptrend", "breakout")) or
            (action == "SHORT" and structure in ("downtrend", "breakdown"))
        )
        if (
            failed_setup_enabled
            and failed_min_m <= minutes_held <= failed_max_m
            and roi_pct <= failed_roi
            and not structure_supports
        ):
            logger.info(
                f"  [FAILED-SETUP] {sym} {action} early reversal: "
                f"ROI={roi_pct:+.1f}% struct={structure} held={minutes_held:.0f}m"
            )

            live_ok = True
            actual_fill_px = 0.0
            if hasattr(executor, "partial_close"):
                try:
                    res = await executor.partial_close(sym, action, 100.0)
                    live_ok = bool(res) and not (res.get("dry_run") and not Config.DRY_RUN)
                    if res:
                        actual_fill_px = float(res.get("price", 0) or 0)
                except Exception as exc:
                    logger.error(f"  [FAILED-SETUP] {sym} executor close error: {exc}")
                    live_ok = False

            if not live_ok and not Config.DRY_RUN:
                logger.warning(
                    f"  [FAILED-SETUP] {sym} Bybit close FAILED -- keeping tracker open"
                )
                continue

            exit_px_use = actual_fill_px if actual_fill_px > 0 else price
            rec = pnl.record_close(sym, exit_px_use, reason="failed_setup_reversal")
            if rec:
                await notifier.send(
                    f"<b>Failed Setup Exit</b> {action} {sym}\n"
                    f"Struct: {structure}  Held: {minutes_held:.0f}m\n"
                    f"ROI: {roi_pct:+.1f}%\n"
                    f"Position PnL: <b>{rec.get('position_pnl_usdt', rec['pnl_usdt']):+.4f}</b> "
                    f"USDT ({rec.get('position_pnl_pct', rec['pnl_pct']):+.2f}%)"
                )
                if memory:
                    memory.record_trade_result(
                        symbol=sym,
                        exit_price=rec.get("exit_price", exit_px_use),
                        pnl_usdt=rec.get("position_pnl_usdt", rec["pnl_usdt"]),
                        pnl_pct=rec.get("position_pnl_pct", rec.get("pnl_pct", 0)),
                        reason="failed_setup_reversal",
                        gross_pnl_usdt=rec.get(
                            "position_gross_pnl_usdt", rec.get("gross_pnl_usdt")
                        ),
                        fees_usdt=rec.get(
                            "position_fees_usdt", rec.get("fees_usdt", 0)
                        ),
                    )
            continue

        if hours_held < min_hours:
            continue

        # 2. RSI flip
        rsi_flip = False
        if action == "LONG"  and rsi < rsi_long_thr:
            rsi_flip = True
        if action == "SHORT" and rsi > rsi_short_thr:
            rsi_flip = True
        if not rsi_flip:
            continue

        # 3. Structure flip
        structure_flip = (
            (action == "LONG" and structure in ("downtrend", "breakdown")) or
            (action == "SHORT" and structure in ("uptrend", "breakout"))
        )
        if not structure_flip:
            continue

        # 4. Must be in profit (safety floor)
        if roi_pct <= 0:
            continue   # not yet profitable — let SL handle it

        logger.info(
            f"  [QUICK-EXIT] {sym} {action} reversal detected: "
            f"RSI={rsi:.0f} struct={structure} ROI={roi_pct:+.1f}% held={hours_held:.1f}h"
        )

        # Execute close (guard exception seperti failed-setup path — call tak
        # ter-wrap dulu bisa mematikan seluruh scan loop)
        live_ok = True
        actual_fill_px = 0.0
        actual_fees = None
        if hasattr(executor, "partial_close"):
            try:
                res = await executor.partial_close(sym, action, 100.0)
                live_ok = bool(res)
                if res:
                    actual_fill_px = float(res.get("price", 0) or 0)
                    if res.get("fees") is not None:
                        actual_fees = float(res["fees"])
            except Exception as exc:
                logger.error(f"  [QUICK-EXIT] {sym} executor close error: {exc}")
                live_ok = False

        # Prefer actual VWAP fill from Bybit; fall back to market data price.
        exit_px_use = actual_fill_px if actual_fill_px > 0 else price
        if not live_ok and not Config.DRY_RUN:
            logger.warning(
                f"  [QUICK-EXIT] {sym} exchange close failed -- keeping tracker open"
            )
            continue
        rec = pnl.record_close(
            sym, exit_px_use, reason="quick_exit_reversal", actual_fees=actual_fees
        )
        if rec:
            await notifier.send(
                f"⚡ <b>Quick-Exit (Reversal)</b> {action} {sym}\n"
                f"RSI: {rsi:.0f}  Struct: {structure}\n"
                f"Held: {hours_held:.1f}h  ROI locked: {roi_pct:+.1f}%\n"
                f"Position PnL: <b>{rec.get('position_pnl_usdt', rec['pnl_usdt']):+.4f}</b> "
                f"USDT ({rec.get('position_pnl_pct', rec['pnl_pct']):+.2f}%)"
            )
            if memory:
                memory.record_trade_result(
                    symbol=sym,
                    exit_price=rec.get("exit_price", exit_px_use),
                    pnl_usdt=rec.get("position_pnl_usdt", rec["pnl_usdt"]),
                    pnl_pct=rec.get("position_pnl_pct", rec.get("pnl_pct", 0)),
                    reason="quick_exit_reversal",
                    gross_pnl_usdt=rec.get(
                        "position_gross_pnl_usdt", rec.get("gross_pnl_usdt")
                    ),
                    fees_usdt=rec.get(
                        "position_fees_usdt", rec.get("fees_usdt", 0)
                    ),
                )


# ── Live Position Sync ─────────────────────────────────────────────────────

async def sync_live_positions(
    executor: Any,
    pnl: Any,
    memory: Any = None,
    notifier: Any = None,
) -> None:
    """
    Reconcile pnl_store with actual Bybit positions.
    Called at startup and every scan in live mode.
    """
    from config import Config
    from utils.logger import logger

    if Config.DRY_RUN:
        return

    # Fetch actual open positions from Bybit
    try:
        bybit_open = await executor.get_open_positions()
    except Exception as exc:
        logger.error(
            f"[Sync] Open-position fetch failed; reconciliation skipped: {exc}"
        )
        return
    if bybit_open is None:
        logger.error("[Sync] Open-position fetch returned no result; reconciliation skipped")
        return

    # Fetch closed PnL for symbols we think are still open
    closed_pnl: List[Dict] = []
    store_syms = list(pnl._store["open_positions"].keys())
    bybit_syms = {p.get("symbol", "") for p in bybit_open}

    for sym in store_syms:
        if sym not in bybit_syms:
            # This position might have been closed — fetch its close data
            try:
                c = await executor.get_closed_pnl(sym, limit=5)
                closed_pnl.extend(c)
            except Exception as exc:
                logger.warning(f"[Sync] Closed-PnL lookup failed for {sym}: {exc}")

    # Run sync
    sync_result = pnl.sync_positions(bybit_open, closed_pnl)

    added = sync_result.get("added", [])
    updated = sync_result.get("updated", [])
    closed = sync_result.get("closed", [])

    if added or updated or closed:
        logger.info(
            f"[Sync] Result: added={added} updated={updated} closed={closed}"
        )

        # ── SAFETY: attach SL to orphan synced positions ──
        # If sync added a position with no SL on Bybit (needs_sl_attach flag),
        # immediately set SL at -100% ROI from current entry.
        # This prevents TRUMPUSDT-style liquidation (-160% ROI) where bot has
        # no awareness of position and Bybit has no protective stop.
        for sym in added:
            pos = pnl._store["open_positions"].get(sym, {})
            if pos.get("needs_sl_attach") and hasattr(executor, "update_sl_roi"):
                action = pos.get("action", "")
                entry  = float(pos.get("entry_price", 0))
                lev    = float(pos.get("leverage", 1))
                logger.warning(
                    f"[Sync][SAFETY] {sym} has NO SL on Bybit — "
                    f"attaching ROI-based SL @ entry={entry:.6f} lev={lev}x"
                )
                try:
                    new_sl = await executor.update_sl_roi(sym, action, entry, lev)
                    if new_sl:
                        pos["stop_loss"] = new_sl
                        pos["original_sl"] = new_sl
                        pos["needs_sl_attach"] = False
                        from utils.pnl_tracker import _save_store
                        _save_store(pnl._store)
                        logger.info(
                            f"[Sync][SAFETY] {sym} SL attached @ {new_sl:.6f}"
                        )
                        if notifier:
                            await notifier.send(
                                f"⚠️ <b>SL Auto-Attached</b> {action} {sym}\n"
                                f"Posisi sync dari Bybit tanpa SL → "
                                f"set ke <code>{new_sl:.6f}</code> (-100% ROI)"
                            )
                except Exception as exc:
                    logger.error(f"[Sync][SAFETY] {sym} SL attach failed: {exc}")

        # Notify via Telegram
        if notifier:
            parts = []
            if added:
                parts.append(f"Added: {', '.join(added)}")
            if updated:
                parts.append(f"Updated: {', '.join(updated)}")
            if closed:
                parts.append(f"Closed: {', '.join(closed)}")
            await notifier.send(
                f"<b>Position Sync</b>\n" + "\n".join(parts)
            )

        # Record closed positions in trade memory
        if memory and closed:
            for sym in closed:
                closed_trade = next(
                    (t for t in reversed(pnl._store["closed_trades"])
                     if t["symbol"] == sym),
                    None,
                )
                if closed_trade:
                    memory.record_trade_result(
                        symbol=sym,
                        exit_price=closed_trade.get("exit_price", 0),
                        pnl_usdt=closed_trade.get(
                            "position_pnl_usdt", closed_trade.get("pnl_usdt", 0)
                        ),
                        pnl_pct=closed_trade.get(
                            "position_pnl_pct", closed_trade.get("pnl_pct", 0)
                        ),
                        reason=closed_trade.get("reason", "closed_on_exchange"),
                        gross_pnl_usdt=closed_trade.get(
                            "position_gross_pnl_usdt", closed_trade.get("gross_pnl_usdt")
                        ),
                        fees_usdt=closed_trade.get(
                            "position_fees_usdt", closed_trade.get("fees_usdt", 0)
                        ),
                    )


# ── Dynamic symbol refresh ─────────────────────────────────────────────────

async def refresh_symbols(pnl: Any) -> bool:
    """
    Fetch top-N symbols by volume and update Config.SYMBOLS in-place.
    Always retains symbols with open positions so SL/TP monitoring is never lost.
    Returns True if the list changed.
    """
    from config import Config
    from data.market_feed import fetch_top_symbols
    from utils.logger import logger

    try:
        new_syms = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: fetch_top_symbols(
                limit=Config.AUTO_SYMBOLS_COUNT,
                min_turnover_usdt=Config.AUTO_SYMBOLS_MIN_TURNOVER_USD,
                blacklist=Config.SYMBOL_BLACKLIST,
                data_only=Config.DATA_ONLY_SYMBOLS,
            )
        )
    except Exception as exc:
        logger.error(f"[SymbolFeed] Refresh failed: {exc}")
        return False

    if not new_syms:
        logger.warning("[SymbolFeed] Empty result — keeping current symbol list")
        return False

    # Always keep symbols with open positions
    open_syms = set(pnl._store.get("open_positions", {}).keys())
    for s in open_syms:
        if s not in new_syms:
            new_syms.append(s)
            logger.info(f"[SymbolFeed] Retained {s} (open position)")

    old_set = set(Config.SYMBOLS)
    new_set = set(new_syms)
    added   = new_set - old_set
    removed = old_set - new_set

    Config.SYMBOLS = new_syms

    if added or removed:
        logger.info(
            f"[SymbolFeed] Updated: {len(new_syms)} symbols  "
            f"(+{len(added)} added, -{len(removed)} removed)"
        )
        if added:
            logger.info(f"[SymbolFeed]   Added  : {sorted(added)[:20]}")
        if removed:
            logger.info(f"[SymbolFeed]   Removed: {sorted(removed)[:20]}")
        return True

    logger.info(f"[SymbolFeed] No change ({len(new_syms)} symbols)")
    return False
