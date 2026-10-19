"""
main.py — AI Quant Trading Bot v2.0

DATA LAYER      MarketFeed  — direct HTTP OHLCV (Bybit/OKX/Gate.io)
ANALYZER LAYER  SignalEngine — AI quant signal (multi-provider)
EXECUTION LAYER CEXExecutor (Bybit futures) | DEXExecutor (Hyperliquid)
PNL TRACKER     PnLTracker  — dry-run simulation + live stats
MEMORY          TradeMemory — AI learning + adaptive risk
"""
import sys
import os
from dotenv import load_dotenv

load_dotenv()

# Fix Windows console encoding for Unicode box-drawing characters
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
from pathlib import Path
from typing import Any, Dict, List
from datetime import datetime

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)
(_ROOT / "logs").mkdir(exist_ok=True)

# ── Modular building blocks (extracted from the former monolith) ───────
# Risk stage (deterministic pre-execution gate + audit trail)
from risk.decision_engine import (
    apply_expectancy_filter_and_sizing,
    apply_max_loss_cap,
    calculate_admission_rr,
    calculate_weighted_rr,
    materialize_effective_stop,
    write_decision_audit,
)
# Presentation
from ui.console import _clr, print_dashboard
# AI signal payload helpers
from agent.signals import extract_signals
# Filters / regime gates / adaptive rules
from agent.filters import (
    daily_realized_loss as _daily_realized_loss,
    global_regime_quality as _global_regime_quality,
    passes_vol_regime_filter as _passes_vol_regime_filter,
    symbol_trade_count_today as _symbol_trade_count_today,
    symbol_hours_since_last_loss as _symbol_hours_since_last_loss,
    symbol_side_loss_streak as _symbol_side_loss_streak,
    apply_adaptive_rules as _apply_adaptive_rules,
    passes_smc_retest_filter as _passes_smc_retest_filter,
)
# Open-position lifecycle
from agent.positions import (
    check_dca as _check_dca,
    check_quick_exit as _check_quick_exit,
    sync_live_positions as _sync_live_positions,
    refresh_symbols as _refresh_symbols,
)
# Telegram command processing
from agent.commands import process_telegram_commands as _process_telegram_commands

# ── Agent loop ─────────────────────────────────────────────────────────────

async def run_agent(feed: Any, engine: Any, executor: Any,
                    notifier: Any, pnl: Any, memory: Any = None,
                    onchain: Any = None) -> None:
    from utils.logger import logger
    from config import Config

    scan_count = 0
    account_balance = Config.ACCOUNT_BALANCE
    _initial_balance = Config.ACCOUNT_BALANCE  # fixed baseline for dry-run simulation
    _last_symbol_refresh: float = 0.0   # epoch seconds

    while True:
        import time as _time
        scan_count += 1
        logger.info(f"SCAN #{scan_count:04d} starting ...")

        # ── Auto-refresh symbol list ──────────────────────────────────────
        if Config.AUTO_SYMBOLS:
            refresh_interval = Config.AUTO_SYMBOLS_REFRESH_H * 3600
            if _time.time() - _last_symbol_refresh >= refresh_interval:
                await _refresh_symbols(pnl)
                _last_symbol_refresh = _time.time()

        # Fetch / compute balance each scan
        if not Config.DRY_RUN and hasattr(executor, "get_balance"):
            # Live mode: fetch real balance from exchange
            real_balance = await executor.get_balance()
            if real_balance is not None and real_balance > 0:
                account_balance = real_balance
                pnl.account_balance = account_balance
        elif Config.DRY_RUN:
            # Dry-run mode: simulate balance = initial + cumulative realized PnL
            realized = pnl._store["stats"].get("total_realized", 0.0)
            account_balance = max(_initial_balance + realized, 0.0)  # floor at 0 only (no artificial minimum)
            pnl.account_balance = account_balance
            logger.debug(f"[DryRun] Balance updated: {_initial_balance:.2f} + {realized:.2f} = {account_balance:.2f} USDT")

        # 1. Fetch market data
        # When all position slots are full, only fetch symbols with open positions
        # (needed for SL/TP monitoring). Skip the full universe to save API calls.
        market_data: List[Dict] = []
        data_sources: Dict[str, str] = {}

        open_syms_now  = set(pnl._store.get("open_positions", {}).keys())
        slots_available = Config.MAX_OPEN_POSITIONS - len(open_syms_now)

        universe_limited = False
        if slots_available <= 0 and Config.AUTO_SYMBOLS:
            # Full — only scan open positions
            scan_symbols = list(open_syms_now) if open_syms_now else Config.SYMBOLS[:1]
            universe_limited = True   # market_data cacat: tanpa BTC anchor & whitelist
            logger.info(
                f"[SymbolFeed] Slots full — scanning only {len(scan_symbols)} "
                f"open position(s) for SL/TP monitoring"
            )
        else:
            # Whitelist + ALWAYS include open positions (even if not in whitelist).
            # Without this, positions opened on now-removed symbols (e.g. ZECUSDT
            # after whitelist update) would have current_price=0 in dashboard
            # and SL/TP/trail/time-exit logic would never fire (no market_data).
            #
            # Data-only symbols (mis. BTCUSDT) di-fetch untuk konteks AI
            # (BTC global bias) tapi DI-EKSEKUSI sebagai HOLD di entry loop
            # via DATA_ONLY_SYMBOLS check. Prevent BTC trade tapi tetap suplai
            # bias data ke prompt.
            whitelist = [s.strip() for s in Config.SYMBOLS if s.strip()]
            data_only = [s.strip() for s in Config.DATA_ONLY_SYMBOLS if s.strip()]
            scan_symbols = list(dict.fromkeys(
                whitelist + data_only + list(open_syms_now)
            ))
            orphans = open_syms_now - set(whitelist) - set(data_only)
            if orphans:
                logger.info(
                    f"[SymbolFeed] Including {len(orphans)} non-whitelist "
                    f"open position(s) for monitoring: {sorted(orphans)}"
                )
            if data_only:
                logger.debug(
                    f"[SymbolFeed] Data-only (no trade): {data_only}"
                )

        tasks = {s: feed.fetch_symbol_data(s) for s in scan_symbols}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        for symbol, result in zip(tasks.keys(), results):
            if isinstance(result, Exception) or result is None:
                continue
            data_sources[result["symbol"]] = result.get("_source", "?")
            logger.info(
                f"  OK  {result['symbol']:<14s} "
                f"price={result['current_price']:>14,.4f}  "
                f"RSI={result['rsi']:5.1f}  "
                f"vol={result['volume_condition']:6s}  "
                f"struct={result['market_structure']}"
            )
            market_data.append(result)

        if not market_data:
            logger.warning("No market data this cycle.")
            stats = pnl.get_stats()
            stats["unrealized_pnl"] = 0.0
            print_dashboard(
                scan_count, [], 0, account_balance,
                Config.EXCHANGE_MODE, Config.DRY_RUN, Config.SCAN_INTERVAL,
                {}, stats, [], pnl.get_recent_trades()
            )
            await asyncio.sleep(Config.SCAN_INTERVAL)
            continue

        logger.info(f"Fetched {len(market_data)}/{len(tasks)} symbols")

        # 1b. Live position sync — reconcile pnl_store with Bybit
        if not Config.DRY_RUN:
            await _sync_live_positions(executor, pnl, memory, notifier)

        # 2. Check SL/TP/Trailing for positions (dry-run AND live)
        closed_trades = pnl.check_sl_tp(market_data)
        for trade in closed_trades:
            reason = trade.get("reason", "unknown")
            sym   = trade.get("symbol", "?")
            act   = trade.get("action", "")

            # ── Time-exit: ASK USER via Telegram (no auto-close) ──
            # Triggered when LOSING position held >= MAX_HOLD or STAGNANT.
            # User decides y/n. Y=close, N=cooldown 6h re-prompt, timeout=re-ask.
            # SL/Trail/TP/DCA logic still active independently.
            if trade.get("requires_user_decision"):
                hh = trade.get("hours_held", 0)
                roi_at = trade.get("roi_at_exit", 0)
                exit_px = float(trade.get("exit_price", 0))
                pos = pnl._store["open_positions"].get(sym)
                if not pos:
                    continue   # position gone (closed by other path) — skip

                label = "TIME-EXIT" if reason == "time_exit_max" else "STAGNANT"

                question = (
                    f"⏱ <b>{label} {act} {sym}?</b>\n\n"
                    f"Held: <b>{hh}h</b>  ROI: <b>{roi_at:+.1f}%</b>\n"
                    f"Reason: <code>{reason}</code>\n"
                    f"Current price: <code>{exit_px:.6f}</code>\n"
                    f"Entry: <code>{pos.get('entry_price', 0):.6f}</code>\n\n"
                    f"<b>Close 100% sekarang?</b>\n"
                    f"Reply <b>y</b> = close | <b>n</b> = hold (re-prompt 6h lagi)\n"
                    f"Timeout 120s = re-ask scan berikutnya"
                )

                logger.info(
                    f"  [USER-DECISION] {sym} {label} — asking user "
                    f"(held={hh}h ROI={roi_at:+.1f}%)"
                )
                answer = await notifier.ask_yes_no(question, 120)

                if answer is True:
                    # User says close
                    logger.info(f"  [USER-DECISION] {sym} {label} — user CLOSE confirmed")
                    live_ok = True
                    actual_fill_px = 0.0
                    actual_fees = None
                    if hasattr(executor, "partial_close"):
                        try:
                            res = await executor.partial_close(sym, act, 100.0)
                            live_ok = bool(res) and not (res.get("dry_run") and not Config.DRY_RUN)
                            if res:
                                actual_fill_px = float(res.get("price", 0) or 0)
                                if res.get("fees") is not None:
                                    actual_fees = float(res["fees"])
                        except Exception as exc:
                            logger.error(f"  [USER-DECISION] {sym} executor close error: {exc}")
                            live_ok = False
                    if live_ok or Config.DRY_RUN:
                        # Prefer actual VWAP fill from Bybit; fall back to market data price.
                        exit_px_use = actual_fill_px if actual_fill_px > 0 else exit_px
                        rec = pnl.record_close(
                            sym, exit_px_use, reason=reason, actual_fees=actual_fees
                        )
                        if rec:
                            await notifier.send(
                                f"✅ <b>Closed by user</b> {act} {sym}\n"
                                f"Held: {hh}h  ROI: {roi_at:+.1f}%\n"
                                f"Position PnL: <b>{rec.get('position_pnl_usdt', rec['pnl_usdt']):+.4f}</b> "
                                f"USDT ({rec.get('position_pnl_pct', rec['pnl_pct']):+.2f}%)"
                            )
                            if memory:
                                memory.record_trade_result(
                                    symbol=sym, exit_price=rec.get("exit_price", exit_px_use),
                                    pnl_usdt=rec.get("position_pnl_usdt", rec["pnl_usdt"]),
                                    pnl_pct=rec.get("position_pnl_pct", rec.get("pnl_pct", 0)),
                                    reason=reason,
                                    gross_pnl_usdt=rec.get(
                                        "position_gross_pnl_usdt", rec.get("gross_pnl_usdt")
                                    ),
                                    fees_usdt=rec.get(
                                        "position_fees_usdt", rec.get("fees_usdt", 0)
                                    ),
                                )
                elif answer is False:
                    # User says hold — set cooldown 6h before re-prompt
                    import time as _time
                    pos["time_exit_declined_at"] = _time.time()
                    from utils.pnl_tracker import _save_store
                    _save_store(pnl._store)
                    logger.info(
                        f"  [USER-DECISION] {sym} {label} — user HOLD, "
                        f"re-prompt in 6h"
                    )
                    await notifier.send(
                        f"⏸ <b>{label} {sym}</b> — hold per user. "
                        f"Re-prompt in 6h."
                    )
                else:
                    # Timeout — don't set flag, will re-ask next scan
                    logger.info(
                        f"  [USER-DECISION] {sym} {label} — timeout, re-ask next scan"
                    )
                    await notifier.send(
                        f"⏱ <b>{label} {sym}</b> timeout — re-ask next scan."
                    )
                continue

            # ── Generic full-close events (SL, Trail, TP3) ──
            # IMPORTANT: order matters. Close on Bybit FIRST, then record in tracker.
            # If Bybit close fails, do NOT update tracker (prevents desync where
            # bot says closed but Bybit position lingers).
            if trade.get("requires_close"):
                pct = float(trade.get("close_pct", 100.0))
                exit_px = float(trade.get("exit_price", 0))

                # Pretty label per reason
                label_map = {
                    "stop_loss":         "SL-HIT",
                    "trailing_stop":     "TRAIL-HIT",
                    "be_lock_stop":      "BE-LOCK-HIT",
                    "tp2_lock_stop":     "TP2-LOCK-HIT",
                    "take_profit":       "TP3-HIT",
                    "trailing_take_profit":   "TRAIL-TP-HIT",
                    "tp_trail_max_extension": "TRAIL-TP-MAX-R",
                    "tp_trail_stall":         "TRAIL-TP-STALL",
                    "time_exit_max":     "TIME-EXIT",
                    "time_exit_stagnant":"STAGNANT",
                }
                label = label_map.get(reason, reason.upper())
                hh = trade.get("hours_held", 0)
                roi_at = trade.get("roi_at_exit", 0)

                logger.info(
                    f"  [{label}] {sym} {reason} — sending close order to Bybit ({pct:.0f}%)"
                )

                # 1) Send the actual close order to Bybit FIRST
                live_ok = True
                actual_fill_px = 0.0
                actual_fees = None
                if hasattr(executor, "partial_close"):
                    try:
                        res = await executor.partial_close(sym, act, pct)
                        live_ok = bool(res) and not (res.get("dry_run") and not Config.DRY_RUN)
                        if res:
                            actual_fill_px = float(res.get("price", 0) or 0)
                            if res.get("fees") is not None:
                                actual_fees = float(res["fees"])
                    except Exception as exc:
                        logger.error(f"  [{label}] {sym} executor close error: {exc}")
                        live_ok = False

                # 2) ONLY update tracker if Bybit close succeeded (or in DRY_RUN)
                if not live_ok and not Config.DRY_RUN:
                    logger.warning(
                        f"  [{label}] {sym} Bybit close FAILED — keeping position in tracker. "
                        f"Will retry next scan."
                    )
                    continue   # do NOT call record_close — let next scan retry

                # 3) Record close in tracker
                # Prefer actual VWAP fill from Bybit; fall back to market data price.
                exit_px_use = actual_fill_px if actual_fill_px > 0 else exit_px
                rec = pnl.record_close(
                    sym, exit_px_use, reason=reason, actual_fees=actual_fees
                )
                if rec:
                    if hh:
                        rec["hours_held"] = hh
                        rec["roi_at_exit"] = roi_at
                    icon = {"stop_loss":"🛑", "trailing_stop":"📉", "take_profit":"🎯",
                            "trailing_take_profit":"🏁", "tp_trail_max_extension":"🎯",
                            "tp_trail_stall":"⏱",
                            "time_exit_max":"⏱", "time_exit_stagnant":"⏱"}.get(reason, "✓")
                    msg_extra = (
                        f"Held: {hh}h  ROI: {roi_at:+.1f}%\n" if hh else ""
                    )
                    await notifier.send(
                        f"{icon} <b>{label}</b> {act} {sym}\n"
                        f"Reason: <code>{reason}</code>\n"
                        f"{msg_extra}"
                        f"Position PnL: <b>{rec.get('position_pnl_usdt', rec['pnl_usdt']):+.4f}</b> "
                        f"USDT ({rec.get('position_pnl_pct', rec['pnl_pct']):+.2f}%)"
                    )
                    if memory:
                        memory.record_trade_result(
                            symbol=sym,
                            exit_price=rec.get("exit_price", exit_px_use),
                            pnl_usdt=rec.get("position_pnl_usdt", rec["pnl_usdt"]),
                            pnl_pct=rec.get("position_pnl_pct", rec.get("pnl_pct", 0)),
                            reason=reason,
                            gross_pnl_usdt=rec.get(
                                "position_gross_pnl_usdt", rec.get("gross_pnl_usdt")
                            ),
                            fees_usdt=rec.get(
                                "position_fees_usdt", rec.get("fees_usdt", 0)
                            ),
                        )
                continue

            # ── P2: Partial-close events at TP1/TP2 ──
            if trade.get("requires_partial_close"):
                pct = float(trade.get("close_pct", 30.0))
                exit_px = float(trade.get("exit_price", 0))
                logger.info(f"  [PARTIAL-TP] {sym} {reason} — closing {pct:.0f}% @ {exit_px:.4f}")
                # 1) Live close on exchange (Bybit accepts reduceOnly partial)
                live_ok = True
                actual_fill_px = 0.0
                actual_fees = None
                if hasattr(executor, "partial_close"):
                    try:
                        res = await executor.partial_close(sym, act, pct)
                        live_ok = bool(res) and not (res.get("dry_run") and not Config.DRY_RUN)
                        if res:
                            actual_fill_px = float(res.get("price", 0) or 0)
                            if res.get("fees") is not None:
                                actual_fees = float(res["fees"])
                    except Exception as exc:
                        logger.error(f"  [PARTIAL-TP] {sym} executor close error: {exc}")
                        live_ok = False
                if not live_ok and not Config.DRY_RUN:
                    pnl.queue_partial_retry(
                        sym,
                        int(trade.get("tp_index", 0)),
                        exit_px,
                        pct,
                        reason,
                    )
                    logger.warning(
                        f"  [PARTIAL-TP] {sym} Bybit partial close FAILED — retry queued. "
                        f"Will retry next scan."
                    )
                    continue
                # 2) Record partial in pnl tracker (books realized PnL for the slice)
                exit_px_use = actual_fill_px if actual_fill_px > 0 else exit_px
                rec = pnl.partial_close_record(
                    sym, exit_px_use, pct, reason=reason, actual_fees=actual_fees
                )
                if rec:
                    tp_label = "TP1" if reason.startswith("tp1") else "TP2"
                    await notifier.send(
                        f"💰 <b>{tp_label} Partial Close</b> {act} {sym}\n"
                        f"Closed: {pct:.0f}% @ <code>{exit_px:.4f}</code>\n"
                        f"PnL: <b>{rec['pnl_usdt']:+.4f}</b> USDT ({rec['pnl_pct']:+.2f}%)\n"
                        f"Remaining: {rec.get('remaining_pct_after', 0)*100:.0f}%"
                    )
                    if memory and rec.get("position_completed"):
                        memory.record_trade_result(
                            symbol=sym,
                            exit_price=rec.get("exit_price", exit_px_use),
                            pnl_usdt=rec.get("position_pnl_usdt", rec["pnl_usdt"]),
                            pnl_pct=rec.get("position_pnl_pct", rec.get("pnl_pct", 0)),
                            reason=reason,
                            gross_pnl_usdt=rec.get("position_gross_pnl_usdt"),
                            fees_usdt=rec.get("position_fees_usdt", 0),
                        )
                continue

            # ── Milestone events (BE activated, SL advanced) ──
            # These don't close the position — just send Telegram notification
            if trade.get("milestone"):
                sym = trade["symbol"]
                act = trade["action"]
                if reason == "be_activated":
                    trail = trade.get("trail_pct", 0)
                    logger.info(f"  [BE] {sym} SL→entry, trail={trail:.1f}%")
                    await notifier.send(
                        f"🔒 <b>Breakeven Activated</b>\n"
                        f"{act} {sym}\n"
                        f"SL moved to entry price\n"
                        f"Trailing: {trail:.1f}% (ATR-based)"
                    )
                elif reason == "sl_advanced":
                    new_sl = trade.get("new_sl", 0)
                    trail = trade.get("trail_pct", 0)
                    logger.info(f"  [SL+] {sym} SL→TP1 @ {new_sl:.4f}, trail={trail:.1f}%")
                    await notifier.send(
                        f"📈 <b>SL Advanced to TP1</b>\n"
                        f"{act} {sym}\n"
                        f"SL → {new_sl:.4f}\n"
                        f"Trail tightened: {trail:.1f}%"
                    )
                elif reason == "tp_trail_armed":
                    give = trade.get("giveback_pct", 0)
                    new_sl = trade.get("new_sl", 0)
                    tgt = trade.get("tp_trail_target", 0)
                    logger.info(
                        f"  [TRAIL-TP] {sym} armed at TP3 — giveback={give:.2f}%, "
                        f"floor={new_sl:.4f}, next target={tgt:.4f}"
                    )
                    await notifier.send(
                        f"🏁 <b>Trailing TP Armed</b>\n"
                        f"{act} {sym}\n"
                        f"TP3 reached — position stays open, target follows price\n"
                        f"Next target: <code>{tgt:.4f}</code>\n"
                        f"Profit floor: <code>{new_sl:.4f}</code>\n"
                        f"Giveback: {give:.2f}% from peak"
                    )
                continue

            # ── Real close events ─────────────────────────────
            partial = trade.get("partial", False)
            label = "PARTIAL" if partial else "CLOSED"
            logger.info(
                f"  [{label}] {trade['symbol']} {reason}: "
                f"PnL={trade['pnl_usdt']:+.4f} USDT"
            )
            await notifier.alert_pnl_close(trade)

            # Record trade result in memory
            if memory and (not partial or trade.get("position_completed")):
                memory.record_trade_result(
                    symbol=trade["symbol"],
                    exit_price=trade.get("exit_price", 0),
                    pnl_usdt=trade.get("position_pnl_usdt", trade["pnl_usdt"]),
                    pnl_pct=trade.get("position_pnl_pct", trade.get("pnl_pct", 0)),
                    reason=reason,
                    gross_pnl_usdt=trade.get(
                        "position_gross_pnl_usdt", trade.get("gross_pnl_usdt")
                    ),
                    fees_usdt=trade.get(
                        "position_fees_usdt", trade.get("fees_usdt", 0)
                    ),
                )

        # 2b. Telegram command processing (close %, positions, help)
        await _process_telegram_commands(notifier, executor, pnl)

        # 2c. P3: Quick-Exit on Reversal — close profitable positions if signals flip
        if Config.QUICK_EXIT_ENABLED and pnl._store["open_positions"]:
            await _check_quick_exit(pnl, market_data, executor, notifier, memory)

        # 2d. DCA check for open positions
        if Config.DCA_ENABLED and pnl._store["open_positions"]:
            await _check_dca(
                pnl, market_data, executor, notifier, memory, account_balance
            )

        # 3. Position capacity (pnl_store is source of truth after sync)
        already_open_symbols = set(pnl._store["open_positions"].keys())
        open_count = len(already_open_symbols)

        available = Config.MAX_OPEN_POSITIONS - open_count
        logger.info(
            f"Positions: {open_count}/{Config.MAX_OPEN_POSITIONS} "
            f"({available} slot free)"
            + (f"  open={list(already_open_symbols)}" if already_open_symbols else "")
        )

        # 4. AI Analysis — skip when all slots full OR circuit breaker hit
        signals: List[Dict] = []
        executed_count = 0
        adaptive_params = {"mode": "normal"}

        # Pre-check: daily loss circuit breaker. If hit, no point calling AI
        # (any signal it generates will be rejected). Save tokens + latency.
        circuit_breaker_hit = False
        if Config.DAILY_LOSS_LIMIT_USDT > 0:
            _daily_loss_now = _daily_realized_loss(pnl)
            if abs(_daily_loss_now) >= Config.DAILY_LOSS_LIMIT_USDT:
                circuit_breaker_hit = True
                logger.warning(
                    f"[CIRCUIT BREAKER] 6h rolling loss "
                    f"{_daily_loss_now:.4f} USDT >= limit "
                    f"-{Config.DAILY_LOSS_LIMIT_USDT:.2f} — "
                    f"SKIP AI scan (resets in 6h window)."
                )

        # Forward-looking regime gate. "bad" = BTC choppy + market thin vol,
        # skip seluruh AI scan (proactive, beda dari CB yang reactive).
        # "marginal" = scan tetap jalan tapi score/conf floor di-bump.
        regime_quality = _global_regime_quality(market_data)
        regime_skip_scan = (regime_quality == "bad")
        if regime_skip_scan:
            _btc = next((m for m in market_data if m.get("symbol") == "BTCUSDT"), {})
            logger.warning(
                f"[REGIME GATE] quality=bad — BTC atr={_btc.get('atr_pct', 0):.2f}%, "
                f"vol={_btc.get('volume_condition', '?')}, "
                f"struct={_btc.get('market_structure', '?')} + market thin. "
                f"SKIP AI scan (prevents whipsaw entries)."
            )
        elif regime_quality == "marginal":
            logger.info(
                f"[REGIME GATE] quality=marginal — threshold di-bump "
                f"+{Config.REGIME_MARGINAL_SCORE_BUMP:.1f} score, "
                f"+{Config.REGIME_MARGINAL_CONF_BUMP:.2f} conf untuk cycle ini."
            )

        if available <= 0:
            logger.info(
                f"All {Config.MAX_OPEN_POSITIONS} slots full — "
                "skip AI scan (monitoring SL/TP/DCA only)"
            )
        elif universe_limited:
            # Audit 2026-07-26: slot penuh saat fetch → market_data hanya berisi
            # 1-3 simbol posisi (tanpa BTC anchor, tanpa whitelist). Kalau slot
            # kosong karena SL/trail close di scan ini, AI scan dulu tetap jalan
            # di universe cacat itu — regime gate fail-open tanpa BTC dan bot
            # re-chase simbol yang baru saja ditutup. Tunggu scan berikutnya
            # yang fetch universe penuh.
            logger.info(
                "[SymbolFeed] Slot freed this scan but universe was fetched "
                "slots-full (no BTC anchor) — defer AI scan to next cycle"
            )
        elif circuit_breaker_hit:
            pass   # already logged above; just skip AI scan
        elif regime_skip_scan:
            pass   # already logged above; just skip AI scan
        else:
            # Generate learning context and adaptive params
            learning_context = ""
            if memory and Config.AI_LEARNING:
                symbols_list = [m.get("symbol", "") for m in market_data]
                learning_context = memory.generate_learning_context(symbols_list)
                adaptive_params = memory.get_adaptive_params(account_balance)

                if learning_context:
                    logger.info(f"[Memory] Learning context: {len(learning_context)} chars")
                if adaptive_params["mode"] != "normal":
                    logger.info(
                        f"[Memory] Adaptive mode: {adaptive_params['mode']} "
                        f"(risk={adaptive_params['risk_multiplier']:.1f}x, "
                        f"min_score={adaptive_params['min_score']:.1f}, "
                        f"lev={adaptive_params['max_leverage_mult']:.1f}x) "
                        f"reason={adaptive_params.get('reason', '')}"
                    )

            # Fetch on-chain regime data (cached 5 min, non-blocking on failure)
            onchain_context = ""
            oc_data = {}  # Initialize for whale impact modifier
            if onchain is not None:
                try:
                    _sm_syms = [m.get("symbol", "") for m in market_data if m.get("symbol")]
                    _ctx_limit = int(getattr(Config, "ONCHAIN_CONTEXT_MAX_SYMBOLS", 0) or 0)
                    if _ctx_limit > 0:
                        _sm_syms = _sm_syms[:_ctx_limit]
                    oc_data = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: onchain.fetch(
                            trading_mode=Config.TRADING_MODE,
                            symbols=_sm_syms,
                        )
                    )
                    from data.onchain_feed import OnChainFeed
                    onchain_context = OnChainFeed.format_for_prompt(
                        oc_data, trading_mode=Config.TRADING_MODE
                    )
                    logger.info(
                        f"[OnChain] regime={oc_data.get('regime')}  "
                        f"SmartMoney={oc_data.get('pos_whale_bias')}  "
                        f"liq={oc_data.get('liq_signal')}  "
                        f"composite={oc_data.get('composite')}  "
                        f"F&G={oc_data.get('fng')}  "
                        f"ctx_symbols={len(_sm_syms)}"
                    )
                except Exception as exc:
                    logger.warning(f"[OnChain] Skipped (error: {exc})")

            logger.info(f"Sending {len(market_data)} symbol(s) to AI ...")
            ai_result = await engine.analyze(
                market_data, account_balance,
                learning_context=learning_context,
                adaptive_params=adaptive_params,
                onchain_context=onchain_context,
            )

            if ai_result is None:
                logger.error("AI returned no result.")
            else:
                if memory and Config.AI_LEARNING:
                    reviews = ai_result.get("feedback_reviews", [])
                    if reviews:
                        saved_reviews = memory.record_feedback_reviews(reviews)
                        if saved_reviews:
                            logger.info(
                                f"[Memory] Bot LLM reviewed {saved_reviews} "
                                "MCP feedback item(s)"
                            )

                signals, best_candidate = extract_signals(
                    ai_result, [m.get("symbol", "") for m in market_data]
                )
                logger.info(f"Signals: {len(signals)}  Best: {best_candidate}")

                # Sort: best_candidate first, then by score descending
                def _signal_priority(s):
                    sym = s.get("symbol", "")
                    is_best = 1 if sym == best_candidate else 0
                    score = float(s.get("score", 0))
                    return (-is_best, -score)
                signals.sort(key=_signal_priority)

                # ── Whale Impact Score Modifier ─────────────────────
                # Boost/penalize signal score based on whale activity
                smart_money_flows = oc_data.get("smart_money_flows", {}) if oc_data else {}
                whale_by_symbol = smart_money_flows.get("by_symbol", {})

                for signal in signals:
                    signal["_ai_score_raw"] = float(signal.get("score", 0))
                    action = signal.get("action", "HOLD")
                    sym = signal.get("symbol", "?")
                    if action in ("LONG", "SHORT") and sym in whale_by_symbol:
                        whale_data = whale_by_symbol[sym]
                        whale_direction = whale_data.get("whale_direction", "neutral")
                        whale_impact = whale_data.get("whale_impact", 0.0)
                        current_score = float(signal.get("score", 0))

                        modifier = 0.0
                        if action == "LONG":
                            if whale_direction == "strongly_bullish":
                                modifier = 1.5
                            elif whale_direction == "bullish":
                                modifier = 1.0
                            elif whale_direction == "strongly_bearish":
                                modifier = -1.0
                            elif whale_direction == "bearish":
                                modifier = -0.5
                        elif action == "SHORT":
                            if whale_direction == "strongly_bearish":
                                modifier = 1.5
                            elif whale_direction == "bearish":
                                modifier = 1.0
                            elif whale_direction == "strongly_bullish":
                                modifier = -1.0
                            elif whale_direction == "bullish":
                                modifier = -0.5

                        if modifier != 0.0:
                            new_score = round(current_score + modifier, 2)
                            signal["score"] = new_score
                            logger.info(
                                f"  [WHALE] {sym} {action}: "
                                f"whale={whale_direction} impact={whale_impact:.1f} "
                                f"score {current_score:.1f} → {new_score:.1f} "
                                f"({modifier:+.1f})"
                            )

                # Ranking must use the final enriched score, not the stale AI-only
                # order computed before whale context was applied.
                signals.sort(key=_signal_priority)

                for signal in signals:
                    action = signal.get("action", "HOLD")
                    sym    = signal.get("symbol", "?")

                    if action == "HOLD":
                        _tier = signal.get('_ai_tier', '?')
                        logger.info(
                            f"  HOLD  {sym:<14s} "
                            f"score={float(signal.get('score',0)):.1f}  "
                            f"conf={float(signal.get('confidence',0))*100:.0f}%  "
                            f"[{_tier}]  {signal.get('reason','')}"
                        )
                        continue

                    # Skip symbols already in an open position
                    if sym in already_open_symbols:
                        logger.info(f"  Already open -- skip {sym}")
                        continue

                    # ── HARD BLACKLIST ───────────────────────────
                    # Reject simbol di SYMBOL_BLACKLIST (.env) — historical loser
                    # yang tidak boleh masuk lagi meski lolos quality gate.
                    _sym_norm = sym.replace("/", "").replace(":USDT", "").upper()
                    if _sym_norm in Config.SYMBOL_BLACKLIST:
                        logger.warning(
                            f"  [BLACKLIST] {sym} di SYMBOL_BLACKLIST -- skip"
                        )
                        continue

                    # ── DATA-ONLY ────────────────────────────────
                    # Simbol ini di-fetch hanya untuk konteks AI (mis. BTC
                    # global bias di prompt). Bot dilarang masuk posisi.
                    if _sym_norm in Config.DATA_ONLY_SYMBOLS:
                        logger.info(
                            f"  [DATA-ONLY] {sym} fetched untuk konteks AI -- no trade"
                        )
                        continue

                    # ── 4 Risk Detection Filters (HARD reject) ───
                    # Bot menolak signal walaupun AI mengeluarkan LONG/SHORT
                    # — sebagai backstop kalau AI mengabaikan prompt.
                    # Auto-block symbols that memory shows are persistently weak.
                    # This prevents repeated re-entry into symbols that have
                    # already produced negative expectancy.
                    # NOTE: gate di belakang AI_LEARNING — memori historis masih
                    # terkalibrasi ke rezim sizing pra-Juli (threshold absolut
                    # -50 USDT ≈ 1.4 loss ukuran Juni vs ~11 loss ukuran Juli),
                    # dan replay menunjukkan blacklist lama justru memblok 32
                    # posisi Juli ber-PnL net +$23.5. Aktifkan lagi hanya
                    # setelah memory di-rebuild dari lifecycle rezim baru.
                    if memory and Config.AI_LEARNING:
                        _sym_stats = memory.get_symbol_stats(sym)
                        if _sym_stats and _sym_stats.get("trades", 0) >= 3:
                            _trades = int(_sym_stats.get("trades", 0))
                            _wins = int(_sym_stats.get("wins", 0))
                            _wr = (_wins / _trades * 100) if _trades else 0.0
                            _pnl = float(_sym_stats.get("total_pnl", 0.0))
                            if _pnl < -50 or (_pnl < 0 and _wr < 40):
                                logger.warning(
                                    f"  [MEM-BLACKLIST] {sym} WR={_wr:.0f}% "
                                    f"PnL={_pnl:+.2f} over {_trades} trades -- skip"
                                )
                                continue

                    _mkt_for_filter = next(
                        (m for m in market_data if m.get("symbol") == sym), {}
                    )
                    if not _mkt_for_filter.get("ticker_available", False):
                        logger.warning(
                            f"  [DATA-QUALITY] {sym} executable ticker/spread unavailable -- skip"
                        )
                        continue
                    _decision_price = float(_mkt_for_filter.get("current_price", 0.0))
                    if _decision_price <= 0:
                        logger.warning(f"  [DATA-QUALITY] {sym} invalid decision price -- skip")
                        continue
                    signal["_ai_entry_price"] = float(signal.get("entry_price", 0.0) or 0.0)
                    signal["entry_price"] = _decision_price
                    _spread = float(_mkt_for_filter["spread_pct"])
                    _vol_usd = float(_mkt_for_filter.get("volume_24h_usd", 0.0))
                    _upper_wick = float(_mkt_for_filter.get("upper_wick_atr", 0.0))
                    _lower_wick = float(_mkt_for_filter.get("lower_wick_atr", 0.0))
                    _funding_anom = bool(_mkt_for_filter.get("funding_anomaly", False))
                    _funding_z = float(_mkt_for_filter.get("funding_zscore", 0.0))

                    # Filter 1: Spread terlalu lebar
                    if _spread > Config.MAX_SPREAD_PCT:
                        logger.warning(
                            f"  [RISK-SPREAD] {sym} spread={_spread:.3f}% > "
                            f"max {Config.MAX_SPREAD_PCT:.3f}% -- skip"
                        )
                        continue

                    # Filter 2: Wick anomaly (stop-hunt risk) on direction-relevant side
                    # LONG entry → upper wick danger (fake breakout)
                    # SHORT entry → lower wick danger (fake breakdown)
                    _wick_threshold = Config.MAX_WICK_ATR_RATIO
                    if action == "LONG" and _upper_wick >= _wick_threshold:
                        logger.warning(
                            f"  [RISK-WICK] {sym} LONG: upper_wick={_upper_wick:.1f}×ATR "
                            f">= {_wick_threshold} (fake breakout risk) -- skip"
                        )
                        continue
                    if action == "SHORT" and _lower_wick >= _wick_threshold:
                        logger.warning(
                            f"  [RISK-WICK] {sym} SHORT: lower_wick={_lower_wick:.1f}×ATR "
                            f">= {_wick_threshold} (fake breakdown risk) -- skip"
                        )
                        continue

                    # Filter 3: Funding anomaly (forced-liq cascade)
                    if _funding_anom:
                        logger.warning(
                            f"  [RISK-FUNDING] {sym} funding_zscore={_funding_z:.1f} "
                            f"|z|>=3 sigma (cascade risk) -- skip"
                        )
                        continue

                    # Filter 4: Wash trading (Volume vs OI divergence)
                    _wash_susp = bool(_mkt_for_filter.get("wash_suspicion", False))
                    _wash_sev  = _mkt_for_filter.get("wash_severity", "low")
                    _oi_raw    = _mkt_for_filter.get("oi_change_pct_24h")
                    _oi_chg    = float(_oi_raw) if _oi_raw is not None else None
                    if _wash_susp and _wash_sev == "high":
                        logger.warning(
                            f"  [RISK-WASH] {sym} HIGH wash suspicion "
                            f"(vol=${_vol_usd/1e6:.0f}M, OI change={_oi_chg:.1f}%) -- skip"
                        )
                        continue
                    # Medium wash: require higher AI score (inline lookup —
                    # sig_score is computed lower; compute here to gate early)
                    # Gate deterministik WAJIB pakai skor AI mentah — whale
                    # boost (+1.5) hanya untuk ranking, bukan meloloskan gate.
                    _early_score = float(
                        signal.get("_ai_score_raw", signal.get("score", 0)) or 0
                    )
                    if _wash_susp and _wash_sev == "medium" and _early_score < 9.0:
                        logger.warning(
                            f"  [RISK-WASH] {sym} medium wash, score {_early_score:.1f}<9.0 "
                            f"(vol=${_vol_usd/1e6:.0f}M, OI={_oi_chg:.1f}%) -- skip"
                        )
                        continue

                    # Filter 5: Thin liquidity warning (not hard-reject, just log)
                    if 0 < _vol_usd < 10_000_000:
                        logger.info(
                            f"  [RISK-VOL] {sym} 24h volume ${_vol_usd/1e6:.1f}M "
                            f"< $10M (thin liquidity, AI score should be high)"
                        )

                    # ── Daily circuit breaker ────────────────────
                    # SMC/retest + anti-late-entry gate. Mode is configurable:
                    # soft by default, strict for the old hard-gate behavior.
                    if not _passes_vol_regime_filter(signal, _mkt_for_filter):
                        continue
                    if not _passes_smc_retest_filter(signal, _mkt_for_filter):
                        continue

                    # Halt new entries if 6h rolling loss > limit
                    if Config.DAILY_LOSS_LIMIT_USDT > 0:
                        _daily_loss = _daily_realized_loss(pnl)
                        if abs(_daily_loss) >= Config.DAILY_LOSS_LIMIT_USDT:
                            logger.warning(
                                f"  [CIRCUIT BREAKER] 6h rolling loss limit hit: "
                                f"{_daily_loss:.4f} USDT (limit: -{Config.DAILY_LOSS_LIMIT_USDT:.2f}) "
                                f"-- no new entries"
                            )
                            break  # stop processing all signals for this scan

                    if executed_count >= available:
                        logger.info(f"  Slot limit -- skip {sym}")
                        continue

                    # ── Quality filters: score + confidence (hard floor + adaptive) ──
                    # Hard floor (Config.MIN_SCORE_HARD, MIN_CONFIDENCE_HARD) protects
                    # against bad signals even when adaptive mode is "normal".
                    # Both score AND confidence must pass.
                    adaptive_min_score = adaptive_params.get("min_score", 5.5)
                    min_score = max(adaptive_min_score, Config.MIN_SCORE_HARD)
                    min_conf = Config.MIN_CONFIDENCE_HARD

                    # Lift floor saat regime=marginal (defense in depth)
                    if regime_quality == "marginal":
                        min_score = min_score + Config.REGIME_MARGINAL_SCORE_BUMP
                        min_conf = min_conf + Config.REGIME_MARGINAL_CONF_BUMP

                    # On-chain context may rank/downsize signals, but must never
                    # lift a weak AI signal through the deterministic hard floor.
                    sig_score = float(signal.get("_ai_score_raw", signal.get("score", 0)))
                    sig_conf = float(signal.get("confidence", 0))

                    if sig_score < min_score:
                        logger.info(
                            f"  Score {sig_score:.1f} < min {min_score:.1f} "
                            f"(hard={Config.MIN_SCORE_HARD:.1f}, "
                            f"adaptive={adaptive_params['mode']}) -- skip {sym}"
                        )
                        continue

                    if sig_conf < min_conf:
                        logger.info(
                            f"  Confidence {sig_conf:.2f} < min {min_conf:.2f} "
                            f"-- skip {sym}"
                        )
                        continue

                    decision = apply_expectancy_filter_and_sizing(
                        signal, _mkt_for_filter, account_balance
                    )
                    write_decision_audit(decision, signal)
                    if not decision.allowed:
                        continue

                    # Validate TP direction vs ACTUAL current market price
                    # (not AI's stated entry_price — AI can hallucinate a wrong entry
                    #  that makes its TPs look valid, but they're still below market)
                    _mkt = _mkt_for_filter
                    _cur = float(_mkt.get("current_price", 0))
                    _tps = signal.get("take_profit", [])
                    if _tps:
                        _ai_entry = float(signal.get("entry_price", 0))
                        # Use the higher reference price for LONG (conservative),
                        # lower for SHORT — catches both wrong AI entry AND moved market.
                        if action in ("LONG", "BUY"):
                            _ref = max(_cur, _ai_entry) if _ai_entry > 0 else _cur
                            bad  = [t for t in _tps if float(t) <= _ref]
                        else:
                            _ref = min(_cur, _ai_entry) if _ai_entry > 0 else _cur
                            bad  = [t for t in _tps if float(t) >= _ref]
                        if bad and _ref > 0:
                            logger.warning(
                                f"  [REJECT] {sym} {action} invalid TPs {bad} "
                                f"vs market={_cur:.6f} ai_entry={_ai_entry:.6f} — skipping"
                            )
                            continue

                    # ── Risk/Reward filter (uses AI's invalidation SL) ──
                    # Use weighted TP reward instead of TP1-only reward because
                    # TP1 is only a partial close + BE/trail activation. TP3
                    # closes the remainder, so RR should reflect the planned
                    # exit distribution.
                    _rr_tps = signal.get("take_profit", [])
                    _rr_sl  = float(signal.get("stop_loss", 0))
                    _rr_entry = float(signal.get("entry_price", _cur)) or _cur
                    if _rr_tps and _rr_sl > 0 and _rr_entry > 0:
                        _tp1_w = max(0.0, min(float(Config.TP1_CLOSE_PCT) / 100.0, 1.0))
                        _tp2_remaining = max(
                            0.0, min(float(Config.TP2_CLOSE_PCT) / 100.0, 1.0)
                        )
                        _tp2_w = (1.0 - _tp1_w) * _tp2_remaining
                        _tp_weights = [_tp1_w, _tp2_w, max(0.0, 1.0 - _tp1_w - _tp2_w)]
                        _weighted_reward = 0.0
                        if action in ("LONG", "BUY"):
                            _risk   = _rr_entry - _rr_sl
                            for _tp, _w in zip(_rr_tps[:3], _tp_weights):
                                _weighted_reward += max(0.0, float(_tp) - _rr_entry) * _w
                        else:
                            _risk   = _rr_sl - _rr_entry
                            for _tp, _w in zip(_rr_tps[:3], _tp_weights):
                                _weighted_reward += max(0.0, _rr_entry - float(_tp)) * _w
                        if _risk > 0:
                            _rr = _weighted_reward / _risk
                            if _rr < Config.MIN_RR:
                                logger.warning(
                                    f"  [REJECT] {sym} {action} weighted RR={_rr:.2f} < "
                                    f"min={Config.MIN_RR} "
                                    f"(weighted_reward={_weighted_reward:.4f} "
                                    f"risk={_risk:.4f} weights={_tp_weights}) — skipping"
                                )
                                continue

                    # ── Anti-overtrading: daily symbol limit ─────
                    _daily_count = _symbol_trade_count_today(pnl, sym)
                    if _daily_count >= Config.MAX_TRADES_PER_SYMBOL_DAY:
                        logger.info(
                            f"  [SKIP] {sym} daily limit "
                            f"({_daily_count}/{Config.MAX_TRADES_PER_SYMBOL_DAY} trades today)"
                        )
                        continue

                    # ── Anti-overtrading: loss cooldown ──────────
                    _loss_h = _symbol_hours_since_last_loss(pnl, sym)
                    if _loss_h < Config.SYMBOL_LOSS_COOLDOWN_H:
                        logger.info(
                            f"  [SKIP] {sym} loss cooldown "
                            f"({_loss_h:.1f}h < {Config.SYMBOL_LOSS_COOLDOWN_H}h required)"
                        )
                        continue

                    # Strong post-loss effect in the audited sample: after two
                    # consecutive losses on the same symbol+side, the next
                    # entry had materially negative expectancy.  Require a
                    # longer reset window before trying that direction again.
                    _loss_streak, _streak_h = _symbol_side_loss_streak(
                        pnl, sym, action
                    )
                    if (
                        _loss_streak >= Config.REPEATED_LOSS_STREAK
                        and _streak_h < Config.REPEATED_LOSS_COOLDOWN_H
                    ):
                        logger.info(
                            f"  [SKIP] {sym} {action} repeated-loss cooldown "
                            f"(streak={_loss_streak}, {_streak_h:.1f}h < "
                            f"{Config.REPEATED_LOSS_COOLDOWN_H:.1f}h)"
                        )
                        continue

                    # Clamp position size to configured limits
                    raw_size = float(signal.get("position_size_percent", 2.0))
                    clamped_size = max(
                        Config.MIN_POSITION_SIZE_PCT,
                        min(raw_size, Config.MAX_POSITION_SIZE_PCT),
                    )
                    signal["position_size_percent"] = clamped_size
                    # Warn if AI requested >2x cap — indicates AI is unreliable on
                    # position sizing (may need stricter prompt or model change)
                    if raw_size > Config.MAX_POSITION_SIZE_PCT * 2:
                        logger.warning(
                            f"  [SIZE-WARN] {sym} AI requested {raw_size:.1f}% "
                            f"(cap {Config.MAX_POSITION_SIZE_PCT}%, clamped). "
                            f"AI sizing logic unreliable — review prompt"
                        )

                    # Phase 3: adaptive rules (SL width, leverage cap, symbol penalty)
                    mkt_snap = next(
                        (m for m in market_data if m.get("symbol") == sym), {}
                    )
                    signal = _apply_adaptive_rules(
                        signal, mkt_snap, adaptive_params, memory
                    )

                    signal["_atr_pct"] = mkt_snap.get("atr_pct", 0.0)
                    signal["_mode"] = mkt_snap.get(
                        "trading_mode", Config.TRADING_MODE
                    )
                    signal = materialize_effective_stop(signal, mkt_snap)

                    # Re-check RR after adaptive rules because they can widen
                    # the stop. Audit 2026-07-26: gate sekarang cost-adjusted
                    # (round-trip ~0.15% masuk ke reward DAN risk) plus floor
                    # kedua yang menilai runner di TP2 — RR gross lama optimis
                    # dua arah dan meloloskan setup ber-payoff 0.5R.
                    _adm = calculate_admission_rr(signal)
                    if _adm is None:
                        logger.warning(
                            f"  [REJECT] {sym} {action} invalid effective SL/TP plan"
                        )
                        continue
                    if _adm["rr_net"] < Config.MIN_RR:
                        logger.warning(
                            f"  [REJECT] {sym} {action} net weighted RR="
                            f"{_adm['rr_net']:.2f} < min={Config.MIN_RR} "
                            f"(gross={_adm['rr_gross']:.2f}, risk={_adm['risk']:.4f}, "
                            f"cost={_adm['cost']:.4f})"
                        )
                        continue
                    if Config.RR_TP2_FLOOR > 0 and _adm["rr_tp2_net"] < Config.RR_TP2_FLOOR:
                        logger.warning(
                            f"  [REJECT] {sym} {action} truncated RR(TP2)="
                            f"{_adm['rr_tp2_net']:.2f} < floor={Config.RR_TP2_FLOOR} "
                            f"(runner valued at TP2 — TP3 rarely reached)"
                        )
                        continue

                    decision = apply_max_loss_cap(signal, account_balance, mkt_snap)
                    write_decision_audit(decision, signal)
                    if not decision.allowed:
                        continue

                    logger.info(
                        f"  {action:5s} {sym:<14s} "
                        f"entry={float(signal.get('entry_price',0)):,.4f}  "
                        f"sl={float(signal.get('stop_loss',0)):,.4f}  "
                        f"lev={signal.get('leverage',0)}x  "
                        f"size={float(signal.get('position_size_percent', clamped_size)):.1f}%"
                    )

                    exec_result = await executor.execute_signal(signal)
                    if exec_result:
                        executed_count += 1
                        pnl.record_entry(signal, exec_result)
                        already_open_symbols.add(sym)
                        logger.info(f"  Executed: {exec_result}")
                        await notifier.alert_execution(exec_result)
                        if exec_result.get("requires_manual_intervention"):
                            await notifier.send(
                                f"🚨 <b>CRITICAL UNPROTECTED POSITION</b>\n"
                                f"{action} {sym}: exchange SL and emergency rollback failed. "
                                "Check the exchange immediately."
                            )

                        # Record entry context in memory
                        if memory:
                            memory.record_entry_context(sym, action, signal, mkt_snap)
                    else:
                        logger.warning(f"  Execution failed: {sym}")

        # 5. Build PnL stats and print dashboard
        stats = pnl.get_stats()
        stats["unrealized_pnl"] = pnl.unrealized_pnl(market_data)
        open_pos_summary = pnl.open_positions_summary(market_data)
        recent = pnl.get_recent_trades(5)

        print_dashboard(
            scan_count, signals, executed_count,
            account_balance, Config.EXCHANGE_MODE,
            Config.DRY_RUN, Config.SCAN_INTERVAL,
            data_sources, stats, open_pos_summary, recent,
            adaptive_mode=adaptive_params.get("mode", "normal"),
        )

        logger.info(
            f"Scan #{scan_count:04d} done -- "
            f"executed={executed_count}  "
            f"realized={stats['realized_pnl']:+.4f} USDT  "
            f"unrealized={stats['unrealized_pnl']:+.4f} USDT  "
            f"sleep={Config.SCAN_INTERVAL}s"
        )
        await asyncio.sleep(Config.SCAN_INTERVAL)


# ── Entry point ────────────────────────────────────────────────────────────

async def main() -> None:
    from config import Config
    from analyzer.signal_engine import SignalEngine
    from data.market_feed import MarketFeed
    from utils.logger import logger
    from utils.telegram_notifier import TelegramNotifier
    from utils.pnl_tracker import PnLTracker
    from utils.trade_memory import TradeMemory

    print(_clr("""
  +----------------------------------------------------------+
  |         AI QUANT TRADING BOT  v2.0                       |
  |         Analyzer + Execution + PnL + AI Memory           |
  +----------------------------------------------------------+
    """, "cyan"))

    try:
        Config.validate()
    except ValueError as exc:
        print(f"[CONFIG ERROR]\n{exc}")
        sys.exit(1)

    logger.info("Configuration loaded:")
    logger.info(Config.summary())

    if Config.EXCHANGE_MODE == "CEX":
        # CEX_PROVIDER memilih bursa; keduanya tetap "CEX" supaya gate ROI-SL
        # di risk/decision_engine.py dan sizing RR berlaku sama.
        if Config.CEX_PROVIDER == "okx":
            from execution.okx_executor import OKXExecutor
            executor = OKXExecutor()
            logger.info(f"Executor : OKX USDT-SWAP (demo={Config.OKX_DEMO})")
        else:
            from execution.cex_executor import CEXExecutor
            executor = CEXExecutor()
            logger.info(f"Executor : Bybit Futures (testnet={Config.BYBIT_TESTNET})")
    elif Config.EXCHANGE_MODE == "DEX":
        from execution.dex_executor import DEXExecutor
        executor = DEXExecutor()
        logger.info(f"Executor : Hyperliquid (testnet={Config.HL_TESTNET})")
    else:
        print(f"Unknown EXCHANGE_MODE '{Config.EXCHANGE_MODE}'")
        sys.exit(1)

    feed = MarketFeed()

    # On-chain regime feed (Binance public endpoints + Fear & Greed)
    from data.onchain_feed import OnChainFeed
    okx_web3 = None
    if Config.OKX_WEB3_ENABLED:
        from data.okx_web3_feed import OkxWeb3Feed
        okx_web3 = OkxWeb3Feed.from_config(Config)
        logger.info(
            f"OKX Web3 enrichment enabled "
            f"(mapped_symbols={len(okx_web3.mapped_symbols())}, auth={okx_web3.has_auth})"
        )
    onchain = OnChainFeed(binance_api_key=Config.BINANCE_API_KEY, okx_web3_feed=okx_web3)

    # Unified AI provider initialization (with optional tier-2/3 fallback chain)
    ai_cfg = Config.get_ai_config()
    fallback_chain = Config.get_tier_chain()  # may be empty; supports N tiers

    engine = SignalEngine(
        api_key=ai_cfg["api_key"],
        model=ai_cfg["model"],
        provider=ai_cfg["provider"],
        base_url=ai_cfg["base_url"],
        fallback_chain=fallback_chain,
    )

    notifier = TelegramNotifier(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)
    # Flush stale Telegram commands accumulated while bot was offline.
    # Without this, restart replays old "close X" commands from up to 24h ago.
    if notifier.enabled:
        await asyncio.to_thread(notifier._sync_flush_old)
        logger.info(
            f"[Telegram] Flushed old updates "
            f"(offset reset to {notifier._last_update_id})"
        )

    pnl      = PnLTracker(Config.ACCOUNT_BALANCE)

    # Trade Memory for AI Learning.
    # Audit 2026-07-26: memory SELALU diinstansiasi supaya perekaman hasil
    # trade (record_entry_context/record_trade_result) jalan terus — tanpa itu
    # memori lifecycle baru tidak akan pernah terkumpul untuk rebuild.
    # AI_LEARNING=false sekarang hanya mematikan KONSUMSI memori (prompt
    # learning-context, adaptive params, MEM-BLACKLIST), bukan pencatatannya.
    memory = TradeMemory()
    if Config.AI_LEARNING:
        logger.info(
            f"AI Learning  : ON ({memory.get_total_trades()} trades in memory, "
            f"streak={memory.get_streak():+d})"
        )
    else:
        logger.info(
            "AI Learning  : OFF (recording still active — "
            f"{memory.get_total_trades()} trades in memory)"
        )

    mode_label = f"{Config.EXCHANGE_MODE} {'[DRY RUN]' if Config.DRY_RUN else '[LIVE]'}"
    await notifier.send(
        f"<b>Bot Started</b> | {mode_label}\n"
        f"Symbols: {len(Config.SYMBOLS)}  Balance: {Config.ACCOUNT_BALANCE:,.0f} USDT\n"
        f"AI: {ai_cfg['provider']}/{ai_cfg['model']}\n"
        f"Learning: {'ON' if Config.AI_LEARNING else 'OFF (recording only)'}"
    )

    # Startup sync — reconcile pnl_store with actual Bybit positions
    if not Config.DRY_RUN:
        logger.info("[Sync] Running startup position sync ...")
        await _sync_live_positions(executor, pnl, memory, notifier)
        open_syms = list(pnl._store["open_positions"].keys())
        logger.info(f"[Sync] Startup done — {len(open_syms)} open: {open_syms}")

    # Initial auto-symbol load (before first scan)
    if Config.AUTO_SYMBOLS:
        logger.info(
            f"[SymbolFeed] AUTO_SYMBOLS=true — "
            f"fetching top-{Config.AUTO_SYMBOLS_COUNT} by volume ..."
        )
        from data.market_feed import fetch_top_symbols
        initial_syms = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: fetch_top_symbols(
                limit=Config.AUTO_SYMBOLS_COUNT,
                min_turnover_usdt=Config.AUTO_SYMBOLS_MIN_TURNOVER_USD,
                blacklist=Config.SYMBOL_BLACKLIST,
                data_only=Config.DATA_ONLY_SYMBOLS,
            )
        )
        if initial_syms:
            Config.SYMBOLS = initial_syms
            logger.info(
                f"[SymbolFeed] Loaded {len(Config.SYMBOLS)} symbols: "
                f"{Config.SYMBOLS[:10]}{'...' if len(Config.SYMBOLS) > 10 else ''}"
            )
        else:
            logger.warning("[SymbolFeed] Initial fetch failed — using SYMBOLS from .env")

    # Audit 2026-07-26: satu exception tak tertangkap dulu MEMATIKAN bot —
    # padahal trailing/BE stop bersifat local-only (Bybit tidak tahu), jadi
    # proses mati = posisi live kehilangan seluruh enforcement trail. Sekarang
    # crash di scan loop di-log + notif Telegram, lalu agent di-restart dengan
    # backoff. State persisten (SQLite store) aman untuk resume.
    _crash_streak = 0
    try:
        while True:
            _run_started = asyncio.get_event_loop().time()
            try:
                await run_agent(feed, engine, executor, notifier, pnl, memory, onchain)
                break  # run_agent returned cleanly (never expected, but honor it)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as exc:
                # Streak hanya dihitung untuk crash beruntun; sesi sehat >1 jam
                # me-reset counter supaya bot long-running tidak mati karena
                # akumulasi crash sporadis.
                if asyncio.get_event_loop().time() - _run_started > 3600:
                    _crash_streak = 0
                _crash_streak += 1
                logger.exception(f"Scan loop crashed (#{_crash_streak}): {exc}")
                try:
                    await notifier.alert_error(
                        f"Scan loop crashed (#{_crash_streak}): {exc} — restarting"
                    )
                except Exception:
                    pass
                if _crash_streak >= 10:
                    logger.error("10 consecutive crashes — giving up.")
                    raise
                await asyncio.sleep(min(30 * _crash_streak, 300))
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except asyncio.CancelledError:
        logger.info("Task cancelled.")
    except Exception as exc:
        logger.exception(f"Fatal: {exc}")
        await notifier.alert_error(str(exc))
    finally:
        logger.info("Shutting down ...")
        for obj in (feed, executor):
            try:
                await obj.close()
            except Exception:
                pass
        await asyncio.sleep(0.3)
        logger.info("Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
