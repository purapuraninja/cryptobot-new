"""
ui/console.py — terminal dashboard rendering.

Pure presentation: takes already-computed stats/positions/signals and prints
the colored box dashboard. No trading logic, no state mutation. Extracted
verbatim from main.py so behavior is unchanged.
"""

from datetime import datetime
from typing import Dict, List


# ── ANSI color helpers ──────────────────────────────────────────────────────

def _clr(text: str, code: str) -> str:
    """ANSI color wrapper."""
    codes = {
        "green":  "\033[92m", "red":    "\033[91m",
        "yellow": "\033[93m", "cyan":   "\033[96m",
        "white":  "\033[97m", "grey":   "\033[90m",
        "bold":   "\033[1m",  "reset":  "\033[0m",
    }
    return f"{codes.get(code,'')}{text}{codes['reset']}"


def _pnl_color(val: float) -> str:
    if val > 0:
        return _clr(f"+{val:,.4f}", "green")
    if val < 0:
        return _clr(f"{val:,.4f}", "red")
    return _clr(f"{val:,.4f}", "grey")


def _pct_color(val: float) -> str:
    if val > 0:
        return _clr(f"+{val:.2f}%", "green")
    if val < 0:
        return _clr(f"{val:.2f}%", "red")
    return _clr(f"{val:.2f}%", "grey")


def print_dashboard(
    scan: int,
    signals: List[Dict],
    executed: int,
    balance: float,
    exchange: str,
    dry_run: bool,
    next_scan: int,
    data_sources: Dict[str, str],
    pnl_stats: Dict,
    open_positions: List[Dict],
    recent_trades: List[Dict],
    adaptive_mode: str = "normal",
) -> None:
    W = 66
    now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    mode = _clr("DRY RUN", "yellow") if dry_run else _clr("LIVE", "green")

    print()
    print(_clr("=" * W, "cyan"))
    title = f"  AI QUANT BOT  [{mode}{_clr('', 'reset')}]  |  {exchange}  |  Scan #{scan:04d}"
    print(title)
    print(f"  {_clr(now, 'grey')}")
    print(_clr("=" * W, "cyan"))

    # ── Account ───────────────────────────────────────────────
    realized = pnl_stats.get("realized_pnl", 0.0)
    unrealized = pnl_stats.get("unrealized_pnl", 0.0)
    total_trades = pnl_stats.get("total_trades", 0)
    win_rate = pnl_stats.get("win_rate", 0.0)
    wins = pnl_stats.get("wins", 0)
    losses = pnl_stats.get("losses", 0)
    open_cnt = pnl_stats.get("open_count", 0)

    print(f"  {_clr('ACCOUNT', 'bold')}")
    print(f"  Balance      : {_clr(f'{balance:>12,.2f} USDT', 'white')}")
    print(f"  Realized PnL : {_pnl_color(realized):>20}  USDT")
    print(f"  Unrealized   : {_pnl_color(unrealized):>20}  USDT")
    print(f"  Positions    : {open_cnt} open  |  Next scan: {next_scan}s")

    # ── Adaptive mode ────────────────────────────────────────
    if adaptive_mode != "normal":
        mode_colors = {"defensive": "red", "conservative": "yellow", "aggressive": "green"}
        mc = mode_colors.get(adaptive_mode, "grey")
        print(f"  AI Mode      : {_clr(adaptive_mode.upper(), mc)}")

    # ── Stats ─────────────────────────────────────────────────
    if total_trades > 0:
        print(_clr("-" * W, "cyan"))
        print(f"  {_clr('PERFORMANCE', 'bold')}")
        wr_col = _clr(f"{win_rate:.1f}%", "green" if win_rate >= 50 else "red")
        print(f"  Win Rate     : {wr_col}  ({wins}W / {losses}L / {total_trades} trades)")
        best = pnl_stats.get("best_trade", 0.0)
        worst = pnl_stats.get("worst_trade", 0.0)
        bsym = pnl_stats.get("best_symbol", "")
        wsym = pnl_stats.get("worst_symbol", "")
        if best != 0:
            print(f"  Best Trade   : {_pnl_color(best)} USDT  {_clr(bsym, 'grey')}")
        if worst != 0:
            print(f"  Worst Trade  : {_pnl_color(worst)} USDT  {_clr(wsym, 'grey')}")

    # ── Open positions ────────────────────────────────────────
    if open_positions:
        print(_clr("-" * W, "cyan"))
        print(f"  {_clr('OPEN POSITIONS', 'bold')}")
        for p in open_positions:
            sym = p["symbol"]
            act = p["action"]
            act_col = _clr(f"^{act}", "green") if act == "LONG" else _clr(f"v{act}", "red")
            entry = p.get("entry_price", 0)
            cur = p.get("current_price", 0)
            upnl = p.get("unrealized_pnl", 0.0)
            upct = p.get("unrealized_pnl_pct", 0.0)
            lev = p.get("leverage", 0)
            remaining = p.get("remaining_pct", 1.0)
            trailing = p.get("trailing_active", False)
            tp_trailing = p.get("tp_trail_active", False)

            remain_str = f" {remaining*100:.0f}%" if remaining < 1.0 else ""
            # [TP>] = runner post-TP3, target bergeser ikut harga.
            # Lebih informatif dari [T] karena rezim proteksinya beda
            # (giveback ketat, bukan trail lebar).
            if tp_trailing:
                _tgt = p.get("tp_trail_target", 0) or 0
                trail_str = _clr(f" [TP>{_tgt:,.4f}]", "green")
            elif trailing:
                trail_str = _clr(" [T]", "cyan")
            else:
                trail_str = ""

            print(f"  {act_col:8s}  {_clr(sym, 'white'):<14s}"
                  f"  {_clr('E:', 'grey')}{entry:>10,.4f}"
                  f"  {_clr('C:', 'grey')}{cur:>10,.4f}"
                  f"  {_pnl_color(upnl)} ({_pct_color(upct)})  {lev}x"
                  f"{_clr(remain_str, 'yellow')}{trail_str}")

    # ── Recent trades ─────────────────────────────────────────
    if recent_trades:
        print(_clr("-" * W, "cyan"))
        print(f"  {_clr('RECENT TRADES', 'bold')}")
        for t in reversed(recent_trades[-4:]):
            sym = t["symbol"]
            act = t["action"]
            pnl = float(t["pnl_usdt"])
            pct = float(t["pnl_pct"])
            reason = t.get("reason", "")
            reason_sym = {"stop_loss": "SL", "take_profit": "TP",
                          "trailing_stop": "TS",
                          "trailing_take_profit": "TTP",
                          "tp_trail_max_extension": "TTPmx",
                          "tp_trail_stall": "TTPst"}.get(reason, reason[:6])
            act_s = _clr("^", "green") if act == "LONG" else _clr("v", "red")
            print(f"  {act_s}  {_clr(sym, 'white'):<14s}"
                  f"  {_pnl_color(pnl):>20}  ({_pct_color(pct)})  {reason_sym}")

    # ── Data sources ──────────────────────────────────────────
    if data_sources:
        src_items = list(data_sources.items())
        src_str = "  ".join(_clr(f"{s}:{e}", "grey") for s, e in src_items[:5])
        print(_clr("-" * W, "cyan"))
        print(f"  {_clr('DATA', 'bold')}  {src_str}")

    # ── Signals ───────────────────────────────────────────────
    active_signals = [s for s in signals if s.get("action", "HOLD") in ("LONG", "SHORT")]
    hold_signals   = [s for s in signals if s.get("action", "HOLD") == "HOLD"]

    print(_clr("-" * W, "cyan"))
    print(f"  {_clr('SIGNALS', 'bold')}"
          + (f"  {_clr(f'({len(hold_signals)} HOLD)', 'grey')}" if hold_signals else ""))

    if not active_signals:
        print(f"  {_clr('No actionable signals this cycle.', 'grey')}")
    else:
        for s in active_signals:
            action = s.get("action", "HOLD")
            sym    = s.get("symbol", "?")
            score  = float(s.get("score", 0))
            conf   = float(s.get("confidence", 0)) * 100
            reason = (s.get("reason") or "").strip()

            tag    = _clr("^ LONG ", "green") if action == "LONG" else _clr("v SHORT", "red")
            score_col = _clr(f"{score:.1f}", "green" if score >= 7 else ("yellow" if score >= 5.5 else "grey"))
            conf_col  = _clr(f"{conf:.0f}%", "green" if conf >= 70 else ("yellow" if conf >= 60 else "grey"))

            print(f"  {tag}  {_clr(sym, 'white'):<14s}"
                  f"  Score:{score_col:>8}  Conf:{conf_col:>7}")

            entry  = float(s.get("entry_price", 0))
            sl     = float(s.get("stop_loss", 0))
            tps    = [float(v) for v in s.get("take_profit", []) if v]
            lev    = s.get("leverage", 0)
            size   = s.get("position_size_percent", 0)
            risk   = s.get("risk_level", "")
            risk_c = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red"}.get(risk, "grey")

            print(f"         {_clr('Entry', 'grey')} {_clr(f'{entry:>12,.4f}', 'white')}"
                  f"  {_clr('SL', 'grey')} {_clr(f'{sl:>12,.4f}', 'red')}")
            for i, tp in enumerate(tps, 1):
                print(f"         {_clr(f'TP{i}  ', 'grey')} {_clr(f'{tp:>12,.4f}', 'green')}")
            print(f"         Lev:{_clr(f'{lev}x', 'white')}  "
                  f"Size:{_clr(f'{size}%', 'white')}  "
                  f"Risk:{_clr(risk, risk_c)}")

            if reason:
                words, line = reason.split(), "         "
                for w in words:
                    if len(line) + len(w) + 1 > W - 4:
                        print(_clr(line.rstrip(), "grey"))
                        line = "         " + w + " "
                    else:
                        line += w + " "
                if line.strip():
                    print(_clr(line.rstrip(), "grey"))

            trail = s.get("trailing_stop", {})
            if isinstance(trail, dict) and trail.get("enabled"):
                print(f"         {_clr('Trailing: ' + trail.get('rule', ''), 'cyan')}")
            print()

    print(_clr("=" * W, "cyan"))
