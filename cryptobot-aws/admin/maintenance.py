"""
admin/maintenance.py — pnl_store.json maintenance operations.

Consolidates the old one-off scripts into reusable, parameterised functions:

  recount-stats   (was recount_stats.py)
      Rebuild wins/losses/total_trades/total_realized/best/worst from
      closed_trades.

  rollback-close  (was rollback_false_close.py)
      Remove the most recent false close per symbol (default: time_exit_*)
      and reverse its stats impact, so the next bot startup re-syncs the
      position from the exchange.

  fix-pnl         (was fix_dash_pnl.py + fix_siren.py, generalised)
      Correct a recorded trade's PnL, or delete a mis-recorded trade,
      selected by symbol (+ optional close_time / reason), then rebuild
      stats so aggregates stay consistent.

  inject-sltp     (was inject_sl_tp.py)
      Attach ATR-based SL/TP to open positions missing protective levels,
      using live Bybit 1H candles.

Every mutating function backs up the store first and honours dry_run.
Functions return a summary dict for the CLI to print.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from admin.store import backup_store, load_store, save_store


# ── Shared stats rebuild ────────────────────────────────────────────────────

def _rebuild_stats(store: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute aggregate stats from closed_trades. Returns the computed dict.

    total_realized includes partial closes (partial_close_record books PnL
    immediately); win/loss/trade counts use full closes only. Mirrors the
    original recount_stats.py logic exactly.
    """
    closed = store.get("closed_trades", [])
    full_closes = [t for t in closed if not t.get("partial")]

    total = wins = losses = 0
    total_realized = round(
        sum(float(t.get("pnl_usdt", 0) or 0) for t in closed), 4
    )
    best_trade = worst_trade = 0.0
    best_symbol = worst_symbol = ""
    by_symbol: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    )

    for t in full_closes:
        pnl = float(t.get("pnl_usdt", 0))
        sym = t.get("symbol", "?")
        total += 1
        if pnl >= 0:
            wins += 1
            by_symbol[sym]["wins"] += 1
            if pnl > best_trade:
                best_trade, best_symbol = pnl, sym
        else:
            losses += 1
            by_symbol[sym]["losses"] += 1
            if pnl < worst_trade:
                worst_trade, worst_symbol = pnl, sym
        by_symbol[sym]["trades"] += 1
        by_symbol[sym]["pnl"] = round(by_symbol[sym]["pnl"] + pnl, 4)

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "total_realized": total_realized,
        "best_trade": best_trade,
        "best_symbol": best_symbol,
        "worst_trade": worst_trade,
        "worst_symbol": worst_symbol,
        "by_symbol": dict(by_symbol),
    }


def _apply_stats(store: Dict[str, Any], computed: Dict[str, Any]) -> None:
    """Write the recomputed scalar stats back into the store (skip by_symbol)."""
    store.setdefault("stats", {})
    for key in (
        "total_trades", "wins", "losses", "total_realized",
        "best_trade", "best_symbol", "worst_trade", "worst_symbol",
    ):
        store["stats"][key] = computed[key]


# ── recount-stats ───────────────────────────────────────────────────────────

def recount_stats(dry_run: bool = False) -> Dict[str, Any]:
    """Rebuild aggregate stats from closed_trades."""
    store = load_store()
    closed = store.get("closed_trades", [])
    if not closed:
        return {"changed": False, "message": "No closed_trades — nothing to recount."}

    old = dict(store.get("stats", {}))
    computed = _rebuild_stats(store)

    result = {
        "changed": True,
        "dry_run": dry_run,
        "before": {k: old.get(k) for k in ("total_trades", "wins", "losses", "total_realized")},
        "after": {k: computed[k] for k in ("total_trades", "wins", "losses", "total_realized")},
        "best": (computed["best_trade"], computed["best_symbol"]),
        "worst": (computed["worst_trade"], computed["worst_symbol"]),
        "by_symbol": computed["by_symbol"],
    }

    if dry_run:
        result["backup"] = None
        return result

    result["backup"] = str(backup_store())
    _apply_stats(store, computed)
    save_store(store)
    return result


# ── rollback-close ──────────────────────────────────────────────────────────

def rollback_close(
    symbols: List[str],
    reason_prefix: str = "time_exit",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove the most recent matching false close per symbol; rebuild stats.

    Args:
        symbols:        symbols whose latest matching close should be removed.
        reason_prefix:  only remove closes whose `reason` starts with this
                        (default "time_exit"). Use "" to match any reason.
    """
    store = load_store()
    closed = store.get("closed_trades", [])
    targets = {s.upper() for s in symbols}

    to_remove: List[int] = []
    seen: set = set()
    for i in range(len(closed) - 1, -1, -1):
        t = closed[i]
        sym = str(t.get("symbol", "")).upper()
        if sym not in targets or sym in seen:
            continue
        if reason_prefix and not str(t.get("reason", "")).startswith(reason_prefix):
            continue
        to_remove.append(i)
        seen.add(sym)
        if len(seen) == len(targets):
            break

    if not to_remove:
        return {"changed": False, "message": "No matching closes found (already rolled back?)."}

    removed = [
        {
            "symbol": closed[i].get("symbol"),
            "pnl_usdt": float(closed[i].get("pnl_usdt", 0)),
            "reason": closed[i].get("reason"),
            "close_time": closed[i].get("close_time"),
        }
        for i in to_remove
    ]

    if dry_run:
        return {"changed": True, "dry_run": True, "removed": removed, "backup": None}

    backup = str(backup_store())
    for idx in sorted(to_remove, reverse=True):
        closed.pop(idx)
    store["closed_trades"] = closed
    computed = _rebuild_stats(store)
    _apply_stats(store, computed)
    save_store(store)

    return {
        "changed": True,
        "dry_run": False,
        "removed": removed,
        "backup": backup,
        "after": {k: computed[k] for k in ("total_trades", "wins", "losses", "total_realized")},
    }


# ── fix-pnl (generalised from fix_dash_pnl.py + fix_siren.py) ────────────────

def fix_pnl(
    symbol: str,
    real_pnl: Optional[float] = None,
    close_time: Optional[str] = None,
    reason_prefix: Optional[str] = None,
    delete: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Correct or delete a recorded closed trade, then rebuild stats.

    Selection: most recent closed trade matching `symbol` and, if given,
    `close_time` (exact) and/or `reason_prefix`.

    Actions (mutually exclusive):
        delete=True         -> remove the trade entirely (was fix_siren.py).
        real_pnl=<float>    -> overwrite pnl_usdt (+ recompute pnl_pct from
                               notional/leverage), was fix_dash_pnl.py.

    Stats are rebuilt from scratch afterwards so aggregates stay consistent.
    """
    if not delete and real_pnl is None:
        raise ValueError("fix-pnl requires either --real-pnl <value> or --delete")
    if delete and real_pnl is not None:
        raise ValueError("fix-pnl: choose either --real-pnl or --delete, not both")

    store = load_store()
    closed = store.get("closed_trades", [])
    sym = symbol.upper()

    target_idx = None
    for i in range(len(closed) - 1, -1, -1):
        t = closed[i]
        if str(t.get("symbol", "")).upper() != sym:
            continue
        if close_time and t.get("close_time") != close_time:
            continue
        if reason_prefix and not str(t.get("reason", "")).startswith(reason_prefix):
            continue
        target_idx = i
        break

    if target_idx is None:
        return {"changed": False, "message": f"No matching {sym} trade found."}

    trade = closed[target_idx]
    before = {
        "symbol": trade.get("symbol"),
        "close_time": trade.get("close_time"),
        "reason": trade.get("reason"),
        "pnl_usdt": float(trade.get("pnl_usdt", 0)),
    }

    if dry_run:
        return {
            "changed": True, "dry_run": True, "action": "delete" if delete else "correct",
            "trade": before, "new_pnl": real_pnl, "backup": None,
        }

    backup = str(backup_store())

    if delete:
        closed.pop(target_idx)
    else:
        trade["pnl_usdt"] = float(real_pnl)
        notional = float(trade.get("notional", 1) or 1)
        lev = float(trade.get("leverage", 1) or 1)
        margin = notional / max(lev, 1e-9)
        trade["pnl_pct"] = round(float(real_pnl) / max(margin, 0.01) * 100, 2)
        closed[target_idx] = trade

    store["closed_trades"] = closed
    computed = _rebuild_stats(store)
    _apply_stats(store, computed)
    save_store(store)

    return {
        "changed": True, "dry_run": False, "action": "delete" if delete else "correct",
        "trade": before, "new_pnl": real_pnl, "backup": backup,
        "after": {k: computed[k] for k in ("total_trades", "wins", "losses", "total_realized")},
    }


# ── inject-sltp ─────────────────────────────────────────────────────────────

# ATR / multiplier config (swing-style, matches the original inject_sl_tp.py)
_ATR_PERIOD = 14
_ATR_INTERVAL = "60"   # 1H candles
_ATR_LIMIT = 50
_SL_MULT = 1.5
_TP1_MULT = 3.0
_TP2_MULT = 5.0
_TP3_MULT = 7.0
_TRAIL_MULT = 3.0


def _fetch_candles(symbol: str) -> List[Dict[str, float]]:
    import requests
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear", "symbol": symbol,
        "interval": _ATR_INTERVAL, "limit": _ATR_LIMIT,
    }
    r = requests.get(url, params=params, timeout=8)
    r.raise_for_status()
    raw = r.json().get("result", {}).get("list", [])
    return [
        {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
        for c in reversed(raw)
    ]


def _calc_atr(candles: List[Dict[str, float]], period: int = _ATR_PERIOD) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


def _clamp_trail(atr_pct: float, leverage: float) -> float:
    """Mirror pnl_tracker._calc_trail_pct (swing-aware)."""
    from config import Config
    is_swing = Config.TRADING_MODE == "swing"
    mult = 2.0 if is_swing else _TRAIL_MULT
    t = max(atr_pct * mult, 0.8)
    t = min(t, 8.0)
    if is_swing:
        if leverage >= 30:
            t = min(t, 4.0)
        elif leverage >= 8:
            t = min(t, 3.0)
    else:
        if leverage >= 8:
            t = min(t, 2.0)
        elif leverage >= 5:
            t = min(t, 4.0)
    return round(t, 2)


def inject_sltp(dry_run: bool = False) -> Dict[str, Any]:
    """Attach ATR-based SL/TP to open positions missing protective levels."""
    store = load_store()
    positions = store.get("open_positions", {})
    targets = {
        sym: pos for sym, pos in positions.items()
        if pos.get("stop_loss", 0) == 0 or not pos.get("take_profit")
    }

    if not targets:
        return {"changed": False, "message": "All open positions already have SL/TP."}

    backup = None if dry_run else str(backup_store())
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for sym, pos in targets.items():
        action = pos.get("action", "LONG")
        entry = float(pos.get("entry_price", 0))
        leverage = int(pos.get("leverage", 1))
        is_long = action in ("LONG", "BUY")

        try:
            candles = _fetch_candles(sym)
        except Exception as exc:  # noqa: BLE001
            errors.append({"symbol": sym, "error": str(exc)})
            continue

        atr = _calc_atr(candles)
        cur = candles[-1]["close"] if candles else 0.0
        atr_pct = (atr / cur * 100) if cur > 0 else 0.0
        trail_pct = _clamp_trail(atr_pct, leverage)

        if entry == 0 or atr == 0:
            errors.append({"symbol": sym, "error": f"invalid entry/atr (entry={entry}, atr={atr})"})
            continue

        if is_long:
            sl = round(entry - _SL_MULT * atr, 8)
            tp1, tp2, tp3 = (round(entry + m * atr, 8) for m in (_TP1_MULT, _TP2_MULT, _TP3_MULT))
        else:
            sl = round(entry + _SL_MULT * atr, 8)
            tp1, tp2, tp3 = (round(entry - m * atr, 8) for m in (_TP1_MULT, _TP2_MULT, _TP3_MULT))

        sl_final, trailing_active, state = sl, False, "normal"
        if is_long:
            if cur >= tp2:
                sl_final, trailing_active, state = tp1, True, "trailing (past TP2)"
            elif cur >= tp1:
                sl_final, trailing_active, state = entry, True, "breakeven (past TP1)"
        else:
            if cur <= tp2:
                sl_final, trailing_active, state = tp1, True, "trailing (past TP2)"
            elif cur <= tp1:
                sl_final, trailing_active, state = entry, True, "breakeven (past TP1)"

        if not dry_run:
            pos["stop_loss"] = sl_final
            pos["original_sl"] = sl
            pos["take_profit"] = [tp1, tp2, tp3]
            pos["trailing_active"] = trailing_active
            pos["trailing_activation_price"] = tp1
            pos["trailing_trail_pct"] = trail_pct

        results.append({
            "symbol": sym, "action": action, "entry": entry, "current": cur,
            "atr": atr, "atr_pct": atr_pct, "state": state,
            "sl": sl_final, "original_sl": sl, "tp": [tp1, tp2, tp3],
            "trailing_active": trailing_active, "trail_pct": trail_pct,
        })

    if not dry_run and results:
        save_store(store)

    return {
        "changed": bool(results) and not dry_run,
        "dry_run": dry_run,
        "backup": backup,
        "updated": results,
        "errors": errors,
    }
