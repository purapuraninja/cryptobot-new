"""
dashboard.py — Flask web dashboard server for AI Quant Bot.

Serves:
  GET /           → dashboard.html
  GET /api/data   → JSON with bot state (live-refreshed from pnl_store.json + trade_memory.json)

Run:
  python dashboard.py
  Then open: http://localhost:5050
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import requests as req
from flask import Flask, jsonify, send_from_directory, request

# ── Paths ─────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
PNL_STORE     = BASE_DIR / "pnl_store.json"
TRADE_MEMORY  = BASE_DIR / "trade_memory.json"
DASHBOARD_HTML = BASE_DIR / "dashboard.html"

app = Flask(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _use_sqlite() -> bool:
    try:
        from config import Config
        return Config.STORAGE_BACKEND == "sqlite"
    except Exception:
        return False


def _load_pnl_store() -> dict:
    if _use_sqlite():
        try:
            from storage import sqlite_backend
            return sqlite_backend.load_store()
        except Exception:
            pass
    return _load_json(PNL_STORE)


def _load_trade_memory() -> dict:
    if _use_sqlite():
        try:
            from storage import sqlite_backend
            return sqlite_backend.load_memory()
        except Exception:
            pass
    return _load_json(TRADE_MEMORY)


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _fetch_bybit_prices(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        resp = req.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear"},
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json().get("result", {}).get("list", [])
        wanted = {s.upper().replace("/", "").replace(":USDT", "") for s in symbols}
        prices = {}
        for row in rows:
            sym = str(row.get("symbol", "")).upper()
            if sym in wanted:
                prices[sym] = _safe_float(row.get("lastPrice", 0))
        return prices
    except Exception:
        return {}


def _build_api_data() -> dict:
    pnl   = _load_pnl_store()
    mem   = _load_trade_memory()

    # ── Config from env (bot writes .env, we read it here too) ───────────
    dry_run        = os.getenv("DRY_RUN", "true").lower() == "true"
    trading_mode   = os.getenv("TRADING_MODE", "intraday").lower()
    initial_bal    = _safe_float(os.getenv("ACCOUNT_BALANCE", "10000"))  # starting capital

    # ── Open positions ────────────────────────────────────────────────────
    raw_positions = pnl.get("open_positions", {})
    open_positions = []
    live_prices = _fetch_bybit_prices(list(raw_positions.keys()))

    for sym, pos in raw_positions.items():
        action      = pos.get("action", "LONG")
        entry_price = _safe_float(pos.get("entry_price", 0))
        leverage    = int(_safe_float(pos.get("leverage", 1)))
        notional    = _safe_float(pos.get("notional", 0))
        trailing    = bool(pos.get("trailing_active", False))
        status      = pos.get("status", "")

        canonical = sym.upper().replace("/", "").replace(":USDT", "")

        # current_price: live Bybit price first, then stored pos price, then entry fallback
        current_price = live_prices.get(canonical, _safe_float(pos.get("current_price", entry_price)))
        if current_price == 0:
            current_price = entry_price

        # unrealized pnl
        if entry_price > 0 and current_price > 0:
            price_move_pct = (current_price - entry_price) / entry_price * 100
            if action in ("SELL", "SHORT"):
                price_move_pct = -price_move_pct
            unrealized_pnl_pct = price_move_pct * leverage
            unrealized_pnl     = notional * price_move_pct / 100
        else:
            unrealized_pnl_pct = 0.0
            unrealized_pnl     = 0.0

        open_positions.append({
            "symbol":              sym,
            "action":              "BUY" if action in ("BUY", "LONG") else "SELL",
            "entry_price":         entry_price,
            "current_price":       current_price,
            "unrealized_pnl":      round(unrealized_pnl, 2),
            "unrealized_pnl_pct":  round(unrealized_pnl_pct, 2),
            "leverage":            leverage,
            "trailing_active":     trailing,
            "status":              status,
        })

    # ── Closed trades — single source of truth: pnl_store.closed_trades ──────
    # trade_memory.json is for AI learning only and is NOT reset when pnl_store
    # is reset/backed up, so it can contain stale historical data that skews stats.
    from utils.trade_analytics import aggregate_position_trades, calculate_position_metrics

    all_trades_raw = pnl.get("closed_trades", [])
    all_trades = aggregate_position_trades(all_trades_raw)
    position_metrics = calculate_position_metrics(all_trades_raw)

    total_trades = len(all_trades)
    wins         = position_metrics["wins"]
    losses       = position_metrics["losses"]
    flats        = position_metrics["flats"]

    # Realized PnL: use stats.total_realized from pnl_store — same source as bot.
    # Bot accumulates PnL from BOTH full closes AND partial TP hits (TP1/TP2).
    # Summing full closes only would drop all partial TP PnL → wrong figure.
    realized_pnl = _safe_float(pnl.get("stats", {}).get("total_realized", 0.0))

    pnls = [_safe_float(t.get("position_pnl_usdt", 0)) for t in all_trades]
    best_trade  = max(pnls) if pnls else 0.0
    worst_trade = min(pnls) if pnls else 0.0

    # best / worst symbol by total PnL
    sym_totals: dict[str, float] = {}
    for t in all_trades:
        s = t.get("symbol", "?")
        sym_totals[s] = sym_totals.get(s, 0.0) + _safe_float(t.get("position_pnl_usdt", 0))
    best_symbol  = max(sym_totals, key=sym_totals.get) if sym_totals else "—"
    worst_symbol = min(sym_totals, key=sym_totals.get) if sym_totals else "—"

    # Win streak
    streak = 0
    for t in reversed(all_trades):
        p = _safe_float(t.get("position_pnl_usdt", 0))
        if p == 0:
            break
        if streak == 0:
            streak = 1 if p > 0 else -1
        elif (p > 0 and streak > 0):
            streak += 1
        elif (p < 0 and streak < 0):
            streak -= 1
        else:
            break

    # ── Recent trades (last 15, full + partial TP hits) ──────────────────
    # Show all events so the user sees the complete picture of each trade:
    # TP1 partial → TP2 partial → final close all appear in sequence.
    recent_raw = list(reversed(all_trades_raw))[:15]
    recent_trades = []
    for t in recent_raw:
        reason_raw = t.get("reason", "")
        is_partial = bool(t.get("partial"))
        # Normalise reason
        # Trailing-TP exits harus dicek SEBELUM cabang tp*/take_profit:
        # 'trailing_take_profit' mengandung 'take_profit' dan 'tp_trail_*'
        # mengandung 'tp', jadi tanpa ini runner-exit tercampur ke TP3.
        if "trailing_take_profit" in reason_raw or "tp_trail" in reason_raw:
            reason = "TTP"
        elif "tp3" in reason_raw or "take_profit_3" in reason_raw:
            reason = "TP3"
        elif "tp2" in reason_raw or "take_profit_2" in reason_raw:
            reason = "TP2" if not is_partial else "TP2•"
        elif "tp1" in reason_raw or "take_profit_1" in reason_raw:
            reason = "TP1" if not is_partial else "TP1•"
        elif "trail" in reason_raw:
            reason = "TRAIL"
        elif "sl" in reason_raw or "stop" in reason_raw:
            reason = "SL"
        else:
            reason = reason_raw.upper()[:6]

        # pnl_store uses "close_time"
        close_time = t.get("close_time", t.get("exit_time", "—"))
        if len(close_time) > 16:
            close_time = close_time[:16]

        recent_trades.append({
            "symbol":      t.get("symbol", "?"),
            "action":      "BUY" if t.get("action", "LONG") in ("BUY","LONG") else "SELL",
            "entry_price": _safe_float(t.get("entry_price", 0)),
            "exit_price":  _safe_float(t.get("exit_price", 0)),
            "pnl_usdt":    round(_safe_float(t.get("pnl_usdt", 0)), 2),
            "pnl_pct":     round(_safe_float(t.get("pnl_pct", 0)), 2),
            "reason":      reason,
            "close_time":  close_time,
            "is_partial":  is_partial,
        })

    # ── Symbol stats ──────────────────────────────────────────────────────
    symbol_stats: dict[str, dict] = {}
    for t in all_trades:
        s = t.get("symbol", "?")
        p = _safe_float(t.get("position_pnl_usdt", 0))
        sc = _safe_float(t.get("ai_score", 0))
        if s not in symbol_stats:
            symbol_stats[s] = {"trades": 0, "wins": 0, "total_pnl": 0.0, "_scores": []}
        symbol_stats[s]["trades"] += 1
        if p > 0:
            symbol_stats[s]["wins"] += 1
        symbol_stats[s]["total_pnl"] += p
        if sc > 0:
            symbol_stats[s]["_scores"].append(sc)

    # Clean up and compute avg_score
    clean_ss = {}
    for s, v in symbol_stats.items():
        scores = v.pop("_scores", [])
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0
        clean_ss[s] = {
            "trades":    v["trades"],
            "wins":      v["wins"],
            "total_pnl": round(v["total_pnl"], 2),
            "avg_score": avg_score,
        }

    # ── 14-day chart ──────────────────────────────────────────────────────
    today = datetime.utcnow().date()
    day_map: dict[str, float] = {}
    for t in all_trades_raw:
        # pnl_store uses "close_time"; fallback to "exit_time" or "entry_time"
        raw_t = t.get("close_time", t.get("exit_time", t.get("entry_time", "")))
        try:
            d = raw_t[:10]  # "YYYY-MM-DD"
            day_map[d] = day_map.get(d, 0.0) + _safe_float(t.get("pnl_usdt", 0))
        except Exception:
            pass

    chart_labels = []
    chart_values = []
    for i in range(13, -1, -1):
        day = today - timedelta(days=i)
        d_str = day.strftime("%Y-%m-%d")
        label = day.strftime("%b %-d") if os.name != "nt" else day.strftime("%b %#d")
        chart_labels.append(label)
        chart_values.append(round(day_map.get(d_str, 0.0), 2))

    # ── Balance ───────────────────────────────────────────────────────────
    # Both dry-run and live: initial_balance + cumulative realized PnL
    # realized_pnl already reads from stats.total_realized (includes partial TP PnL)
    balance = initial_bal + realized_pnl

    return {
        "dry_run":         dry_run,
        "trading_mode":    trading_mode,
        "initial_balance": round(initial_bal, 2),
        "balance":         round(balance, 2),
        "stats": {
            "realized_pnl":  round(realized_pnl, 2),
            "total_trades":  total_trades,
            "wins":          wins,
            "losses":        losses,
            "flats":         flats,
            "profit_factor": position_metrics["profit_factor"],
            "expectancy_usdt": position_metrics["expectancy_usdt"],
            "max_drawdown_usdt": position_metrics["max_drawdown_usdt"],
            "best_trade":    round(best_trade, 2),
            "worst_trade":   round(worst_trade, 2),
            "best_symbol":   best_symbol,
            "worst_symbol":  worst_symbol,
        },
        "streak":         streak,
        "open_positions": open_positions,
        "recent_trades":  recent_trades,
        "chart_labels":   chart_labels,
        "chart_values":   chart_values,
        "symbol_stats":   clean_ss,
    }


# ── Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "dashboard.html")


@app.route("/api/closed_positions")
def api_closed_positions():
    """
    Paginated closed positions.
    Query params:
      page     : 1-indexed page number (default 1)
      per_page : rows per page (default 20, max 100)
      symbol   : optional symbol filter (case-insensitive)
      sort     : field to sort by (close_time|pnl_usdt|symbol) — default close_time
      order    : asc|desc — default desc
    """
    page     = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    sym_filter = request.args.get("symbol", "").upper().strip()
    sort_field = request.args.get("sort", "close_time")
    order      = request.args.get("order", "desc").lower()

    pnl = _load_pnl_store()
    mem = _load_trade_memory()

    # Prefer pnl_store closed_trades (richer data: open_time, close_time, notional)
    # Supplement with trade_memory for entries not in pnl_store
    raw_closed = list(pnl.get("closed_trades", []))

    def _normalise_reason(r: str) -> str:
        r = r.lower()
        # Cek trailing-TP dulu: 'trailing_take_profit' cocok dengan
        # 'take_profit' dan 'tp_trail_*' cocok dengan 'tp', jadi tanpa
        # cabang ini runner-exit akan salah dihitung sebagai TP3/TP statis.
        if "trailing_take_profit" in r or "tp_trail" in r:  return "TTP"
        if "tp3" in r or "take_profit_3" in r:  return "TP3"
        if "tp2" in r or "take_profit_2" in r:  return "TP2"
        if "tp1" in r or "take_profit_1" in r:  return "TP1"
        if "take_profit" in r:                   return "TP"
        if "trail" in r:                         return "TRAIL"
        if "sl" in r or "stop_loss" in r:        return "SL"
        if "exchange" in r:                      return "EXCH"
        return r.upper()[:6]

    rows = []
    for t in raw_closed:
        close_t = t.get("close_time", t.get("exit_time", ""))
        open_t  = t.get("open_time",  t.get("entry_time", ""))
        sym     = t.get("symbol", "?")
        if sym_filter and sym_filter not in sym.upper():
            continue
        action = t.get("action", "LONG")
        rows.append({
            "symbol":      sym,
            "action":      "BUY" if action in ("BUY","LONG") else "SELL",
            "entry_price": _safe_float(t.get("entry_price", 0)),
            "exit_price":  _safe_float(t.get("exit_price", 0)),
            "pnl_usdt":    round(_safe_float(t.get("pnl_usdt", 0)), 4),
            "pnl_pct":     round(_safe_float(t.get("pnl_pct", 0)), 2),
            "leverage":    int(_safe_float(t.get("leverage", 1))),
            "notional":    round(_safe_float(t.get("notional", 0)), 2),
            "reason":      _normalise_reason(t.get("reason", "")),
            "open_time":   open_t[:16] if len(open_t) > 16 else open_t,
            "close_time":  close_t[:16] if len(close_t) > 16 else close_t,
            "partial":     bool(t.get("partial", False)),
        })

    # Sort
    VALID_SORTS = {"close_time", "pnl_usdt", "symbol", "pnl_pct", "open_time"}
    if sort_field not in VALID_SORTS:
        sort_field = "close_time"
    rows.sort(key=lambda r: r.get(sort_field, "") or "", reverse=(order == "desc"))

    total  = len(rows)
    start  = (page - 1) * per_page
    end    = start + per_page
    paged  = rows[start:end]

    return jsonify({
        "page":       page,
        "per_page":   per_page,
        "total":      total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "trades":     paged,
    })


@app.route("/api/calendar")
def api_calendar():
    """
    Realized PnL calendar for a given month.
    Query params:
      year  : e.g. 2026   (default: current UTC year)
      month : e.g. 5      (default: current UTC month)
      mode  : total_pnl | fees_only (default: total_pnl — fees_only returns 0 always since we don't track fees separately)
    Returns:
      {
        year, month,
        days: { "YYYY-MM-DD": { pnl, positions, wins, win_rate } },
        weeks: [ { week_num, label, pnl, positions, days_active } ],
        monthly: { pnl, positions, days_active }
      }
    """
    import calendar as cal_mod
    now   = datetime.utcnow()
    year  = int(request.args.get("year",  now.year))
    month = int(request.args.get("month", now.month))
    year  = max(2020, min(2099, year))
    month = max(1,    min(12,   month))

    pnl_data = _load_pnl_store()
    raw_closed = pnl_data.get("closed_trades", [])
    from utils.trade_analytics import aggregate_position_trades
    completed_positions = aggregate_position_trades(raw_closed)

    # Build day-level aggregation
    day_agg: dict[str, dict] = {}

    # Pass 1: accumulate PnL from ALL closes (full + partial) for the correct daily total
    for t in raw_closed:
        close_t = t.get("close_time", t.get("exit_time", ""))
        if not close_t or len(close_t) < 10:
            continue
        d_str = close_t[:10]           # "YYYY-MM-DD"
        try:
            td = datetime.strptime(d_str, "%Y-%m-%d")
        except Exception:
            continue
        if td.year != year or td.month != month:
            continue
        p = _safe_float(t.get("pnl_usdt", 0))
        if d_str not in day_agg:
            day_agg[d_str] = {"pnl": 0.0, "positions": 0, "wins": 0}
        day_agg[d_str]["pnl"] += p

    # Pass 2: count completed lifecycles on their terminal close date.
    for t in completed_positions:
        close_t = t.get("close_time", t.get("exit_time", ""))
        if not close_t or len(close_t) < 10:
            continue
        d_str = close_t[:10]
        try:
            td = datetime.strptime(d_str, "%Y-%m-%d")
        except Exception:
            continue
        if td.year != year or td.month != month:
            continue
        if d_str not in day_agg:
            day_agg[d_str] = {"pnl": 0.0, "positions": 0, "wins": 0}
        p = _safe_float(t.get("position_pnl_usdt", 0))
        day_agg[d_str]["positions"] += 1
        if p > 0:
            day_agg[d_str]["wins"] += 1

    # Round and compute win_rate per day
    days_out = {}
    for d_str, v in day_agg.items():
        pos = v["positions"]
        days_out[d_str] = {
            "pnl":       round(v["pnl"], 4),
            "positions": pos,
            "wins":      v["wins"],
            "win_rate":  round(v["wins"] / pos * 100, 1) if pos > 0 else 0.0,
        }

    # Build ISO week groupings aligned to the calendar month
    first_day = datetime(year, month, 1)
    last_day  = datetime(year, month, cal_mod.monthrange(year, month)[1])

    # Group by week-of-month (week starts Sunday)
    # We label weeks Week 1..N where Week 1 = days 1-7 ish
    weeks_out = []
    # collect days in month sorted
    all_days_in_month = []
    d = first_day
    while d <= last_day:
        all_days_in_month.append(d)
        d += timedelta(days=1)

    # split into 7-day chunks anchored to Sunday
    # find first Sunday on or before first_day
    dow = first_day.weekday()  # Mon=0..Sun=6
    # our week starts Sunday: offset = (dow+1) % 7 days back from first_day
    sunday_offset = (dow + 1) % 7
    chunk_start = first_day - timedelta(days=sunday_offset)

    week_num = 0
    while chunk_start <= last_day:
        week_num += 1
        chunk_end   = chunk_start + timedelta(days=6)
        week_pnl    = 0.0
        week_pos    = 0
        week_days   = 0
        week_label  = f"Week {week_num}"

        for offset in range(7):
            day = chunk_start + timedelta(days=offset)
            d_str = day.strftime("%Y-%m-%d")
            if d_str in days_out:
                week_pnl  += days_out[d_str]["pnl"]
                week_pos  += days_out[d_str]["positions"]
                week_days += 1

        # date range label for the week
        w_start_label = max(chunk_start, first_day).strftime("%-d") if os.name != "nt" \
                        else max(chunk_start, first_day).strftime("%#d")
        w_end_label   = min(chunk_end, last_day).strftime("%-d")   if os.name != "nt" \
                        else min(chunk_end, last_day).strftime("%#d")
        days_active_label = f"{week_days} day{'s' if week_days != 1 else ''}"

        weeks_out.append({
            "week_num":    week_num,
            "label":       week_label,
            "date_range":  f"{w_start_label}–{w_end_label}",
            "pnl":         round(week_pnl, 4),
            "positions":   week_pos,
            "days_active": week_days,
            "days_label":  days_active_label,
        })
        chunk_start += timedelta(days=7)

    # Monthly totals
    monthly_pnl  = sum(v["pnl"] for v in days_out.values())
    monthly_pos  = sum(v["positions"] for v in days_out.values())
    monthly_days = len(days_out)

    return jsonify({
        "year":    year,
        "month":   month,
        "days":    days_out,
        "weeks":   weeks_out,
        "monthly": {
            "pnl":         round(monthly_pnl, 4),
            "positions":   monthly_pos,
            "days_active": monthly_days,
        },
    })


@app.route("/api/data")
def api_data():
    try:
        data = _build_api_data()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<symbol>")
def api_chart(symbol):
    """
    Fetch OHLCV candles from Bybit public API + entry/exit markers from bot data.
    Query params:
      interval : Bybit interval (1,5,15,60,240,D) — default 60
      limit    : number of candles — default 100
    """
    interval = request.args.get("interval", "60")
    limit    = min(int(request.args.get("limit", "100")), 200)

    # ── Fetch candles from Bybit public API ───────────────────────
    candles = []
    try:
        url    = "https://api.bybit.com/v5/market/kline"
        params = {
            "category": "linear",
            "symbol":   symbol.upper(),
            "interval": interval,
            "limit":    limit,
        }
        resp = req.get(url, params=params, timeout=8)
        data = resp.json()
        raw  = data.get("result", {}).get("list", [])
        # Bybit returns newest first — reverse for chart
        for item in reversed(raw):
            candles.append({
                "time":   int(item[0]) // 1000,   # ms → unix seconds
                "open":   float(item[1]),
                "high":   float(item[2]),
                "low":    float(item[3]),
                "close":  float(item[4]),
                "volume": float(item[5]),
            })
    except Exception as e:
        return jsonify({"error": f"Bybit fetch failed: {e}"}), 502

    # ── Build markers from bot data ───────────────────────────────
    markers = []
    pnl     = _load_pnl_store()
    mem     = _load_trade_memory()
    sym_up  = symbol.upper()

    # Open positions → entry marker
    for sym, pos in pnl.get("open_positions", {}).items():
        if sym != sym_up:
            continue
        open_ts = pos.get("open_time", "")
        try:
            dt = datetime.strptime(open_ts[:19], "%Y-%m-%d %H:%M:%S")
            ts = int(dt.timestamp())
        except Exception:
            ts = candles[0]["time"] if candles else 0
        action = pos.get("action", "LONG")
        markers.append({
            "time":     ts,
            "position": "belowBar" if action in ("LONG","BUY") else "aboveBar",
            "color":    "#23c55e" if action in ("LONG","BUY") else "#f85149",
            "shape":    "arrowUp" if action in ("LONG","BUY") else "arrowDown",
            "text":     f"OPEN {action} @{pos.get('entry_price',0)}",
        })

    # Closed trades → entry + exit markers
    for t in mem.get("trades", []):
        if t.get("symbol", "") != sym_up:
            continue
        action = t.get("action", "LONG")
        pnl_v  = _safe_float(t.get("pnl_usdt", 0))
        reason = t.get("reason", "")

        # Entry marker
        entry_ts_raw = t.get("entry_time", "")
        if entry_ts_raw:
            try:
                dt = datetime.strptime(entry_ts_raw[:19], "%Y-%m-%d %H:%M:%S")
                markers.append({
                    "time":     int(dt.timestamp()),
                    "position": "belowBar" if action in ("LONG","BUY") else "aboveBar",
                    "color":    "#58a6ff",
                    "shape":    "arrowUp" if action in ("LONG","BUY") else "arrowDown",
                    "text":     f"ENTRY @{t.get('entry_price',0)}",
                })
            except Exception:
                pass

        # Exit marker
        exit_ts_raw = t.get("exit_time", "")
        if exit_ts_raw:
            try:
                dt = datetime.strptime(exit_ts_raw[:19], "%Y-%m-%d %H:%M:%S")
                sign = "+" if pnl_v >= 0 else ""
                markers.append({
                    "time":     int(dt.timestamp()),
                    "position": "aboveBar" if action in ("LONG","BUY") else "belowBar",
                    "color":    "#23c55e" if pnl_v >= 0 else "#f85149",
                    "shape":    "arrowDown" if action in ("LONG","BUY") else "arrowUp",
                    "text":     f"{reason.upper()} {sign}{pnl_v:.2f}U",
                })
            except Exception:
                pass

    # Sort markers by time
    markers.sort(key=lambda m: m["time"])

    return jsonify({"candles": candles, "markers": markers})


# ── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print("=" * 55)
    print("  AI Quant Bot Dashboard")
    print(f"  Open: http://localhost:5050")
    print(f"  pnl_store  : {PNL_STORE}")
    print(f"  trade_memory: {TRADE_MEMORY}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5050, debug=False)
