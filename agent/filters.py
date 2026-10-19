"""
agent/filters.py — deterministic signal filters, regime gates, adaptive rules.

Every function here is a pure predicate or a bounded signal transform used by
the entry gauntlet in main.py. They read Config and market snapshots but do
not touch the exchange or the AI. Extracted verbatim from main.py.

Contents:
  daily_realized_loss        — 6h rolling realized loss (circuit breaker input)
  global_regime_quality      — good/marginal/bad market regime
  passes_vol_regime_filter   — reject the 24/05 choppy-mid-range loss pattern
  symbol_trade_count_today    — anti-overtrading day counter
  symbol_hours_since_last_loss — loss cooldown input
  apply_adaptive_rules       — SL width / leverage cap / symbol penalty
  passes_smc_retest_filter   — chase / late-entry rejection (SMC/ICT)
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from utils.logger import logger


# ── Anti-overtrading helpers ───────────────────────────────────────────────

def daily_realized_loss(pnl: Any) -> float:
    """
    Return total realized LOSS (negative value) for trades closed in the last 6 hours.

    Resets every 6 hours instead of UTC midnight — gives bot more chances to trade
    after a losing streak without waiting until next day.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=6)
    total = 0.0
    for t in pnl._store.get("closed_trades", []):
        close_time = t.get("close_time", "")
        if not close_time:
            continue
        try:
            close_dt = datetime.strptime(close_time, "%Y-%m-%d %H:%M:%S")
            close_dt = close_dt.replace(tzinfo=timezone.utc)
            if close_dt >= cutoff:
                p = t.get("pnl_usdt", 0.0)
                if p < 0:
                    total += p
        except Exception:
            pass
    return total  # always <= 0


def global_regime_quality(market_data: List[Dict], btc_symbol: str = "BTCUSDT") -> str:
    """
    Return "good" / "marginal" / "bad" based on BTC + market-wide weak-volume share.

    bad      = skip whole AI scan (mirrors circuit_breaker_hit path)
    marginal = scan but raise score/conf floor (see Config.REGIME_MARGINAL_*)
    good     = normal operation

    Calibrated from 24/05/2026 incident: BTC range 0.3% / 5h, vol=weak,
    structure=sideways → 7-loss streak. Gate must trip BEFORE entry, not after.
    """
    from config import Config

    if Config.REGIME_BTC_ATR_THRESHOLD <= 0:
        return "good"  # disabled via env

    btc = next((m for m in market_data if m.get("symbol") == btc_symbol), None)
    if not btc:
        return "good"  # no BTC snapshot, fail-open

    btc_atr = float(btc.get("atr_pct", 1.0) or 1.0)
    btc_vol = str(btc.get("volume_condition", "normal"))
    btc_struct = str(btc.get("market_structure", "sideways"))

    weak_count = sum(
        1 for m in market_data
        if str(m.get("volume_condition", "normal")) == "weak"
    )
    weak_pct = weak_count / max(len(market_data), 1)

    btc_choppy = (
        btc_atr < Config.REGIME_BTC_ATR_THRESHOLD
        and btc_vol == "weak"
        and btc_struct == "sideways"
    )
    market_thin = weak_pct >= Config.REGIME_WEAK_SYMBOL_PCT

    if btc_choppy and market_thin:
        return "bad"
    if btc_choppy or market_thin:
        return "marginal"
    return "good"


def passes_vol_regime_filter(signal: Dict, snap: Dict) -> bool:
    """
    Hard reject signal matching the 24/05/2026 7-loss pattern:
    weak vol + non-rising vol_trend + sideways structure + RSI mid-range.

    AI tetap boleh generate signal di vol=weak (rule signal_engine.py:95),
    tapi kombinasi spesifik ini terbukti doomed di pasar ranging.
    """
    from config import Config

    if signal.get("action") not in ("LONG", "SHORT"):
        return True

    vol_cond = str(snap.get("volume_condition", "normal"))
    vol_trend = str(snap.get("volume_trend", "flat"))
    structure = str(snap.get("market_structure", "sideways"))
    rsi = float(snap.get("rsi", 50.0) or 50.0)

    if Config.REJECT_FALLING_VOLUME_ENTRIES and vol_trend == "falling":
        logger.warning(
            f"  [REGIME-VOLUME] {signal.get('symbol')} {signal.get('action')} "
            "rejected: volume_trend=falling (no participation confirmation)"
        )
        return False

    if Config.REGIME_RSI_NEUTRAL_BAND <= 0:
        return True  # disable only the choppy mid-range sub-filter

    if (
        vol_cond == "weak"
        and vol_trend != "rising"
        and structure == "sideways"
        and abs(rsi - 50.0) < Config.REGIME_RSI_NEUTRAL_BAND
    ):
        logger.warning(
            f"  [REGIME-VOL] {signal.get('symbol')} {signal.get('action')} "
            f"rejected: vol=weak, trend={vol_trend}, struct=sideways, "
            f"RSI={rsi:.1f} (choppy mid-range entry)"
        )
        return False
    return True


def symbol_trade_count_today(pnl: Any, symbol: str) -> int:
    """Count unique position lifecycles opened today for a symbol.

    TP1/TP2 rows are exit legs of the same position, not additional trades.
    Using ``(symbol, open_time)`` also prevents an open position with booked
    partials from being counted twice.
    """
    today = datetime.now(timezone.utc).date()
    lifecycles = set()
    for t in pnl._store.get("closed_trades", []):
        if t.get("symbol") != symbol:
            continue
        trade_time = t.get("open_time") or t.get("close_time", "")
        if trade_time:
            try:
                trade_date = datetime.strptime(trade_time, "%Y-%m-%d %H:%M:%S").date()
                if trade_date == today:
                    lifecycle_id = t.get("position_id") or t.get("open_time") or trade_time
                    lifecycles.add((symbol, lifecycle_id))
            except Exception:
                pass
    open_pos = pnl._store.get("open_positions", {}).get(symbol)
    if open_pos:
        open_time = open_pos.get("open_time", "")
        try:
            if datetime.strptime(open_time, "%Y-%m-%d %H:%M:%S").date() == today:
                lifecycles.add((symbol, open_pos.get("position_id") or open_time))
        except Exception:
            # An open position still consumes a daily slot even if a legacy
            # record has no parseable timestamp.
            lifecycles.add((symbol, open_time or "__open__"))
    return len(lifecycles)


def symbol_hours_since_last_loss(pnl: Any, symbol: str) -> float:
    """Return hours since the last losing completed lifecycle for a symbol."""
    from utils.trade_analytics import aggregate_position_trades

    now = datetime.now(timezone.utc)
    positions = aggregate_position_trades(
        pnl._store.get("closed_trades", []), include_incomplete=False
    )
    for t in reversed(positions):
        if t.get("symbol") != symbol:
            continue
        if float(t.get("position_pnl_usdt", 0.0) or 0.0) < 0:
            close_time = t.get("close_time", "")
            if close_time:
                try:
                    close_dt = datetime.fromisoformat(
                        str(close_time).replace("Z", "+00:00")
                    )
                    if close_dt.tzinfo is None:
                        close_dt = close_dt.replace(tzinfo=timezone.utc)
                    else:
                        close_dt = close_dt.astimezone(timezone.utc)
                    return (now - close_dt).total_seconds() / 3600
                except (TypeError, ValueError):
                    pass
    return 999.0


def symbol_side_loss_streak(pnl: Any, symbol: str, action: str) -> tuple[int, float]:
    """Return consecutive completed losses and hours since the latest one.

    The streak is scoped to the same symbol and direction.  Position-level
    aggregation is required because TP1/TP2 rows are exit legs, not separate
    trades.  A flat or winning lifecycle resets the streak.
    """
    from utils.trade_analytics import aggregate_position_trades

    positions = aggregate_position_trades(
        pnl._store.get("closed_trades", []), include_incomplete=False
    )
    matching = [
        trade for trade in positions
        if trade.get("symbol") == symbol
        and str(trade.get("action", "")).upper() == str(action).upper()
    ]

    streak = 0
    latest_close = None
    for trade in reversed(matching):
        if float(trade.get("position_pnl_usdt", 0.0) or 0.0) >= 0:
            break
        streak += 1
        if latest_close is None:
            latest_close = trade.get("close_time")

    if not latest_close:
        return streak, 999.0

    try:
        close_dt = datetime.fromisoformat(str(latest_close).replace("Z", "+00:00"))
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        else:
            close_dt = close_dt.astimezone(timezone.utc)
        hours = max(0.0, (datetime.now(timezone.utc) - close_dt).total_seconds() / 3600)
    except (TypeError, ValueError):
        hours = 999.0
    return streak, hours


# ── Phase 3: Adaptive Rules ────────────────────────────────────────────────

def apply_adaptive_rules(
    signal: Dict,
    market_snapshot: Dict,
    adaptive_params: Dict,
    memory: Any,
) -> Dict:
    """
    Phase 3 post-processing — applied to every LONG/SHORT signal before execution.

    Rules:
    1. Dynamic SL width — widen SL if tighter than volatility warrants.
       low_vol  (atr < 1%)  : min SL = 1.0x ATR
       med_vol  (1-3%)      : min SL = 1.5x ATR
       high_vol (> 3%)      : min SL = 2.0x ATR

    2. Leverage cap — hard ceiling per adaptive mode, then apply lev_mult.
       defensive    : max 3x
       conservative : max 5x
       normal/aggr  : max 10x (AI suggestion preserved)

    3. High-conviction attribution — tag high score trades, but do not increase
       size from score alone. Historical expectancy must confirm sizing edge.

    4. Symbol performance penalty — reduce size when symbol history shows
       negative expectancy.
    """
    from config import Config as _Cfg

    sym = signal.get("symbol", "?")
    action = signal.get("action", "HOLD")
    entry = float(signal.get("entry_price", 0))
    sl = float(signal.get("stop_loss", 0))
    leverage = int(signal.get("leverage", 5))
    size_pct = float(signal.get("position_size_percent", 2.0))
    atr_pct = float(market_snapshot.get("atr_pct", 1.5))

    # The adaptive engine previously calculated this multiplier but only sent
    # it to the AI.  Apply it deterministically so defensive/conservative mode
    # actually reduces exposure before the absolute max-loss cap.
    risk_mult = max(0.0, float(adaptive_params.get("risk_multiplier", 1.0) or 1.0))
    if risk_mult != 1.0:
        adjusted_size = round(size_pct * risk_mult, 4)
        signal["position_size_percent"] = adjusted_size
        logger.info(
            f"  [P3-RISK] {sym} size {size_pct:.2f}% → {adjusted_size:.2f}% "
            f"(adaptive risk×{risk_mult:.2f})"
        )
        size_pct = adjusted_size

    # ── 1. Dynamic SL width ───────────────────────────────────────
    if entry > 0 and sl > 0 and atr_pct > 0:
        current_sl_dist_pct = abs(entry - sl) / entry * 100
        if atr_pct < 1.0:
            min_sl_dist_pct = atr_pct * 1.0
        elif atr_pct < 3.0:
            min_sl_dist_pct = atr_pct * 1.5
        else:
            min_sl_dist_pct = atr_pct * 2.0

        # Hard floor: never accept SL tighter than MIN_SL_DISTANCE_PCT
        # (prevents fee-bleed + noise stopouts on low-vol coins)
        min_sl_dist_pct = max(min_sl_dist_pct, _Cfg.MIN_SL_DISTANCE_PCT)

        if current_sl_dist_pct < min_sl_dist_pct:
            new_sl_dist = min_sl_dist_pct / 100.0 * entry
            if action == "LONG":
                new_sl = round(entry - new_sl_dist, 8)
            else:
                new_sl = round(entry + new_sl_dist, 8)
            signal["stop_loss"] = new_sl
            logger.info(
                f"  [P3-SL] {sym} SL widened "
                f"{current_sl_dist_pct:.2f}% → {min_sl_dist_pct:.2f}% "
                f"(ATR={atr_pct:.2f}%)"
            )

    # ── 2. Leverage cap (audited range: 1-10x) ────────────────────
    # Higher leverage cohorts were materially worse; keep a hard ceiling even
    # if the AI proposes a larger number.
    mode = adaptive_params.get("mode", "normal")
    lev_mult = adaptive_params.get("max_leverage_mult", 1.0)
    lev_hard_caps = {
        "defensive": int(getattr(_Cfg, "LEVERAGE_CAP_DEFENSIVE", 5)),
        "conservative": int(getattr(_Cfg, "LEVERAGE_CAP_CONSERVATIVE", 8)),
        "normal": int(getattr(_Cfg, "LEVERAGE_CAP_NORMAL", 10)),
        "aggressive": int(getattr(_Cfg, "LEVERAGE_CAP_AGGRESSIVE", 10)),
    }
    lev_cap = lev_hard_caps.get(
        mode, int(getattr(_Cfg, "LEVERAGE_CAP_NORMAL", 10))
    )
    if str(signal.get("risk_level", "MEDIUM")).upper() == "HIGH":
        lev_cap = min(lev_cap, int(getattr(_Cfg, "LEVERAGE_CAP_HIGH_RISK", 5)))
    lev_floor = int(getattr(_Cfg, "LEVERAGE_FLOOR", 1))
    lev_floor = min(lev_floor, lev_cap)

    adjusted_lev = max(lev_floor, min(leverage, int(leverage * lev_mult), lev_cap))
    if adjusted_lev != leverage:
        signal["leverage"] = adjusted_lev
        logger.info(
            f"  [P3-LEV] {sym} leverage {leverage}x → {adjusted_lev}x "
            f"(mode={mode}, mult={lev_mult:.1f}x, floor={lev_floor}x, cap={lev_cap}x)"
        )

    # ── 3. High-conviction attribution ────────────────────────────
    # Score alone is not a reliable sizing edge; keep the attribution flag for
    # analysis, but never increase position size only because AI score is high.
    sig_score = float(signal.get("_ai_score_raw", signal.get("score", 0)) or 0)
    if _Cfg.HIGH_CONVICTION_ENABLED and sig_score >= _Cfg.HIGH_CONVICTION_SCORE:
        signal["_high_conviction"] = True   # attribution flag (read by pnl_tracker)
        logger.info(
            f"  [P3-CONV] {sym} score={sig_score:.1f} >= {_Cfg.HIGH_CONVICTION_SCORE:.1f} "
            f"-> tagged only; size unchanged until expectancy confirms edge"
        )

    # ── 4. Symbol performance penalty ────────────────────────────
    if memory and _Cfg.MEMORY_SYMBOL_SIZING_ENABLED:
        sym_stats = memory.get_symbol_stats(sym)
        if sym_stats and sym_stats.get("trades", 0) >= 3:
            t = sym_stats["trades"]
            w = sym_stats["wins"]
            sym_wr = w / t * 100 if t > 0 else 0
            sym_pnl = float(sym_stats.get("total_pnl", 0.0) or 0.0)
            if sym_pnl < 0 and sym_wr < 50:
                penalized = round(size_pct * 0.5, 2)
                signal["position_size_percent"] = penalized
                logger.info(
                    f"  [P3-SYM] {sym} size {size_pct:.1f}% → {penalized:.1f}% "
                    f"(WR={sym_wr:.0f}% < 50%, PnL={sym_pnl:+.2f} over {t} trades)"
                )

    return signal


# ── SMC / ICT retest gate ──────────────────────────────────────────────────

def passes_smc_retest_filter(signal: Dict, market_snapshot: Dict) -> bool:
    """Reject late chase entries using objective pullback/retest + SMC context."""
    from config import Config

    sym = signal.get("symbol", "?")
    action = signal.get("action", "HOLD")

    if action not in ("LONG", "SHORT"):
        return True

    mode = Config.SMC_FILTER_MODE
    if mode == "off":
        return True
    if mode not in ("soft", "strict"):
        logger.warning(f"  [SMC-CONFIG] Unknown SMC_FILTER_MODE={mode!r}; using soft")
        mode = "soft"

    rsi = float(market_snapshot.get("rsi", 50) or 50)
    atr_pct = float(market_snapshot.get("atr_pct", 1.5) or 1.5)
    ema_dist = float(market_snapshot.get("ema20_distance_pct", 0.0) or 0.0)
    premium_discount = str(market_snapshot.get("premium_discount", "equilibrium"))
    structure = str(market_snapshot.get("market_structure", "sideways"))
    volume_trend = str(market_snapshot.get("volume_trend", "flat"))
    htf_bias = str(market_snapshot.get("htf_bias", "neutral"))
    h1_ema_trend = str(market_snapshot.get("h1_ema_trend", "neutral"))
    m15_momentum = str(market_snapshot.get("m15_momentum", "neutral"))
    # Gate deterministik pakai skor AI MENTAH — whale boost (+1.5 di
    # main.py) hanya untuk ranking, tidak boleh membuka soft-bypass
    # anti-chase (filter ini lahir dari loss nyata).
    score = float(signal.get("_ai_score_raw", signal.get("score", 0.0)) or 0.0)
    conf = float(signal.get("confidence", 0.0) or 0.0)

    swept_high = bool(market_snapshot.get("swept_prev_high", False))
    swept_low = bool(market_snapshot.get("swept_prev_low", False))
    bos_bull = bool(market_snapshot.get("bos_bullish", False))
    bos_bear = bool(market_snapshot.get("bos_bearish", False))
    near_support = bool(market_snapshot.get("near_support", False))
    near_resistance = bool(market_snapshot.get("near_resistance", False))
    long_retest = bool(market_snapshot.get("long_retest_zone", False))
    short_retest = bool(market_snapshot.get("short_retest_zone", False))

    if Config.SMC_HARD_REJECT_LATE_CHASE:
        if action == "LONG":
            has_context = long_retest or swept_low or bos_bull
            wrong_location = premium_discount == "premium" or abs(ema_dist) > 3.0
        else:
            has_context = short_retest or swept_high or bos_bear
            wrong_location = premium_discount == "discount" or abs(ema_dist) > 3.0
        if wrong_location and not has_context:
            logger.warning(
                f"  [SMC-LATE-HARD] {sym} {action} rejected: "
                f"zone={premium_discount}, ema_dist={ema_dist:.2f}%, no sweep/BOS/retest"
            )
            return False

    strong_soft_signal = (
        score >= Config.SMC_SOFT_BYPASS_SCORE
        and conf >= Config.SMC_SOFT_BYPASS_CONFIDENCE
    )
    # Multi-TF agreement (htf+h1+m15) — lebih stabil dari `structure` yang
    # flip-flop tiap scan di range market. Sebelumnya `trend_aligned_*`
    # trust `structure` field yang lagging → bypass kebuka di chop.
    trend_aligned_long = (
        htf_bias == "bullish"
        and h1_ema_trend == "bullish"
        and m15_momentum != "bearish"
    )
    trend_aligned_short = (
        htf_bias == "bearish"
        and h1_ema_trend == "bearish"
        and m15_momentum != "bullish"
    )

    def _reject(tag: str, msg: str, *, hard: bool = False) -> bool:
        if mode == "soft" and not hard and strong_soft_signal:
            logger.warning(
                f"  [{tag}-SOFT] {sym} {msg} -- allow high-conviction "
                f"(score={score:.1f}, conf={conf:.2f})"
            )
            return True
        suffix = ""
        if mode == "soft" and not hard:
            suffix = (
                f" (soft bypass needs score>={Config.SMC_SOFT_BYPASS_SCORE:.1f} "
                f"and conf>={Config.SMC_SOFT_BYPASS_CONFIDENCE:.2f})"
            )
        logger.warning(f"  [{tag}] {sym} {msg} -- skip{suffix}")
        return False

    strict_extension_pct = max(atr_pct * 1.2, 2.0)
    soft_extension_pct = max(atr_pct * 2.0, 3.0)
    if ema_dist > strict_extension_pct:
        hard_extension = mode == "strict" or ema_dist > soft_extension_pct
        if not _reject(
            "SMC-LATE",
            f"{action} price {ema_dist:.2f}% from EMA20 > "
            f"{strict_extension_pct:.2f}%",
            hard=hard_extension,
        ):
            return False

    if mode == "soft":
        if action == "LONG" and structure == "breakout" and volume_trend != "rising" and not long_retest:
            if not _reject(
                "SMC-BREAKOUT",
                "LONG breakout without rising volume/retest",
                hard=True,
            ):
                return False
        if action == "SHORT" and structure == "breakdown" and volume_trend != "rising" and not short_retest:
            if not _reject(
                "SMC-BREAKDOWN",
                "SHORT breakdown without rising volume/retest",
                hard=True,
            ):
                return False

    if mode == "soft":
        if action == "LONG" and trend_aligned_long and not (rsi > 74 and not swept_low):
            return True
        if action == "SHORT" and trend_aligned_short and not (rsi < 26 and not swept_high):
            return True

    if action == "LONG":
        has_context = long_retest or swept_low or bos_bull
        if not has_context:
            if not _reject("SMC", "LONG lacks retest/sweep/BOS context"):
                return False
        if premium_discount == "premium" and not swept_low and not long_retest:
            if not _reject("SMC-PD", "LONG in premium without sweep/retest"):
                return False
        if rsi > 72 and not swept_low:
            if not _reject("SMC-LATE", f"LONG RSI={rsi:.1f} without sell-side sweep"):
                return False
        if near_resistance and not bos_bull:
            if not _reject("SMC-SR", "LONG near resistance without bullish BOS"):
                return False
        if structure == "breakout" and volume_trend != "rising" and not long_retest:
            if not _reject("SMC-BREAKOUT", "LONG breakout without rising volume/retest"):
                return False

    if action == "SHORT":
        has_context = short_retest or swept_high or bos_bear
        if not has_context:
            if not _reject("SMC", "SHORT lacks retest/sweep/BOS context"):
                return False
        if premium_discount == "discount" and not swept_high and not short_retest:
            if not _reject("SMC-PD", "SHORT in discount without sweep/retest"):
                return False
        if rsi < 28 and not swept_high:
            if not _reject("SMC-LATE", f"SHORT RSI={rsi:.1f} without buy-side sweep"):
                return False
        if near_support and not bos_bear:
            if not _reject("SMC-SR", "SHORT near support without bearish BOS"):
                return False
        if structure == "breakdown" and volume_trend != "rising" and not short_retest:
            if not _reject("SMC-BREAKDOWN", "SHORT breakdown without rising volume/retest"):
                return False

    return True
