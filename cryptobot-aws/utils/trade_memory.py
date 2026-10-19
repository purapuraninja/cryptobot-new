"""
utils/trade_memory.py — Trade Memory & AI Learning System.

Stores trade outcomes, market conditions at entry/exit, and
generates learning context for AI signal analysis.

Features:
  - Record every trade with full context (market conditions, AI signal, outcome)
  - Compute per-symbol, per-setup, per-condition performance stats
  - Generate compact learning summary for AI system prompt injection
  - Adaptive rules: win streaks → aggressive, losing streaks → conservative
  - Weekly/daily performance tracking

Data stored in: trade_memory.json (auto-created in project root)
"""

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

from utils.logger import logger

_MEMORY_PATH = Path("trade_memory.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_str() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _use_sqlite() -> bool:
    """True when Config.STORAGE_BACKEND == 'sqlite' (lazy import)."""
    try:
        from config import Config
        return Config.STORAGE_BACKEND == "sqlite"
    except Exception:
        return False


def _empty_memory() -> Dict:
    return {
        "trades": [],
        "symbol_stats": {},
        "setup_stats": {},
        "daily_pnl": {},
        "streak": {"current": 0, "type": "none"},  # +N = win streak, -N = loss streak
        "version": 2,
    }


def _load_memory() -> Dict:
    if _use_sqlite():
        try:
            from storage import sqlite_backend
            return sqlite_backend.load_memory()
        except Exception as exc:
            raise RuntimeError(f"[Memory] SQLite load failed: {exc}") from exc
    if _MEMORY_PATH.exists():
        try:
            return json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _empty_memory()


def _save_memory(mem: Dict) -> None:
    if _use_sqlite():
        try:
            from storage import sqlite_backend
            sqlite_backend.save_memory(mem)
            return
        except Exception as exc:
            raise RuntimeError(f"[Memory] SQLite save failed: {exc}") from exc
    try:
        _MEMORY_PATH.write_text(json.dumps(mem, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[Memory] Could not save: {exc}")


def _entry_quality(action: str, market: Dict) -> str:
    """Classify whether the entry respected sweep/BOS/retest workflow."""
    action = (action or "").upper()
    premium_discount = market.get("premium_discount", "equilibrium")
    ema_dist = float(market.get("ema20_distance_pct", 0.0) or 0.0)

    if action == "LONG":
        if market.get("swept_prev_low"):
            return "sweep_reclaim"
        if market.get("bos_bullish"):
            return "bullish_bos"
        if market.get("long_retest_zone"):
            return "valid_retest"
        if premium_discount == "premium" or ema_dist > 3.0:
            return "late_chase"
    elif action == "SHORT":
        if market.get("swept_prev_high"):
            return "sweep_reject"
        if market.get("bos_bearish"):
            return "bearish_bos"
        if market.get("short_retest_zone"):
            return "valid_retest"
        if premium_discount == "discount" or ema_dist > 3.0:
            return "late_chase"

    return "neutral"


class TradeMemory:
    """
    AI Learning Memory — records trades, computes statistics,
    generates learning context for AI prompt injection.
    """

    def __init__(self) -> None:
        self._mem = _load_memory()

    # ── Record trade entry context ────────────────────────────────

    def record_entry_context(
        self,
        symbol: str,
        action: str,
        signal: Dict,
        market_snapshot: Dict,
    ) -> None:
        """Store market conditions at entry time for later analysis."""
        entry_quality = _entry_quality(action, market_snapshot)
        try:
            from config import Config
            strategy_version = Config.STRATEGY_VERSION
            config_hash = Config.strategy_fingerprint()
        except Exception:
            strategy_version = "unknown"
            config_hash = "unknown"
        setup_type = signal.get("setup_type") or (
            f"{market_snapshot.get('market_structure', '?')}_"
            f"{market_snapshot.get('htf_bias', '?')}_{action}_{entry_quality}"
        )
        smc_context = {
            "swept_prev_high": market_snapshot.get("swept_prev_high", False),
            "swept_prev_low": market_snapshot.get("swept_prev_low", False),
            "bos_bullish": market_snapshot.get("bos_bullish", False),
            "bos_bearish": market_snapshot.get("bos_bearish", False),
            "premium_discount": market_snapshot.get("premium_discount", "equilibrium"),
            "near_support": market_snapshot.get("near_support", False),
            "near_resistance": market_snapshot.get("near_resistance", False),
            "near_ema20": market_snapshot.get("near_ema20", False),
            "ema20_distance_pct": market_snapshot.get("ema20_distance_pct", 0),
        }
        entry_ctx = {
            "symbol": symbol,
            "position_id": signal.get("_position_id", ""),
            "action": action,
            "entry_time": _now_str(),
            "entry_price": float(signal.get("entry_price", 0)),
            "stop_loss": float(signal.get("stop_loss", 0)),
            "take_profit": signal.get("take_profit", []),
            "leverage": int(signal.get("leverage", 1)),
            "ai_score": float(signal.get("score", 0)),
            "ai_confidence": float(signal.get("confidence", 0)),
            "risk_level": signal.get("risk_level", ""),
            "reason": signal.get("reason", ""),
            "setup_type": setup_type,
            "entry_quality": entry_quality,
            "strategy_version": strategy_version,
            "config_hash": config_hash,
            "smc": smc_context,
            # Market conditions at entry
            "market": {
                "rsi": market_snapshot.get("rsi", 0),
                "structure": market_snapshot.get("market_structure", ""),
                "htf_bias": market_snapshot.get("htf_bias", ""),
                "volume": market_snapshot.get("volume_condition", ""),
                "volume_trend": market_snapshot.get("volume_trend", ""),
                "atr_pct": market_snapshot.get("atr_pct", 0),
                "funding": market_snapshot.get("funding_rate", 0),
                "oi_condition": market_snapshot.get("open_interest_condition", ""),
                "entry_quality": entry_quality,
                "premium_discount": market_snapshot.get("premium_discount", "equilibrium"),
            },
        }
        # Store temporarily until trade closes
        self._mem.setdefault("pending_entries", {})[symbol] = entry_ctx
        _save_memory(self._mem)

    # ── Record trade close result ─────────────────────────────────

    def record_trade_result(
        self,
        symbol: str,
        exit_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        reason: str,
        duration_mins: int = 0,
        gross_pnl_usdt: Optional[float] = None,
        fees_usdt: float = 0.0,
    ) -> None:
        """Record completed trade with full context for learning."""
        pending = self._mem.get("pending_entries", {}).pop(symbol, {})
        exit_time = _now_str()
        if duration_mins <= 0 and pending.get("entry_time"):
            try:
                opened = datetime.strptime(
                    pending["entry_time"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                closed = datetime.strptime(
                    exit_time, "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                duration_mins = max(0, int((closed - opened).total_seconds() / 60))
            except (TypeError, ValueError):
                duration_mins = 0

        trade = {
            "symbol": symbol,
            "position_id": pending.get("position_id", ""),
            "action": pending.get("action", "?"),
            "entry_price": pending.get("entry_price", 0),
            "exit_price": exit_price,
            "pnl_usdt": round(pnl_usdt, 4),
            "gross_pnl_usdt": round(
                pnl_usdt if gross_pnl_usdt is None else gross_pnl_usdt, 4
            ),
            "fees_usdt": round(fees_usdt, 4),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,  # stop_loss, take_profit, trailing_stop, etc
            "ai_score": pending.get("ai_score", 0),
            "ai_confidence": pending.get("ai_confidence", 0),
            "risk_level": pending.get("risk_level", ""),
            "leverage": pending.get("leverage", 1),
            "setup_type": pending.get("setup_type", ""),
            "entry_quality": pending.get("entry_quality", ""),
            "smc": pending.get("smc", {}),
            "market": pending.get("market", {}),
            "entry_time": pending.get("entry_time", ""),
            "exit_time": exit_time,
            "duration_mins": duration_mins,
            "strategy_version": pending.get("strategy_version", "legacy"),
            "config_hash": pending.get("config_hash", "legacy"),
        }

        self._mem["trades"].append(trade)
        self._update_symbol_stats(trade)
        self._update_setup_stats(trade)
        self._update_daily_pnl(trade)
        self._update_streak(trade)

        _save_memory(self._mem)
        logger.info(
            f"[Memory] Trade recorded: {symbol} {trade['action']} "
            f"PnL={pnl_usdt:+.4f} score={trade['ai_score']:.1f} "
            f"streak={self._mem['streak']['current']:+d}"
        )

    # ── Stats updaters ────────────────────────────────────────────

    def _update_symbol_stats(self, trade: Dict) -> None:
        sym = trade["symbol"]
        stats = self._mem["symbol_stats"].setdefault(sym, {
            "trades": 0, "wins": 0, "losses": 0, "flats": 0,
            "total_pnl": 0, "avg_score": 0, "scores_sum": 0,
            "best_pnl": 0, "worst_pnl": 0,
            "long_wins": 0, "long_total": 0,
            "short_wins": 0, "short_total": 0,
        })
        stats["trades"] += 1
        stats.setdefault("flats", 0)
        stats["total_pnl"] = round(stats["total_pnl"] + trade["pnl_usdt"], 4)
        stats["scores_sum"] += trade["ai_score"]
        stats["avg_score"] = round(stats["scores_sum"] / stats["trades"], 2)

        is_win = trade["pnl_usdt"] > 0
        if is_win:
            stats["wins"] += 1
            if trade["pnl_usdt"] > stats["best_pnl"]:
                stats["best_pnl"] = trade["pnl_usdt"]
        elif trade["pnl_usdt"] < 0:
            stats["losses"] += 1
            if trade["pnl_usdt"] < stats["worst_pnl"]:
                stats["worst_pnl"] = trade["pnl_usdt"]
        else:
            stats["flats"] += 1

        if trade["action"] == "LONG":
            stats["long_total"] += 1
            if is_win:
                stats["long_wins"] += 1
        elif trade["action"] == "SHORT":
            stats["short_total"] += 1
            if is_win:
                stats["short_wins"] += 1

    def _update_setup_stats(self, trade: Dict) -> None:
        """Track performance by setup type (structure + htf_bias + action)."""
        mkt = trade.get("market", {})
        key = trade.get("setup_type") or (
            f"{mkt.get('structure', '?')}_{mkt.get('htf_bias', '?')}_"
            f"{trade['action']}"
        )

        stats = self._mem["setup_stats"].setdefault(key, {
            "trades": 0, "wins": 0, "flats": 0, "total_pnl": 0, "avg_pnl": 0,
        })
        stats["trades"] += 1
        stats.setdefault("flats", 0)
        is_win = trade["pnl_usdt"] > 0
        if is_win:
            stats["wins"] += 1
        elif trade["pnl_usdt"] == 0:
            stats["flats"] += 1
        stats["total_pnl"] = round(stats["total_pnl"] + trade["pnl_usdt"], 4)
        stats["avg_pnl"] = round(stats["total_pnl"] / stats["trades"], 4)

    def _update_daily_pnl(self, trade: Dict) -> None:
        today = _now().strftime("%Y-%m-%d")
        day = self._mem["daily_pnl"].setdefault(today, {"pnl": 0, "trades": 0, "wins": 0})
        day["pnl"] = round(day["pnl"] + trade["pnl_usdt"], 4)
        day["trades"] += 1
        if trade["pnl_usdt"] > 0:
            day["wins"] += 1

    def _update_streak(self, trade: Dict) -> None:
        streak = self._mem["streak"]
        if trade["pnl_usdt"] > 0:
            if streak["type"] == "win":
                streak["current"] += 1
            else:
                streak["current"] = 1
                streak["type"] = "win"
        elif trade["pnl_usdt"] < 0:
            if streak["type"] == "loss":
                streak["current"] -= 1
            else:
                streak["current"] = -1
                streak["type"] = "loss"
        else:
            streak["current"] = 0
            streak["type"] = "none"

    # ── Adaptive Rules ────────────────────────────────────────────

    def get_adaptive_params(self, account_balance: float) -> Dict:
        """
        Return adaptive trading parameters based on memory:
        - risk_multiplier: scale position size (0.5x to 1.3x)
        - min_score: minimum AI score to accept trades (5.0 to 7.0)
        - max_leverage_mult: leverage multiplier (0.6x to 1.0x)
        - mode: "aggressive" / "normal" / "conservative" / "defensive"
        """
        trades = self._mem["trades"]
        streak = self._mem["streak"]["current"]

        # Default params
        params = {
            "risk_multiplier": 1.0,
            "min_score": 5.5,
            "max_leverage_mult": 1.0,
            "mode": "normal",
            "reason": "",
        }

        if len(trades) < 3:
            params["reason"] = "insufficient_data"
            return params

        # Recent performance (last 10 trades)
        recent = trades[-10:]
        recent_pnl = sum(t["pnl_usdt"] for t in recent)
        recent_wins = sum(1 for t in recent if t["pnl_usdt"] > 0)
        recent_wr = recent_wins / len(recent) * 100

        # Weekly PnL
        week_ago = (_now() - timedelta(days=7)).strftime("%Y-%m-%d")
        weekly_pnl = sum(
            d["pnl"] for date, d in self._mem["daily_pnl"].items()
            if date >= week_ago
        )

        # === Adaptive logic ===

        # Defensive: 5+ consecutive losses OR weekly drawdown > 25%
        # (Small accounts ~$150 naturally have 15-20% weekly swings from normal
        # SL hits. 25% = genuine crisis, not normal variance.)
        if streak <= -5 or (weekly_pnl < 0 and abs(weekly_pnl) > account_balance * 0.25):
            params["risk_multiplier"] = 0.5
            params["min_score"] = 7.0
            params["max_leverage_mult"] = 0.6
            params["mode"] = "defensive"
            params["reason"] = f"streak={streak} weekly_pnl={weekly_pnl:+.2f}"
            return params

        # Conservative: 3 consecutive losses OR recent WR < 35%
        if streak <= -3 or recent_wr < 35:
            params["risk_multiplier"] = 0.7
            params["min_score"] = 6.5
            params["max_leverage_mult"] = 0.8
            params["mode"] = "conservative"
            params["reason"] = f"streak={streak} wr={recent_wr:.0f}%"
            return params

        # Aggressive: 3+ consecutive wins AND recent WR > 65% AND weekly PnL > 0
        if streak >= 3 and recent_wr > 65 and weekly_pnl > 0:
            params["risk_multiplier"] = 1.3
            params["min_score"] = 5.5
            params["max_leverage_mult"] = 1.0
            params["mode"] = "aggressive"
            params["reason"] = f"streak=+{streak} wr={recent_wr:.0f}% weekly=+{weekly_pnl:.2f}"
            return params

        # Normal
        params["reason"] = f"streak={streak:+d} wr={recent_wr:.0f}%"
        return params

    # ── AI Learning Context ───────────────────────────────────────

    def generate_learning_context(self, symbols: List[str] = None) -> str:
        """
        Generate compact learning summary for AI prompt injection.
        Adds ~200-400 tokens to the system prompt.
        """
        # MCP/Telegram monitoring agents can append strategy feedback while the
        # bot is running. Reload here so the next scan sees those notes.
        self._mem = _load_memory()
        trades = self._mem["trades"]
        if len(trades) < 2:
            return ""

        lines = []

        # Overall stats
        total = len(trades)
        wins = sum(1 for t in trades if t["pnl_usdt"] >= 0)
        total_pnl = sum(t["pnl_usdt"] for t in trades)
        wr = wins / total * 100 if total > 0 else 0
        lines.append(f"HISTORY: {total} trades, WR={wr:.0f}%, PnL={total_pnl:+.1f}U")

        # Streak + mode
        adaptive = self.get_adaptive_params(10000)  # balance doesn't matter for mode
        lines.append(f"MODE: {adaptive['mode']} (streak={self._mem['streak']['current']:+d})")

        # Reviewer signal: repeated high-score losses mean the model is overconfident.
        recent_review = trades[-20:]
        high_score_losses = [
            t for t in recent_review
            if t.get("pnl_usdt", 0) < 0 and float(t.get("ai_score", 0) or 0) >= 8.0
        ]
        if len(high_score_losses) >= 2:
            loss_symbols = ",".join(t.get("symbol", "?") for t in high_score_losses[-5:])
            lines.append(
                f"REVIEW: {len(high_score_losses)} high-score losses recently "
                f"({loss_symbols}); distrust score without memory+SMC+risk agreement"
            )

        # Top performing symbols (3+ trades, sorted by WR)
        sym_stats = self._mem["symbol_stats"]
        good_syms = []
        bad_syms = []
        for sym, s in sym_stats.items():
            if s["trades"] < 2:
                continue
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            if wr >= 60 and s["total_pnl"] > 0:
                good_syms.append((sym, wr, s["total_pnl"], s["trades"]))
            elif wr < 40 and s["total_pnl"] < 0:
                bad_syms.append((sym, wr, s["total_pnl"], s["trades"]))

        if good_syms:
            good_syms.sort(key=lambda x: -x[1])
            top = good_syms[:3]
            lines.append("STRONG: " + ", ".join(
                f"{s}({wr:.0f}%WR,{pnl:+.1f}U,{n}t)" for s, wr, pnl, n in top
            ))

        if bad_syms:
            bad_syms.sort(key=lambda x: x[1])
            bottom = bad_syms[:3]
            lines.append("WEAK: " + ", ".join(
                f"{s}({wr:.0f}%WR,{pnl:+.1f}U,{n}t)" for s, wr, pnl, n in bottom
            ))

        # Entry quality performance (agentic reviewer: early/valid/late behavior)
        quality_stats = {}
        for t in trades:
            quality = t.get("entry_quality") or t.get("market", {}).get("entry_quality")
            if not quality:
                continue
            q = quality_stats.setdefault(quality, {"trades": 0, "pnl": 0.0, "wins": 0})
            q["trades"] += 1
            q["pnl"] += float(t.get("pnl_usdt", 0) or 0)
            if t.get("pnl_usdt", 0) >= 0:
                q["wins"] += 1
        weak_quality = []
        for quality, q in quality_stats.items():
            if q["trades"] >= 3 and q["pnl"] < 0:
                wr = q["wins"] / q["trades"] * 100
                weak_quality.append((quality, wr, q["pnl"], q["trades"]))
        if weak_quality:
            weak_quality.sort(key=lambda x: x[2])
            lines.append("BAD_ENTRY_QUALITY: " + ", ".join(
                f"{q}({wr:.0f}%WR,{pnl:+.1f}U,{n}t)"
                for q, wr, pnl, n in weak_quality[:3]
            ))

        # Setup performance (which setups work)
        setup_stats = self._mem["setup_stats"]
        good_setups = []
        bad_setups = []
        for setup, s in setup_stats.items():
            if s["trades"] < 3:
                continue
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            pnl = float(s.get("total_pnl", 0.0) or 0.0)
            # GOOD_SETUP wajib profit total > 0, bukan WR saja — WR-only
            # mengajari AI memaksimalkan winrate (partial TP kecil) walau
            # expectancy negatif. Simetris dengan logika symbol-level.
            if wr >= 65 and pnl > 0:
                good_setups.append((setup, wr, pnl, s["trades"]))
            elif wr < 35 or (s["trades"] >= 5 and pnl < 0):
                bad_setups.append((setup, wr, pnl, s["trades"]))

        if good_setups:
            good_setups.sort(key=lambda x: -x[2])
            lines.append("GOOD_SETUP: " + ", ".join(
                f"{s}({wr:.0f}%,{pnl:+.1f}U,{n}t)" for s, wr, pnl, n in good_setups[:3]
            ))

        if bad_setups:
            bad_setups.sort(key=lambda x: x[2])
            lines.append("BAD_SETUP: " + ", ".join(
                f"{s}({wr:.0f}%,{pnl:+.1f}U,{n}t)" for s, wr, pnl, n in bad_setups[:3]
            ))

        # Strategy feedback submitted by MCP monitoring agents. These are not
        # commands; the signal LLM should treat them as reviewer cautions.
        feedback_items = [
            item for item in self._mem.get("strategy_feedback", [])
            if item.get("status", "active") in ("active", "pending_review")
        ]
        if feedback_items:
            active_notes = []
            for item in feedback_items[-5:]:
                fid = item.get("id", "unknown")
                symbol = item.get("symbol") or "ALL"
                severity = str(item.get("severity", "medium")).upper()
                status = str(item.get("status", "active")).upper()
                note = str(item.get("feedback", "")).strip().replace("\n", " ")
                if len(note) > 160:
                    note = note[:157] + "..."
                active_notes.append(f"{status}:{severity}:{symbol}:{note}")
            if active_notes:
                lines.append("MCP_REVIEW_FEEDBACK: " + " | ".join(active_notes))

        pending_reviews = [
            item for item in self._mem.get("strategy_feedback", [])
            if item.get("status") == "pending_review"
            or (item.get("status") == "active" and not item.get("bot_review"))
        ]
        if pending_reviews:
            review_notes = []
            for item in pending_reviews[-5:]:
                symbol = item.get("symbol") or "ALL"
                note = str(item.get("feedback", "")).strip().replace("\n", " ")
                if len(note) > 180:
                    note = note[:177] + "..."
                review_notes.append(
                    f"id={item.get('id')} symbol={symbol} severity={item.get('severity','medium')} note={note}"
                )
            if review_notes:
                lines.append(
                    "MCP_FEEDBACK_PENDING: Review these monitoring-agent notes. "
                    "Return top-level feedback_reviews/fr with decision "
                    "accepted|rejected|watch and applied_as caution|hard_filter|none. "
                    + " || ".join(review_notes)
                )

        # Recent trades (last 5)
        recent = trades[-5:]
        recent_str = []
        for t in recent:
            outcome = "W" if t["pnl_usdt"] >= 0 else "L"
            recent_str.append(
                f"{t['symbol']}:{t['action'][0]}:{outcome}:{t['pnl_usdt']:+.1f}U"
            )
        if recent_str:
            lines.append("RECENT: " + ", ".join(recent_str))

        # Symbol-specific hints for current scan
        if symbols:
            hints = []
            for sym in symbols:
                s = sym_stats.get(sym)
                if not s or s["trades"] < 2:
                    continue
                wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
                long_wr = (s["long_wins"] / s["long_total"] * 100
                           if s["long_total"] > 0 else 0)
                short_wr = (s["short_wins"] / s["short_total"] * 100
                            if s["short_total"] > 0 else 0)
                if abs(long_wr - short_wr) > 20 and s["trades"] >= 3:
                    better = "L" if long_wr > short_wr else "S"
                    hints.append(f"{sym}:prefer_{better}({long_wr:.0f}%L/{short_wr:.0f}%S)")
            if hints:
                lines.append("HINTS: " + ", ".join(hints[:5]))

        if not lines:
            return ""

        return "\n\nLEARNING_DATA:\n" + "\n".join(lines)

    # ── Getters ───────────────────────────────────────────────────

    def get_streak(self) -> int:
        return self._mem["streak"]["current"]

    def get_total_trades(self) -> int:
        return len(self._mem["trades"])

    def get_symbol_stats(self, symbol: str) -> Optional[Dict]:
        return self._mem["symbol_stats"].get(symbol)

    def get_weekly_pnl(self) -> float:
        week_ago = (_now() - timedelta(days=7)).strftime("%Y-%m-%d")
        return sum(
            d["pnl"] for date, d in self._mem["daily_pnl"].items()
            if date >= week_ago
        )

    def get_recent_trades(self, n: int = 10) -> List[Dict]:
        return self._mem["trades"][-n:]

    def record_feedback_reviews(self, reviews: List[Dict]) -> int:
        """Attach LLM review responses to pending strategy feedback."""
        if not reviews:
            return 0

        self._mem = _load_memory()
        feedback = self._mem.setdefault("strategy_feedback", [])
        by_id = {item.get("id"): item for item in feedback if item.get("id")}
        saved = 0

        for review in reviews:
            fid = review.get("id")
            item = by_id.get(fid)
            if not item:
                continue

            decision = str(review.get("decision", "watch")).lower()
            if decision not in ("accepted", "rejected", "watch"):
                decision = "watch"
            applied_as = str(review.get("applied_as", "caution")).lower()
            if applied_as not in ("caution", "hard_filter", "none"):
                applied_as = "caution" if decision != "rejected" else "none"

            item["bot_review"] = {
                "reviewed_at": _now_str(),
                "decision": decision,
                "applied_as": applied_as,
                "reason": str(review.get("reason", ""))[:500],
            }
            if decision == "rejected":
                item["status"] = "rejected"
            else:
                item["status"] = "active"
                item["applied_as"] = applied_as
            saved += 1

        if saved:
            _save_memory(self._mem)
            logger.info(f"[Memory] Saved {saved} MCP feedback review(s)")
        return saved
