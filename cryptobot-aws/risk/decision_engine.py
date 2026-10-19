"""
risk/decision_engine.py — deterministic pre-execution risk gate.

Two decision functions run per LONG/SHORT signal in main.py, right before
`executor.execute_signal`:

  1. apply_expectancy_filter_and_sizing(signal, market_snapshot, balance)
       Reject / resize based on the 2026-06-18 expectancy audit:
         - HIGH-risk trades are blocked (or heavily downsized) — they had
           negative expectancy historically.
         - Position size is conservatively scaled by AI score and objective
           late-chase context; optional setup multipliers default to neutral.

  2. apply_max_loss_cap(signal, balance, market_snapshot)
       Final guardrail: cap size so the worst-case loss to the stop-loss
       never exceeds MAX_LOSS_USDT_PER_TRADE.

Both mutate `signal` in place (position_size_percent) and return a
`DecisionResult`. `write_decision_audit` records every verdict as a JSON
line for later review (the "mudah diaudit" requirement).

These functions are intentionally pure (no I/O except the audit append) so
they can be unit-tested without an exchange or the AI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config
from utils.logger import logger

# Audit trail — one JSON object per line, appended as decisions are made.
_AUDIT_PATH = Path(__file__).resolve().parents[1] / "logs" / "decision_audit.jsonl"

# AI score is on a ~0-14.5 scale (see scoring.md). The expectancy audit
# grouped outcomes into bands anchored on the same scale the bot uses:
#   weak      : score < 7.5      -> historically thin edge, size down
#   medium    : 8.5 <= s < configured high threshold -> neutral by default
#   high      : score >= configured threshold         -> overconfidence downsize
_SCORE_WEAK_MAX = 7.5
_SCORE_MED_LO = 8.5


@dataclass
class DecisionResult:
    """Outcome of a single risk-stage evaluation.

    Attributes:
        allowed:        False means the caller must skip the trade.
        stage:          Which gate produced this result (for the audit log).
        symbol:         Symbol under evaluation.
        action:         LONG / SHORT.
        reason:         Human-readable explanation of the verdict.
        size_before:    position_size_percent before this stage.
        size_after:     position_size_percent after this stage.
        details:        Extra structured context for the audit trail.
    """

    allowed: bool
    stage: str
    symbol: str = "?"
    action: str = "?"
    reason: str = ""
    size_before: float = 0.0
    size_after: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "strategy_version": Config.STRATEGY_VERSION,
            "config_hash": Config.strategy_fingerprint(),
            "stage": self.stage,
            "symbol": self.symbol,
            "action": self.action,
            "allowed": self.allowed,
            "reason": self.reason,
            "size_before": round(self.size_before, 4),
            "size_after": round(self.size_after, 4),
            "details": self.details,
        }


# ── Helpers ──────────────────────────────────────────────────────────────

def _clamp_size(size_pct: float) -> float:
    """Clamp a position size to the configured [MIN, MAX] window."""
    return max(
        Config.MIN_POSITION_SIZE_PCT,
        min(size_pct, Config.MAX_POSITION_SIZE_PCT),
    )


def _sl_distance_pct(signal: Dict, market_snapshot: Dict) -> float:
    """Return the |entry - SL| distance as a fraction of entry price.

    Falls back to the live market price when the AI omitted an entry, and to
    MIN_SL_DISTANCE_PCT when no stop-loss is present, so callers always get a
    positive, sane number to size against.
    """
    entry = float(signal.get("entry_price", 0) or 0)
    if entry <= 0:
        entry = float(market_snapshot.get("current_price", 0) or 0)
    sl = float(signal.get("stop_loss", 0) or 0)
    if entry <= 0 or sl <= 0:
        return Config.MIN_SL_DISTANCE_PCT / 100.0
    dist = abs(entry - sl) / entry
    # Never treat SL distance as below the configured floor.
    return max(dist, Config.MIN_SL_DISTANCE_PCT / 100.0)


def _is_late_chase(action: str, market_snapshot: Dict) -> bool:
    """Return True for an objectively extended entry without SMC context."""
    action = action.upper()
    ema_dist = abs(float(market_snapshot.get("ema20_distance_pct", 0.0) or 0.0))
    zone = str(market_snapshot.get("premium_discount", "equilibrium")).lower()

    if action == "LONG":
        has_context = any((
            market_snapshot.get("swept_prev_low", False),
            market_snapshot.get("bos_bullish", False),
            market_snapshot.get("long_retest_zone", False),
        ))
        return not has_context and (zone == "premium" or ema_dist > 3.0)
    if action == "SHORT":
        has_context = any((
            market_snapshot.get("swept_prev_high", False),
            market_snapshot.get("bos_bearish", False),
            market_snapshot.get("short_retest_zone", False),
        ))
        return not has_context and (zone == "discount" or ema_dist > 3.0)
    return False


def calculate_weighted_rr(signal: Dict) -> tuple[float, float, float, List[float]]:
    """Return weighted reward/risk using the tracker's sequential TP exits.

    TP1 is a fraction of the initial position; TP2 is configured as a fraction
    of what remains after TP1; TP3 receives the runner.  This mirrors
    ``PnLTracker.partial_close_record`` and avoids treating TP2 as another
    initial-position fraction.
    """
    entry = float(signal.get("entry_price", 0.0) or 0.0)
    stop = float(signal.get("stop_loss", 0.0) or 0.0)
    action = str(signal.get("action", "")).upper()
    tps = [
        float(value) for value in signal.get("take_profit", [])[:3]
        if isinstance(value, (int, float)) and float(value) > 0
    ]
    if entry <= 0 or stop <= 0 or not tps or action not in {"LONG", "SHORT", "BUY", "SELL"}:
        return 0.0, 0.0, 0.0, []

    tp1_weight = max(0.0, min(Config.TP1_CLOSE_PCT / 100.0, 1.0))
    tp2_of_remaining = max(0.0, min(Config.TP2_CLOSE_PCT / 100.0, 1.0))
    tp2_weight = (1.0 - tp1_weight) * tp2_of_remaining
    weights = [tp1_weight, tp2_weight, max(0.0, 1.0 - tp1_weight - tp2_weight)]

    # If a legacy signal has fewer targets, assume its final provided target
    # closes the remaining allocation instead of silently dropping exposure.
    if len(tps) == 1:
        weights = [1.0]
    elif len(tps) == 2:
        weights = [tp1_weight, 1.0 - tp1_weight]
    else:
        weights = weights[: len(tps)]

    is_long = action in {"LONG", "BUY"}
    risk = (entry - stop) if is_long else (stop - entry)
    if risk <= 0:
        return 0.0, 0.0, risk, weights

    reward = 0.0
    for target, weight in zip(tps, weights):
        target_reward = (target - entry) if is_long else (entry - target)
        reward += max(0.0, target_reward) * weight
    return reward / risk, reward, risk, weights


def calculate_admission_rr(signal: Dict) -> Optional[Dict]:
    """Cost-adjusted admission RR plus a truncated-runner RR.

    Audit 2026-07-26: the gross weighted RR was optimistic on both sides —
    it ignored the ~0.15% round-trip cost that recorded PnL pays, and it
    credited the 64.6% runner weight at TP3, a target July realized only
    ~11% of the time (the runner actually exits at BE/trail far more often).

    Returns None when the plan is invalid (mirrors calculate_weighted_rr).
      rr_net      — weighted RR, round-trip cost subtracted from every leg's
                    reward and added to the risk side.
      rr_tp2_net  — same, but the runner leg is valued at TP2 (the target that
                    is realistically reached). Gate with Config.RR_TP2_FLOOR.
    """
    entry = float(signal.get("entry_price", 0.0) or 0.0)
    action = str(signal.get("action", "")).upper()
    rr, reward, risk, weights = calculate_weighted_rr(signal)
    if entry <= 0 or risk <= 0 or not weights:
        return None
    tps = [
        float(value) for value in signal.get("take_profit", [])[:3]
        if isinstance(value, (int, float)) and float(value) > 0
    ]
    is_long = action in {"LONG", "BUY"}
    cost = entry * 2.0 * (
        max(float(Config.TAKER_FEE_PCT), 0.0)
        + max(float(Config.DRY_RUN_SLIPPAGE_PCT), 0.0)
    ) / 100.0

    def _net_rr(targets: List[float]) -> float:
        net_reward = 0.0
        for target, weight in zip(targets, weights):
            move = (target - entry) if is_long else (entry - target)
            net_reward += max(0.0, move - cost) * weight
        return net_reward / (risk + cost)

    rr_net = _net_rr(tps)
    # Runner dinilai di TP2, bukan TP3 — TP3 jarang tercapai.
    truncated = tps if len(tps) < 3 else [tps[0], tps[1], tps[1]]
    rr_tp2_net = _net_rr(truncated)
    return {
        "rr_gross": rr,
        "rr_net": rr_net,
        "rr_tp2_net": rr_tp2_net,
        "risk": risk,
        "cost": cost,
        "weights": weights,
    }


def materialize_effective_stop(signal: Dict, market_snapshot: Dict) -> Dict:
    """Put the stop that the executor will actually use onto the signal.

    This keeps RR, max-loss sizing, dry-run tracking, and live execution on the
    same stop.  ATR mode already uses the post-adaptive signal stop; only the
    legacy CEX ROI mode needs deterministic materialisation here.
    """
    if Config.EXCHANGE_MODE != "CEX" or Config.SL_MODE != "roi":
        return signal

    from execution.cex_executor import CEXExecutor

    action = str(signal.get("action", "")).upper()
    if action == "BUY":
        action = "LONG"
    elif action == "SELL":
        action = "SHORT"
    entry = float(signal.get("entry_price", 0.0) or 0.0)
    leverage = max(float(signal.get("leverage", 1.0) or 1.0), 1.0)
    if action not in {"LONG", "SHORT"} or entry <= 0:
        return signal

    signal["stop_loss"] = CEXExecutor._calc_roi_sl(
        action,
        entry,
        leverage,
        price_dp=8,
        max_price_move_pct=Config.SL_MAX_PRICE_MOVE_PCT,
        atr_pct=float(market_snapshot.get("atr_pct", 0.0) or 0.0),
        mode=str(market_snapshot.get("trading_mode") or Config.TRADING_MODE),
    )
    signal["_effective_sl_mode"] = "roi"
    return signal


# ── Stage 1: expectancy filter + edge-based sizing ─────────────────────────

def apply_expectancy_filter_and_sizing(
    signal: Dict,
    market_snapshot: Dict,
    account_balance: float,
) -> DecisionResult:
    """Reject or resize a signal using the 2026-06-18 expectancy audit rules.

    Rules (all configurable via .env, all no-ops when the multiplier is 1.0):
      - risk_level == HIGH:
          BLOCK_HIGH_RISK_TRADES -> reject outright,
          else multiply size by HIGH_RISK_SIZE_MULT.
      - score bands: weak (<7.5), medium (8.5-high threshold), and high
        each scale size by their respective multiplier.
      - setup multipliers remain configurable but default to 1.0.
      - objectively extended entries without sweep/BOS/retest are downsized.

    The signal's position_size_percent is updated in place and re-clamped to
    [MIN_POSITION_SIZE_PCT, MAX_POSITION_SIZE_PCT].
    """
    sym = signal.get("symbol", "?")
    action = str(signal.get("action", "?")).upper()
    size_before = float(signal.get("position_size_percent", Config.MIN_POSITION_SIZE_PCT))
    size = size_before

    if not Config.EXPECTANCY_FILTER_ENABLED:
        return DecisionResult(
            allowed=True, stage="expectancy", symbol=sym, action=action,
            reason="expectancy filter disabled",
            size_before=size_before, size_after=size_before,
        )

    risk_level = str(signal.get("risk_level", "MEDIUM")).upper()
    score = float(signal.get("score", 0) or 0)
    structure = str(market_snapshot.get("market_structure", "sideways")).lower()

    applied: List[str] = []

    # ── HIGH-risk handling ──────────────────────────────────────────────
    if risk_level == "HIGH":
        if Config.BLOCK_HIGH_RISK_TRADES:
            return DecisionResult(
                allowed=False, stage="expectancy", symbol=sym, action=action,
                reason="risk_level=HIGH blocked (negative historical expectancy)",
                size_before=size_before, size_after=0.0,
                details={"risk_level": risk_level, "score": score},
            )
        size *= Config.HIGH_RISK_SIZE_MULT
        applied.append(f"high_risk×{Config.HIGH_RISK_SIZE_MULT}")

    # ── Score-band sizing ───────────────────────────────────────────────
    if score < _SCORE_WEAK_MAX:
        size *= Config.SCORE_LT_75_SIZE_MULT
        applied.append(f"score<{_SCORE_WEAK_MAX}×{Config.SCORE_LT_75_SIZE_MULT}")
    elif _SCORE_MED_LO <= score < Config.SCORE_HIGH_THRESHOLD:
        size *= Config.SCORE_85_95_MEDIUM_SIZE_MULT
        applied.append(
            f"score[{_SCORE_MED_LO},{Config.SCORE_HIGH_THRESHOLD})"
            f"×{Config.SCORE_85_95_MEDIUM_SIZE_MULT}"
        )
    elif score >= Config.SCORE_HIGH_THRESHOLD:
        size *= Config.SCORE_HIGH_SIZE_MULT
        applied.append(
            f"score>={Config.SCORE_HIGH_THRESHOLD}×{Config.SCORE_HIGH_SIZE_MULT}"
        )

    # ── Setup-vs-structure edge ─────────────────────────────────────────
    if action == "SHORT" and "down" in structure:
        size *= Config.SHORT_DOWNTREND_SIZE_MULT
        applied.append(f"short_downtrend×{Config.SHORT_DOWNTREND_SIZE_MULT}")
    elif action == "LONG" and structure == "sideways":
        size *= Config.LONG_SIDEWAYS_SIZE_MULT
        applied.append(f"long_sideways×{Config.LONG_SIDEWAYS_SIZE_MULT}")

    late_chase = _is_late_chase(action, market_snapshot)
    if late_chase:
        size *= Config.LATE_CHASE_SIZE_MULT
        applied.append(f"late_chase×{Config.LATE_CHASE_SIZE_MULT}")

    size_after = _clamp_size(size)
    signal["position_size_percent"] = round(size_after, 4)

    if applied:
        logger.info(
            f"  [EXPECTANCY] {sym} {action} size "
            f"{size_before:.2f}% -> {size_after:.2f}%  ({', '.join(applied) or 'no change'})"
        )

    return DecisionResult(
        allowed=True, stage="expectancy", symbol=sym, action=action,
        reason="; ".join(applied) or "no adjustment",
        size_before=size_before, size_after=size_after,
        details={
            "risk_level": risk_level,
            "score": score,
            "structure": structure,
            "late_chase": late_chase,
        },
    )


# ── Stage 2: absolute max-loss cap ─────────────────────────────────────────

def apply_max_loss_cap(
    signal: Dict,
    account_balance: float,
    market_snapshot: Dict,
) -> DecisionResult:
    """Cap position size so worst-case loss to SL <= MAX_LOSS_USDT_PER_TRADE.

    Worst-case loss (USDT) = notional * sl_distance_fraction, where
    notional = balance * size_fraction * leverage. We solve for the size
    fraction that keeps that product at or below the configured cap and, if
    the signal's requested size exceeds it, shrink to the cap.

    If even the minimum allowed size would breach the cap, the trade is
    rejected (the SL is simply too far for this risk budget).
    """
    sym = signal.get("symbol", "?")
    action = str(signal.get("action", "?")).upper()
    size_before = float(signal.get("position_size_percent", Config.MIN_POSITION_SIZE_PCT))

    max_loss = float(Config.MAX_LOSS_USDT_PER_TRADE)
    equity_risk_pct = max(float(Config.MAX_RISK_PCT_PER_TRADE), 0.0)
    if equity_risk_pct > 0 and account_balance > 0:
        equity_cap = account_balance * equity_risk_pct / 100.0
        max_loss = min(max_loss, equity_cap) if max_loss > 0 else equity_cap
    if max_loss <= 0 or account_balance <= 0:
        return DecisionResult(
            allowed=True, stage="max_loss_cap", symbol=sym, action=action,
            reason="max-loss cap disabled",
            size_before=size_before, size_after=size_before,
        )

    leverage = max(float(signal.get("leverage", 1) or 1), 1.0)
    sl_frac = _sl_distance_pct(signal, market_snapshot)

    # Worst-case loss at the requested size.
    notional = account_balance * (size_before / 100.0) * leverage
    # Include two taker fills plus conservative slippage in the risk budget.
    # Config percentages use price-percent units (0.055 == 5.5 bps).
    round_trip_cost_frac = 2.0 * (
        max(float(Config.TAKER_FEE_PCT), 0.0)
        + max(float(Config.DRY_RUN_SLIPPAGE_PCT), 0.0)
    ) / 100.0
    total_loss_frac = sl_frac + round_trip_cost_frac
    worst_loss = notional * total_loss_frac

    if worst_loss <= max_loss:
        return DecisionResult(
            allowed=True, stage="max_loss_cap", symbol=sym, action=action,
            reason=f"worst-case ${worst_loss:.2f} <= cap ${max_loss:.2f}",
            size_before=size_before, size_after=size_before,
            details={"worst_loss": round(worst_loss, 4), "leverage": leverage,
                     "sl_frac": round(sl_frac, 6),
                     "round_trip_cost_frac": round(round_trip_cost_frac, 6)},
        )

    # Solve for the size % that hits exactly the cap.
    #   max_loss = balance * (size/100) * lev * sl_frac
    capped_size = (max_loss / (account_balance * leverage * total_loss_frac)) * 100.0
    capped_size = round(capped_size, 4)

    if capped_size < Config.MIN_POSITION_SIZE_PCT:
        return DecisionResult(
            allowed=False, stage="max_loss_cap", symbol=sym, action=action,
            reason=(
                f"SL too far: min size {Config.MIN_POSITION_SIZE_PCT}% would still "
                f"risk > ${max_loss:.2f} (sl_dist={sl_frac*100:.2f}%, "
                f"cost={round_trip_cost_frac*100:.2f}%, lev={leverage:.0f}x)"
            ),
            size_before=size_before, size_after=0.0,
            details={"worst_loss": round(worst_loss, 4), "capped_size": capped_size},
        )

    signal["position_size_percent"] = capped_size
    logger.info(
        f"  [MAX-LOSS] {sym} {action} size {size_before:.2f}% -> {capped_size:.2f}% "
        f"(cap worst-case loss at ${max_loss:.2f}, was ${worst_loss:.2f})"
    )
    return DecisionResult(
        allowed=True, stage="max_loss_cap", symbol=sym, action=action,
        reason=f"size capped to keep worst-case loss <= ${max_loss:.2f}",
        size_before=size_before, size_after=capped_size,
        details={"worst_loss_before": round(worst_loss, 4), "leverage": leverage,
                 "sl_frac": round(sl_frac, 6),
                 "round_trip_cost_frac": round(round_trip_cost_frac, 6)},
    )


# ── Audit trail ────────────────────────────────────────────────────────────

def write_decision_audit(decision: DecisionResult, signal: Optional[Dict] = None) -> None:
    """Append a decision to logs/decision_audit.jsonl (best-effort).

    Never raises: auditing must not break the trading loop. A failure to write
    the audit line is logged and swallowed.
    """
    try:
        record = decision.to_dict()
        if signal is not None:
            record["signal"] = {
                "entry_price": signal.get("entry_price"),
                "stop_loss": signal.get("stop_loss"),
                "leverage": signal.get("leverage"),
                "score": signal.get("score"),
                "confidence": signal.get("confidence"),
                "risk_level": signal.get("risk_level"),
                "position_size_percent": signal.get("position_size_percent"),
            }
        _AUDIT_PATH.parent.mkdir(exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — audit must never break trading
        logger.debug(f"[AUDIT] failed to write decision audit: {exc}")
