"""
utils/pnl_tracker.py — PnL tracker for both DRY RUN and LIVE mode.

Dry Run : simulates position lifecycle from signal data, persists to JSON.
Live    : queries realized PnL from Bybit API + tracks open positions.

Data stored in: pnl_store.json (auto-created in project root)
"""

import json
import time
import math
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from utils.logger import logger

_STORE_PATH = Path("pnl_store.json")


def _now_str() -> str:
    """UTC timestamp for trade records — keeps reset time consistent
    regardless of VPS timezone (matches Bybit funding cycle 00:00 UTC)."""
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _use_sqlite() -> bool:
    """True when Config.STORAGE_BACKEND == 'sqlite'. Imported lazily so this
    module has no hard dependency on config/storage at import time."""
    try:
        from config import Config
        return Config.STORAGE_BACKEND == "sqlite"
    except Exception:
        return False


def _empty_store() -> Dict:
    return {
        "open_positions": {},
        "closed_trades": [],
        "stats": {
            "total_realized": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "best_symbol": "",
            "worst_symbol": "",
        }
    }


def _load_store() -> Dict:
    if _use_sqlite():
        try:
            from storage import sqlite_backend
            return sqlite_backend.load_store()
        except Exception as exc:
            raise RuntimeError(f"[PnL] SQLite load failed: {exc}") from exc
    if _STORE_PATH.exists():
        try:
            return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _empty_store()


def _save_store(store: Dict) -> None:
    if _use_sqlite():
        from storage import sqlite_backend
        try:
            sqlite_backend.save_store(store)
            return
        except Exception:
            # Transient lock/IO hiccup (dashboard/MCP share the DB) — retry once
            # before raising so one blip doesn't abort the whole scan.
            time.sleep(0.5)
            try:
                sqlite_backend.save_store(store)
                return
            except Exception as exc:
                raise RuntimeError(f"[PnL] SQLite save failed: {exc}") from exc
    try:
        _STORE_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[PnL] Could not save store: {exc}")


class PnLTracker:
    """
    Tracks PnL for dry-run simulated trades and live Bybit trades.
    All monetary values in USDT.
    """

    def __init__(self, account_balance: float) -> None:
        self.account_balance = account_balance
        self._store = _load_store()
        # Cooldown set: symbols recently closed by tracker (SL/TP/trail/time-exit).
        # Sync should NOT re-add these for ~30s to give Bybit time to also close.
        # Without this cooldown, sync detects orphan position on Bybit and re-adds
        # right after we logically closed it.
        # {symbol: epoch_seconds_when_closed}
        self._recent_closes: Dict[str, float] = {}

    @staticmethod
    def _cost_model_enabled(pos: Optional[Dict] = None) -> bool:
        """True only for positions opened with the new dry-run cost model.

        The per-position marker avoids charging only the final leg of a legacy
        position that was opened before this feature existed.
        """
        if pos is not None:
            return bool(pos.get("costs_included", False))
        try:
            from config import Config
            return bool(Config.DRY_RUN and Config.DRY_RUN_INCLUDE_COSTS)
        except Exception:
            return False

    @staticmethod
    def _apply_dry_run_slippage(action: str, price: float) -> float:
        """Return a conservative market-fill price for a dry-run order."""
        try:
            from config import Config
            slip = max(float(Config.DRY_RUN_SLIPPAGE_PCT), 0.0) / 100.0
        except Exception:
            slip = 0.0
        if action == "LONG":
            return price * (1.0 - slip)  # closing a long is a sell
        return price * (1.0 + slip)      # closing a short is a buy

    @staticmethod
    def _calculate_fees(entry: float, exit_price: float, notional: float) -> float:
        """Estimate entry+exit taker fees for one closed notional slice."""
        try:
            from config import Config
            fee_rate = max(float(Config.TAKER_FEE_PCT), 0.0) / 100.0
        except Exception:
            fee_rate = 0.0
        if entry <= 0 or notional <= 0 or fee_rate <= 0:
            return 0.0
        quantity = notional / entry
        exit_notional = quantity * max(exit_price, 0.0)
        return round((notional + exit_notional) * fee_rate, 4)

    # ── Position lifecycle ─────────────────────────────────────────

    def record_entry(self, signal: Dict, exec_result: Dict) -> None:
        """Record a new position entry (dry run or live)."""
        sym = signal.get("symbol", "?")
        action = signal.get("action", "HOLD")
        planned_entry = float(signal.get("entry_price", 0))
        entry = planned_entry

        # CRITICAL: Use the SL/entry that EXECUTOR actually placed on Bybit,
        # NOT AI's signal SL. When SL_MODE=roi, executor REPLACES AI's tight
        # SL with wide -100% ROI SL. If tracker stores AI's tight value,
        # local check_sl_tp fires PREMATURELY (defeating the wide-buffer
        # purpose of SL_MODE=roi and preventing DCA from triggering).
        sl = float(exec_result.get("stop_loss", 0)) or float(signal.get("stop_loss", 0))
        # Same for entry — actual fill price > AI's planned price
        actual_entry = float(exec_result.get("entry_price", 0))
        if actual_entry > 0:
            entry = actual_entry

        costs_included = self._cost_model_enabled()
        position_id = uuid.uuid4().hex[:16]
        signal["_position_id"] = position_id
        try:
            from config import Config
            strategy_version = Config.STRATEGY_VERSION
            config_hash = Config.strategy_fingerprint()
        except Exception:
            strategy_version = "unknown"
            config_hash = "unknown"
        if costs_included:
            try:
                from config import Config
                slip = max(float(Config.DRY_RUN_SLIPPAGE_PCT), 0.0) / 100.0
            except Exception:
                slip = 0.0
            entry = entry * (1.0 + slip if action == "LONG" else 1.0 - slip)

        tps = [float(v) for v in signal.get("take_profit", []) if v]
        lev = int(exec_result.get("leverage", signal.get("leverage", 1)))
        size_pct = float(signal.get("position_size_percent", 1.0))
        actual_notional = float(exec_result.get("actual_notional", 0.0) or 0.0)
        notional = actual_notional or self.account_balance * (size_pct / 100.0) * lev

        # Parse trailing stop config from signal
        trail_cfg = signal.get("trailing_stop", {})
        if not isinstance(trail_cfg, dict):
            trail_cfg = {}

        pos = {
            "symbol": sym,
            "position_id": position_id,
            "action": action,
            "entry_price": entry,
            "planned_entry_price": planned_entry,
            "stop_loss": sl,
            "original_sl": sl,
            "sl_kind": "initial",   # initial | be_lock | tp2_lock | trail — atribusi reason exit
            "take_profit": tps,
            "leverage": lev,
            "notional": round(notional, 2),
            "original_notional": round(notional, 2),  # FIX #2: never reduced by partials
            "filled_qty": float(exec_result.get("qty", 0.0) or 0.0),
            "remaining_pct": 1.0,   # 100% of position remaining
            "size_pct": size_pct,
            "open_time": _now_str(),
            "status": exec_result.get("status", "unknown"),
            "peak_price": entry,
            "peak_at": 0.0,         # epoch saat peak terakhir maju (stall guard)
            # Trough = ujung terburuk yang pernah disentuh. Dipakai untuk MAE.
            # Tanpa MFE/MAE, menyetel lebar trail cuma tebak-tebakan: tidak ada
            # cara tahu apakah trail keluar terlalu cepat atau sudah pas.
            "trough_price": entry,
            "trailing_active": False,
            "trailing_activation_price": float(trail_cfg.get("activation_price", 0)),
            "trailing_trail_pct": float(trail_cfg.get("trail_pct", 3.0)),
            # ── Trailing take-profit (armed di TP3, target ikut bergeser) ──
            "tp_trail_active": False,
            "tp_trail_target": 0.0,      # target berjalan (informasional/notif)
            "tp_trail_giveback_pct": 0.0,
            "tp_trail_armed_at": 0.0,
            "tp_trail_arm_price": 0.0,
            "tp_hits": [],         # track which TPs have been hit
            "dca_count": 0,        # number of DCA additions
            "ai_tier": signal.get("_ai_tier", "?"),  # which AI generated this
            # Attribution pakai skor AI mentah — whale boost hanya untuk ranking,
            # jangan mencemari kohort ai_score di closed_trades.
            "ai_score": float(signal.get("_ai_score_raw", signal.get("score", 0)) or 0),
            "ai_confidence": float(signal.get("confidence", 0)),
            "high_conviction": bool(signal.get("_high_conviction", False)),
            "costs_included": costs_included,
            "strategy_version": strategy_version,
            "config_hash": config_hash,
            # Fee entry aktual dari exchange (live) — dialokasikan pro-rata ke
            # tiap leg close supaya PnL live tidak tercatat gross.
            "entry_fee_usdt": float(exec_result.get("fees", 0.0) or 0.0),
            "total_pnl_realized": 0.0,
            "total_gross_pnl_realized": 0.0,
            "total_fees_realized": 0.0,
        }
        self._store["open_positions"][sym] = pos
        _save_store(self._store)
        logger.info(f"[PnL] Entry recorded: {action} {sym} @ {entry:,.4f} notional={notional:,.2f}")

    def record_close(
        self,
        symbol: str,
        exit_price: float,
        reason: str = "manual",
        actual_fees: Optional[float] = None,
    ) -> Optional[Dict]:
        """Close a position and calculate realized PnL.

        actual_fees: exchange-reported exit fee (live). When provided it is
        booked (plus the unallocated share of the actual entry fee) instead of
        the dry-run cost model — live PnL was previously recorded gross.
        """
        pos = self._store["open_positions"].get(symbol)
        if not pos:
            return None

        entry = pos["entry_price"]
        notional = pos["notional"]
        action = pos["action"]

        if entry <= 0:
            logger.error(f"[PnL] Refusing to close corrupt position {symbol}: entry={entry}")
            return None
        self._store["open_positions"].pop(symbol, None)

        planned_exit_price = exit_price
        if self._cost_model_enabled(pos):
            exit_price = self._apply_dry_run_slippage(action, exit_price)

        if action == "LONG":
            gross_pnl = (exit_price - entry) / entry * notional
        else:
            gross_pnl = (entry - exit_price) / entry * notional

        if actual_fees is not None and actual_fees >= 0:
            _orig_n = pos.get("original_notional") or notional
            _entry_fee_share = (
                float(pos.get("entry_fee_usdt", 0.0) or 0.0)
                * (notional / _orig_n if _orig_n > 0 else 1.0)
            )
            fees = round(float(actual_fees) + _entry_fee_share, 4)
        else:
            fees = (
                self._calculate_fees(entry, exit_price, notional)
                if self._cost_model_enabled(pos) else 0.0
            )
        gross_pnl = round(gross_pnl, 4)
        pnl = round(gross_pnl - fees, 4)
        # FIX #2: pakai original_notional (bukan notional yang sudah dikurangi partial)
        # agar pnl_pct konsisten dengan Bybit yang selalu hitung dari margin awal.
        orig_notional = pos.get("original_notional") or notional
        pnl_pct = round(pnl / (orig_notional / pos["leverage"]) * 100, 2)
        partial_pnl = float(pos.get("total_pnl_realized", 0.0) or 0.0)
        partial_gross = float(pos.get("total_gross_pnl_realized", 0.0) or 0.0)
        partial_fees = float(pos.get("total_fees_realized", 0.0) or 0.0)
        position_pnl = round(partial_pnl + pnl, 4)
        position_gross_pnl = round(partial_gross + gross_pnl, 4)
        position_fees = round(partial_fees + fees, 4)
        position_pnl_pct = round(
            position_pnl / max(orig_notional / pos["leverage"], 0.01) * 100, 2
        )

        # ── MFE / MAE (persen harga, arah-agnostik) ───────────────
        # MFE = seberapa jauh harga SEMPAT menguntungkan sebelum exit.
        # MAE = seberapa dalam harga SEMPAT merugikan.
        # capture_ratio = porsi MFE yang benar-benar terealisasi.
        # Ini satu-satunya cara mengukur apakah trail memotong pemenang
        # terlalu cepat: capture rendah + MFE besar = trail terlalu ketat.
        _peak = float(pos.get("peak_price", entry) or entry)
        _trough = float(pos.get("trough_price", entry) or entry)
        if action == "LONG":
            _mfe_pct = (_peak - entry) / entry * 100.0
            _mae_pct = (entry - _trough) / entry * 100.0
            _realized_pct = (exit_price - entry) / entry * 100.0
        else:
            _mfe_pct = (entry - _peak) / entry * 100.0
            _mae_pct = (_trough - entry) / entry * 100.0
            _realized_pct = (entry - exit_price) / entry * 100.0
        _mfe_pct = max(_mfe_pct, 0.0)
        _mae_pct = max(_mae_pct, 0.0)
        _capture = round(_realized_pct / _mfe_pct, 4) if _mfe_pct > 1e-9 else None

        trade = {
            "symbol": symbol,
            "position_id": pos.get("position_id", ""),
            "action": action,
            "entry_price": entry,
            "exit_price": exit_price,
            "planned_exit_price": planned_exit_price,
            "mfe_pct": round(_mfe_pct, 4),
            "mae_pct": round(_mae_pct, 4),
            "realized_move_pct": round(_realized_pct, 4),
            "capture_ratio": _capture,
            "pnl_usdt": pnl,
            "pnl_pct": pnl_pct,
            "gross_pnl_usdt": gross_pnl,
            "fees_usdt": fees,
            "position_pnl_usdt": position_pnl,
            "position_gross_pnl_usdt": position_gross_pnl,
            "position_fees_usdt": position_fees,
            "position_pnl_pct": position_pnl_pct,
            "partial_pnl_usdt": round(partial_pnl, 4),
            "notional": notional,
            "leverage": pos["leverage"],
            "reason": reason,
            "open_time": pos["open_time"],
            "close_time": _now_str(),
            # Carry over AI metadata for performance analysis
            "ai_tier": pos.get("ai_tier", "?"),
            "ai_score": pos.get("ai_score", 0),
            "ai_confidence": pos.get("ai_confidence", 0),
            "high_conviction": pos.get("high_conviction", False),
            "strategy_version": pos.get("strategy_version", "legacy"),
            "config_hash": pos.get("config_hash", "legacy"),
        }
        self._store["closed_trades"].append(trade)

        # Update stats
        s = self._store["stats"]
        s.setdefault("flats", 0)
        s["total_realized"] = round(s["total_realized"] + pnl, 4)
        s["total_trades"] += 1
        if position_pnl > 0:
            s["wins"] += 1
            if position_pnl > s["best_trade"]:
                s["best_trade"] = position_pnl
                s["best_symbol"] = symbol
        elif position_pnl < 0:
            s["losses"] += 1
            if position_pnl < s["worst_trade"]:
                s["worst_trade"] = position_pnl
                s["worst_symbol"] = symbol
        else:
            s["flats"] += 1

        _save_store(self._store)
        # Mark this symbol as "recently closed" so sync doesn't immediately
        # re-add it (Bybit may take a few seconds to also reflect the close)
        self._recent_closes[symbol] = time.time()
        logger.info(
            f"[PnL] Closed {symbol}: leg={pnl:+.4f} USDT, "
            f"position={position_pnl:+.4f} USDT ({position_pnl_pct:+.2f}%) "
            f"fees={position_fees:.4f} reason={reason}"
        )
        return trade

    def partial_close_record(
        self,
        symbol: str,
        exit_price: float,
        pct_to_close: float,
        reason: str = "tp_partial",
        actual_fees: Optional[float] = None,
    ) -> Optional[Dict]:
        """
        Record a PARTIAL close — reduces remaining notional, books partial realized PnL,
        keeps the position open with the residual size.

        pct_to_close is % of REMAINING size (e.g. 30 = close 30% of what's left).
        After all partials reach 100% remaining_pct → full close.
        actual_fees: exchange-reported fee for this slice (live) — see record_close.
        """
        pos = self._store["open_positions"].get(symbol)
        if not pos:
            return None
        if exit_price <= 0 or pct_to_close <= 0:
            return None

        entry    = pos["entry_price"]
        notional = pos["notional"]
        action   = pos["action"]
        leverage = pos["leverage"]
        remaining_pct = pos.get("remaining_pct", 1.0)

        if entry <= 0 or notional <= 0:
            return None

        portion = max(0.0, min(pct_to_close / 100.0, 1.0))
        closed_notional = notional * portion

        planned_exit_price = exit_price
        if self._cost_model_enabled(pos):
            exit_price = self._apply_dry_run_slippage(action, exit_price)

        if action == "LONG":
            gross_pnl = (exit_price - entry) / entry * closed_notional
        else:
            gross_pnl = (entry - exit_price) / entry * closed_notional

        if actual_fees is not None and actual_fees >= 0:
            _orig_n = pos.get("original_notional") or notional
            _entry_fee_share = (
                float(pos.get("entry_fee_usdt", 0.0) or 0.0)
                * (closed_notional / _orig_n if _orig_n > 0 else 0.0)
            )
            fees = round(float(actual_fees) + _entry_fee_share, 4)
        else:
            fees = (
                self._calculate_fees(entry, exit_price, closed_notional)
                if self._cost_model_enabled(pos) else 0.0
            )
        gross_pnl = round(gross_pnl, 4)
        pnl = round(gross_pnl - fees, 4)
        margin_portion = closed_notional / max(leverage, 1)
        pnl_pct = round(pnl / max(margin_portion, 0.01) * 100, 2)

        # FIX #4: akumulasi total PnL posisi agar wins/losses dihitung dari keseluruhan, bukan partial terakhir
        pos["total_pnl_realized"] = round(pos.get("total_pnl_realized", 0.0) + pnl, 4)
        pos["total_gross_pnl_realized"] = round(
            pos.get("total_gross_pnl_realized", 0.0) + gross_pnl, 4
        )
        pos["total_fees_realized"] = round(
            pos.get("total_fees_realized", 0.0) + fees, 4
        )

        # Reduce position size
        new_notional = round(notional - closed_notional, 6)
        new_remaining = round(remaining_pct * (1 - portion), 6)
        pos["notional"] = new_notional
        pos["remaining_pct"] = max(0.0, new_remaining)
        tp_index = 0 if str(reason).startswith("tp1") else (
            1 if str(reason).startswith("tp2") else None
        )
        if tp_index is not None:
            pending = pos.get("pending_partial_closes", {})
            pending.pop(str(tp_index), None)
            pos["pending_partial_closes"] = pending
        position_completed = pos["remaining_pct"] <= 0.001 or pos["notional"] <= 0.01

        trade = {
            "symbol": symbol,
            "position_id": pos.get("position_id", ""),
            "action": action,
            "entry_price": entry,
            "exit_price": exit_price,
            "planned_exit_price": planned_exit_price,
            "pnl_usdt": pnl,
            "pnl_pct": pnl_pct,
            "gross_pnl_usdt": gross_pnl,
            "fees_usdt": fees,
            "notional": round(closed_notional, 4),
            "leverage": leverage,
            "reason": reason,
            "open_time": pos["open_time"],
            "close_time": _now_str(),
            "partial": True,
            "remaining_pct_after": pos["remaining_pct"],
            "position_completed": position_completed,
            "position_pnl_usdt": pos["total_pnl_realized"],
            "position_gross_pnl_usdt": pos["total_gross_pnl_realized"],
            "position_fees_usdt": pos["total_fees_realized"],
            "position_pnl_pct": round(
                pos["total_pnl_realized"]
                / max((pos.get("original_notional") or closed_notional) / leverage, 0.01)
                * 100,
                2,
            ),
            "ai_tier": pos.get("ai_tier", "?"),
            "ai_score": pos.get("ai_score", 0),
            "ai_confidence": pos.get("ai_confidence", 0),
            "high_conviction": pos.get("high_conviction", False),
            "strategy_version": pos.get("strategy_version", "legacy"),
            "config_hash": pos.get("config_hash", "legacy"),
        }
        self._store["closed_trades"].append(trade)

        # Update stats — partial PnL counts toward realized total but NOT
        # as a separate trade (the position is still open).
        s = self._store["stats"]
        s.setdefault("flats", 0)
        s["total_realized"] = round(s["total_realized"] + pnl, 4)

        # If nothing left → fully close the position (remove from open)
        if position_completed:
            self._store["open_positions"].pop(symbol, None)
            s["total_trades"] += 1
            # FIX #4: pakai total PnL seluruh posisi, bukan hanya partial terakhir
            total_pos_pnl = pos.get("total_pnl_realized", pnl)
            if total_pos_pnl > 0:
                s["wins"] += 1
                if total_pos_pnl > s["best_trade"]:
                    s["best_trade"] = total_pos_pnl
                    s["best_symbol"] = symbol
            elif total_pos_pnl < 0:
                s["losses"] += 1
                if total_pos_pnl < s["worst_trade"]:
                    s["worst_trade"] = total_pos_pnl
                    s["worst_symbol"] = symbol
            else:
                s["flats"] += 1
            logger.info(f"[PnL] {symbol} fully closed via partials")

        _save_store(self._store)
        logger.info(
            f"[PnL] Partial close {symbol}: {pct_to_close:.0f}% @ {exit_price:.4f} "
            f"PnL={pnl:+.4f} ({pnl_pct:+.2f}%) remaining={pos['remaining_pct']*100:.0f}% "
            f"reason={reason}"
        )
        return trade

    def queue_partial_retry(
        self,
        symbol: str,
        tp_index: int,
        exit_price: float,
        close_pct: float,
        reason: str,
    ) -> None:
        """Persist a failed exchange partial so a later scan retries it."""
        pos = self._store.get("open_positions", {}).get(symbol)
        if not pos:
            return
        pending = pos.setdefault("pending_partial_closes", {})
        pending[str(int(tp_index))] = {
            "tp_index": int(tp_index),
            "exit_price": float(exit_price),
            "close_pct": float(close_pct),
            "reason": str(reason),
        }
        _save_store(self._store)

    @staticmethod
    def _calc_trail_pct(atr_pct: float, leverage: int) -> float:
        """
        Dynamic trailing stop % based on ATR, leverage, and TRADING_MODE.
        Swing (4H primary) needs wider trail than intraday (15M) — 4H wick noise
        is large and 2% trail kills winners on normal pullback.

        Rule: 3.0× ATR (swing) / 2.5× ATR (intraday), min 0.8%, max 8%.
        Per-leverage caps differ by mode so swing winners get room to run.
        """
        from config import Config

        is_swing = Config.TRADING_MODE == "swing"
        mult = 3.0 if is_swing else 2.5

        trail = atr_pct * mult
        trail = max(trail, 0.8)   # floor — avoid noise shakeout
        trail = min(trail, 8.0)   # ceiling — don't give back too much

        if is_swing:
            # Swing: lebih lebar, biar winner punya napas di 4H
            if leverage >= 30:
                trail = min(trail, 4.0)
            elif leverage >= 8:
                trail = min(trail, 3.0)
        else:
            # Intraday: ketat (default lama)
            if leverage >= 8:
                trail = min(trail, 2.0)
            elif leverage >= 5:
                trail = min(trail, 4.0)

        return round(trail, 2)

    @staticmethod
    def _calc_tp_giveback_pct(atr_pct: float) -> float:
        """
        Giveback % dari peak untuk trailing take-profit (post-TP3).

        Sengaja lebih ketat dari _calc_trail_pct: di titik ini posisi sudah
        melewati TP3 (profit besar), yang dijaga adalah runner. Trail lebar
        di sini artinya menyerahkan kembali profit yang sudah nyata.
        """
        try:
            from config import Config as _C
            mult = _C.TRAILING_TP_GIVEBACK_ATR_MULT
            lo   = _C.TRAILING_TP_MIN_GIVEBACK_PCT
            hi   = _C.TRAILING_TP_MAX_GIVEBACK_PCT
        except Exception:
            mult, lo, hi = 1.5, 0.60, 5.0

        giveback = max(float(atr_pct), 0.0) * mult
        return round(min(max(giveback, lo), hi), 3)

    def check_sl_tp(self, market_data: List[Dict]) -> List[Dict]:
        """
        BE + Trail strategy:
          TP1 hit → SL moves to entry (breakeven), trailing activates at 1.5×ATR
          TP2 hit → SL advances to TP1 level, trail tightens
          TP3 hit → ARMS TRAILING TP (position stays open, target follows
                    price). Exit then comes from one of three places:
                      • giveback breach   → reason 'trailing_take_profit'
                      • extension wall    → reason 'tp_trail_max_extension'
                      • no new peak       → reason 'tp_trail_stall'
                    Set TRAILING_TP_ENABLED=false for the old fixed-price
                    TP3 full close ('take_profit').
          SL  hit → full close

        Milestone events (be_activated, sl_advanced) are emitted for Telegram
        notification but do NOT close the position or affect stats.
        """
        events = []
        mkt_map = {d["symbol"]: d for d in market_data}

        # Lazy import to avoid circular dependency at module load
        try:
            from config import Config as _Cfg
            _trading_mode = getattr(_Cfg, "TRADING_MODE", "swing")
            _stagnant_h   = getattr(_Cfg, "STAGNANT_HOLD_HOURS", 6.0)
            _stagnant_roi = getattr(_Cfg, "STAGNANT_ROI_PCT", 5.0)
            _max_hold_intra = getattr(_Cfg, "MAX_HOLD_HOURS_INTRADAY", 12.0)
            _max_hold_swing = getattr(_Cfg, "MAX_HOLD_HOURS_SWING", 72.0)
            _partial_enabled = getattr(_Cfg, "PARTIAL_CLOSE_ENABLED", True)
            _tp1_close_pct = getattr(_Cfg, "TP1_CLOSE_PCT", 30.0)
            _tp2_close_pct = getattr(_Cfg, "TP2_CLOSE_PCT", 30.0)
            _partial_min_move = getattr(_Cfg, "PARTIAL_CLOSE_MIN_MOVE_PCT", 0.20)
            _be_lock_roi_pct = getattr(_Cfg, "BE_PROFIT_LOCK_ROI_PCT", 5.0)
            _be_lock_pct = getattr(_Cfg, "BE_PROFIT_LOCK_PCT", 0.30)
            _tp2_trail_mult = getattr(_Cfg, "TP2_TRAIL_TIGHTEN_MULT", 0.75)
            _tp1_min_r = getattr(_Cfg, "TP1_PARTIAL_MIN_R", 0.5)
            _tp_trail_on = getattr(_Cfg, "TRAILING_TP_ENABLED", True)
            _tp_trail_step_mult = getattr(_Cfg, "TRAILING_TP_STEP_ATR_MULT", 1.0)
            _tp_trail_max_ext_r = getattr(_Cfg, "TRAILING_TP_MAX_EXTENSION_R", 4.0)
            _tp_trail_stall_h = getattr(_Cfg, "TRAILING_TP_STALL_HOURS", 8.0)
            _rt_cost_pct = 2.0 * (
                max(getattr(_Cfg, "TAKER_FEE_PCT", 0.055), 0.0)
                + max(getattr(_Cfg, "DRY_RUN_SLIPPAGE_PCT", 0.02), 0.0)
            )
        except Exception:
            _trading_mode = "swing"
            _stagnant_h, _stagnant_roi = 6.0, 5.0
            _max_hold_intra, _max_hold_swing = 12.0, 72.0
            _partial_enabled = True
            _tp1_close_pct, _tp2_close_pct = 30.0, 30.0
            _partial_min_move = 0.20
            _be_lock_roi_pct = 5.0
            _be_lock_pct = 0.30
            _tp2_trail_mult = 0.75
            _tp1_min_r = 0.5
            _tp_trail_on = True
            _tp_trail_step_mult = 1.0
            _tp_trail_max_ext_r = 4.0
            _tp_trail_stall_h = 8.0
            _rt_cost_pct = 0.15

        max_hold_h = _max_hold_intra if _trading_mode == "intraday" else _max_hold_swing

        for sym, pos in list(self._store["open_positions"].items()):
            mkt   = mkt_map.get(sym, {})
            price = mkt.get("current_price")
            if price is None:
                continue

            atr_pct = float(mkt.get("atr_pct", 2.0))
            action  = pos["action"]
            sl      = pos["stop_loss"]
            tps     = pos.get("take_profit", [])
            tp_hits = pos.get("tp_hits", [])

            # ── P1: Time-based auto-close ─────────────────────
            # Compute hours held & current ROI. Emit time_exit event so main.py
            # can route to executor.partial_close(100%) for live exchange close.
            entry_for_age = pos["entry_price"]
            leverage_for_age = float(pos.get("leverage", 1))
            hours_held = 0.0
            try:
                # IMPORTANT: open_time stored in UTC by _now_str(), so compare in UTC
                # to avoid TZ offset bug (was treating local-now - UTC-stored = +7h)
                from datetime import timezone
                open_dt = datetime.strptime(pos.get("open_time", ""), "%Y-%m-%d %H:%M:%S")
                open_dt = open_dt.replace(tzinfo=timezone.utc)
                hours_held = (datetime.now(timezone.utc) - open_dt).total_seconds() / 3600.0
            except Exception:
                hours_held = 0.0

            if entry_for_age > 0 and hours_held > 0:
                if action == "LONG":
                    price_pct = (price - entry_for_age) / entry_for_age * 100.0
                else:
                    price_pct = (entry_for_age - price) / entry_for_age * 100.0
                roi_pct = price_pct * leverage_for_age

                # Time-exit detection: identify LOSING positions that hit time threshold.
                # Bot does NOT auto-close — emits event for main.py to ask user
                # via Telegram (like DCA prompt). User decides y/n.
                #
                # Rules:
                # - Profit positions (ROI >= 0): NEVER prompt, never close.
                #   Let trail/TP3 handle exit when reverse.
                # - Losing positions held >= 12h (intraday): prompt user
                # - Losing positions held >= 6h + |ROI|<5% (stagnant): prompt user
                # - Cooldown 6h between re-prompts after user declines
                _decline_at = pos.get("time_exit_declined_at", 0)
                _now_ts = time.time()
                _cooldown_h = 6.0
                _can_prompt = (_now_ts - _decline_at) >= (_cooldown_h * 3600)

                time_exit_reason = None
                if _can_prompt and roi_pct < 0:   # ONLY when losing
                    if hours_held >= max_hold_h:
                        time_exit_reason = "time_exit_max"
                    elif (
                        hours_held >= _stagnant_h
                        and abs(roi_pct) < _stagnant_roi
                    ):
                        time_exit_reason = "time_exit_stagnant"

                if time_exit_reason:
                    logger.info(
                        f"[PnL] {sym} TIME-THRESHOLD {time_exit_reason}: "
                        f"held={hours_held:.1f}h ROI={roi_pct:+.1f}% "
                        f"— asking user via Telegram (no auto-close)"
                    )
                    # Emit USER-DECISION event. Bot does NOT auto-close.
                    # main.py asks via Telegram y/n (like DCA prompt).
                    # Reply Y → close, N → set cooldown 6h, timeout → re-ask next scan.
                    events.append({
                        "symbol": sym,
                        "action": action,
                        "entry_price": entry_for_age,
                        "exit_price": price,
                        "leverage": int(leverage_for_age),
                        "reason": time_exit_reason,
                        "requires_user_decision": True,   # not auto-close anymore
                        "hours_held": round(hours_held, 1),
                        "roi_at_exit": round(roi_pct, 2),
                    })
                    # Don't continue — let SL/TP/Trail still check this scan

            # ── Update peak price ─────────────────────────────
            # peak_at dicatat supaya trailing-TP punya basis stall guard:
            # runner PROFIT tidak pernah disentuh time-exit (time-exit hanya
            # untuk posisi rugi), jadi tanpa ini posisi bisa menggantung.
            peak = pos.get("peak_price", pos["entry_price"])
            if action == "LONG" and price > peak:
                pos["peak_price"] = price
                pos["peak_at"] = time.time()
                peak = price
            elif action == "SHORT" and price < peak:
                pos["peak_price"] = price
                pos["peak_at"] = time.time()
                peak = price

            # ── Update trough (worst excursion) ───────────────
            # Dicatat murni untuk MAE di closed_trades. Tidak dipakai
            # sebagai trigger apa pun — hanya alat ukur.
            trough = pos.get("trough_price", pos["entry_price"])
            if action == "LONG" and price < trough:
                pos["trough_price"] = price
            elif action == "SHORT" and price > trough:
                pos["trough_price"] = price

            # ── Pre-TP1 trailing arm (honor AI's activation price) ──
            # ts.ap dari signal disimpan di record_entry tapi dulu tidak pernah
            # dibaca — posisi yang berhenti setipis apapun di bawah TP1 tidak
            # punya proteksi sama sekali. Arming trailing hanya bisa MENAIKKAN
            # SL (ratchet), jadi ini murni proteksi tambahan.
            if not pos.get("trailing_active", False):
                _act_price = float(pos.get("trailing_activation_price", 0) or 0)
                if _act_price > 0:
                    _act_crossed = (
                        (action == "LONG" and price >= _act_price and _act_price > pos["entry_price"]) or
                        (action == "SHORT" and price <= _act_price and _act_price < pos["entry_price"])
                    )
                    if _act_crossed:
                        _pre_trail = self._calc_trail_pct(atr_pct, pos["leverage"])
                        pos["trailing_trail_pct"] = max(round(_pre_trail, 2), 0.5)
                        pos["trailing_active"] = True
                        pos["peak_price"] = price
                        peak = price
                        logger.info(
                            f"[PnL] {sym} trailing armed pre-TP1 @ activation "
                            f"{_act_price:.4f} (trail={pos['trailing_trail_pct']:.1f}%)"
                        )

            # ── Update trailing SL / trailing TP ──────────────
            # Satu ratchet, dua rezim:
            #   pre-TP3  → jarak = trailing_trail_pct (lebar, kasih napas)
            #   post-TP3 → jarak = tp_trail_giveback_pct (ketat, jaga runner)
            # Trailing TP TIDAK memakai limit order yang digeser-geser: target
            # statis di exchange akan selalu ketinggalan harga. Yang digeser
            # adalah floor-nya, dan exit terjadi via market close saat floor
            # ditembus — sama seperti trailing SL, hanya di wilayah profit.
            if pos.get("tp_trail_active", False):
                _give = pos.get("tp_trail_giveback_pct", 0.0)
                if _give <= 0:
                    _give = self._calc_tp_giveback_pct(atr_pct)
                    pos["tp_trail_giveback_pct"] = _give
                trail_f = _give / 100.0
                _kind_on_advance = "tp_trail"
            elif pos.get("trailing_active", False):
                trail_f = pos.get("trailing_trail_pct", 3.0) / 100.0
                _kind_on_advance = "trail"
            else:
                trail_f = None
                _kind_on_advance = None

            if trail_f is not None:
                if action == "LONG":
                    t_sl = peak * (1 - trail_f)
                    if t_sl > sl:
                        pos["stop_loss"] = round(t_sl, 6)
                        pos["sl_kind"] = _kind_on_advance
                        sl = pos["stop_loss"]
                else:
                    t_sl = peak * (1 + trail_f)
                    if t_sl < sl:
                        pos["stop_loss"] = round(t_sl, 6)
                        pos["sl_kind"] = _kind_on_advance
                        sl = pos["stop_loss"]

            # ── Geser target trailing TP mengikuti peak ───────
            # Target = peak + step×ATR (LONG) / peak − step×ATR (SHORT).
            # Ratchet-only, dan berhenti maju saat ekstensi melewati
            # TRAILING_TP_MAX_EXTENSION_R × risiko awal → di titik itu
            # target dijadikan dinding: sentuh = ambil untung.
            if pos.get("tp_trail_active", False):
                _step_f = max(atr_pct, 0.0) * _tp_trail_step_mult / 100.0
                _cap_price = 0.0
                _init_sl_ext = float(pos.get("original_sl", 0) or 0)
                _tp3_ref = float(pos.get("tp_trail_arm_price", 0) or 0)
                if (
                    _tp_trail_max_ext_r > 0
                    and _init_sl_ext > 0
                    and _tp3_ref > 0
                ):
                    _risk_abs = abs(entry_for_age - _init_sl_ext)
                    if _risk_abs > 0:
                        _ext = _tp_trail_max_ext_r * _risk_abs
                        _cap_price = (
                            _tp3_ref + _ext if action == "LONG" else _tp3_ref - _ext
                        )

                if action == "LONG":
                    _new_target = peak * (1.0 + _step_f)
                    if _cap_price > 0:
                        _new_target = min(_new_target, _cap_price)
                    if _new_target > pos.get("tp_trail_target", 0.0):
                        pos["tp_trail_target"] = round(_new_target, 8)
                else:
                    _new_target = peak * (1.0 - _step_f)
                    if _cap_price > 0:
                        _new_target = max(_new_target, _cap_price)
                    _cur_t = pos.get("tp_trail_target", 0.0)
                    if _cur_t <= 0 or _new_target < _cur_t:
                        pos["tp_trail_target"] = round(_new_target, 8)

                # Dinding ekstensi: harga sudah lari sejauh cap → cukup.
                if _cap_price > 0:
                    _cap_hit = (
                        (action == "LONG" and price >= _cap_price) or
                        (action == "SHORT" and price <= _cap_price)
                    )
                    if _cap_hit:
                        logger.info(
                            f"[PnL] {sym} TRAIL-TP max extension "
                            f"{_tp_trail_max_ext_r:.1f}R @ {_cap_price:.6f} "
                            f"(price={price:.6f}) — requesting Bybit close"
                        )
                        events.append({
                            "symbol": sym,
                            "action": action,
                            "entry_price": pos["entry_price"],
                            "exit_price": price,
                            "leverage": pos["leverage"],
                            "reason": "tp_trail_max_extension",
                            "requires_close": True,
                            "close_pct": 100.0,
                        })
                        _save_store(self._store)
                        continue

                # Stall guard: runner profit mandek. time-exit tidak pernah
                # menyentuh posisi profit, jadi guard-nya harus di sini.
                #
                # PRESEDENSI: floor menang atas stall. Kalau harga sudah
                # menembus floor DAN posisi mandek, exit yang jujur adalah
                # 'trailing_take_profit' (blok SL di bawah), bukan
                # 'tp_trail_stall' — dan blok itu juga mengisi di
                # worse-of(floor, observed), bukan di observed saja.
                _floor_breached = sl > 0 and (
                    (action == "LONG" and price <= sl) or
                    (action == "SHORT" and price >= sl)
                )
                if _tp_trail_stall_h > 0 and not _floor_breached:
                    _last_peak_at = float(
                        pos.get("peak_at") or pos.get("tp_trail_armed_at") or 0.0
                    )
                    if _last_peak_at > 0:
                        _idle_h = (time.time() - _last_peak_at) / 3600.0
                        if _idle_h >= _tp_trail_stall_h:
                            logger.info(
                                f"[PnL] {sym} TRAIL-TP stalled {_idle_h:.1f}h "
                                f"without new peak — requesting Bybit close"
                            )
                            events.append({
                                "symbol": sym,
                                "action": action,
                                "entry_price": pos["entry_price"],
                                "exit_price": price,
                                "leverage": pos["leverage"],
                                "reason": "tp_trail_stall",
                                "requires_close": True,
                                "close_pct": 100.0,
                                "hours_held": round(hours_held, 1),
                            })
                            _save_store(self._store)
                            continue

            # ── Check SL hit → emit close event (main.py will close on Bybit) ──
            # IMPORTANT: do NOT call record_close here in LIVE mode. The trail
            # SL is local-only — Bybit doesn't know about it. We must send a
            # market close order to Bybit FIRST, then update tracker. main.py
            # routes requires_close=True events through executor.partial_close.
            if sl > 0:
                hit_sl = (
                    (action == "LONG"  and price <= sl) or
                    (action == "SHORT" and price >= sl)
                )
                if hit_sl:
                    # Atribusi jujur per jenis SL — 'trailing_stop' dulu dipakai
                    # untuk BE-lock stop-out juga, mencemari analitik exit.
                    _kind = pos.get("sl_kind", "initial")
                    reason = {
                        "trail": "trailing_stop",
                        "be_lock": "be_lock_stop",
                        "tp2_lock": "tp2_lock_stop",
                        # Ditembus setelah TP3 = ini exit PROFIT (trailing TP
                        # yang mengunci), bukan stop-loss. Memberinya nama
                        # 'stop_loss' akan mencemari WR & analitik exit.
                        "tp_trail": "trailing_take_profit",
                    }.get(_kind, "stop_loss")
                    # Dry-run honesty: harga sudah bisa gap MELEWATI level stop
                    # antar-scan (300s). Isi di harga yang LEBIH BURUK dari
                    # (stop, observed) — jangan berasumsi fill tepat di stop.
                    if action == "LONG":
                        _fill = min(sl, price)
                    else:
                        _fill = max(sl, price)
                    logger.info(
                        f"[PnL] {sym} {reason.upper()} hit @ {sl:.6f} "
                        f"(price={price:.6f}, fill={_fill:.6f}) — requesting Bybit close"
                    )
                    events.append({
                        "symbol": sym,
                        "action": action,
                        "entry_price": pos["entry_price"],
                        "exit_price": _fill,
                        "leverage": pos["leverage"],
                        "reason": reason,
                        "requires_close": True,
                        "close_pct": 100.0,
                    })
                    continue

            entry = pos["entry_price"]

            # A TP milestone is marked only once, but a failed live reduce-only
            # order must remain retryable.  Process the oldest pending partial
            # before advancing to any later target.
            pending_partials = pos.get("pending_partial_closes", {})
            if pending_partials:
                pending = min(
                    pending_partials.values(),
                    key=lambda item: int(item.get("tp_index", 99)),
                )
                events.append({
                    "symbol": sym,
                    "action": action,
                    "entry_price": entry,
                    "exit_price": float(pending.get("exit_price", price)),
                    "leverage": pos["leverage"],
                    "reason": pending.get("reason", "tp_partial_retry"),
                    "requires_partial_close": True,
                    "close_pct": float(pending.get("close_pct", 0.0)),
                    "tp_index": int(pending.get("tp_index", 0)),
                    "retry": True,
                })
                _save_store(self._store)
                continue

            # ── TP1 → Breakeven + activate trailing ──────────
            if len(tps) > 0 and 0 not in tp_hits:
                tp1 = tps[0]
                # Guard: TP must be on correct side of entry
                tp1_ok = (action == "LONG" and tp1 > entry) or (action == "SHORT" and tp1 < entry)
                if not tp1_ok:
                    logger.warning(f"[PnL] {sym} TP1={tp1} invalid for {action} (entry={entry}) — skipped")
                    tp_hits.append(0)   # mark as consumed so we don't spam
                    pos["tp_hits"] = tp_hits
                hit = tp1_ok and ((action == "LONG" and price >= tp1) or (action == "SHORT" and price <= tp1))
                if hit:
                    tp_hits.append(0)
                    pos["tp_hits"] = tp_hits
                    # SL → entry + profit lock buffer (covers fees + lock gain)
                    # Without buffer, "BE" exit would actually be -fees = net loss.
                    #
                    # INVARIANT (audit 2026-07-26): lock HARUS < jarak TP1.
                    # Dulu lock ROI 5%/lev di intraday >= jarak TP1, lalu di-cap
                    # ke harga pasar → SL == market → runner 85% langsung mati
                    # di tick berikutnya. Itulah biang WR-naik-profit-flat Juli.
                    # Sekarang: lock = min(lock, 50% jarak TP1), floor biaya
                    # round-trip, dan TIDAK PERNAH dipatok ke harga pasar.
                    leverage = max(float(pos.get("leverage", 1)) or 1.0, 1.0)
                    tp1_move_pct = abs(tp1 - entry) / entry * 100.0
                    if _be_lock_roi_pct > 0:
                        _lock_price_pct = _be_lock_roi_pct / leverage
                        _lock_label = f"{_be_lock_roi_pct:.2f}% ROI"
                    else:
                        _lock_price_pct = _be_lock_pct
                        _lock_label = f"{_be_lock_pct:.2f}% price"
                    _half_tp1 = 0.5 * tp1_move_pct
                    if _lock_price_pct > _half_tp1:
                        _lock_price_pct = _half_tp1
                        _lock_label += f" → capped 0.5×TP1 ({_half_tp1:.3f}%)"
                    # Floor: minimal menutup biaya round-trip, tapi tetap < TP1
                    _lock_price_pct = max(_lock_price_pct, min(_rt_cost_pct, 0.9 * tp1_move_pct))
                    _lock_f = _lock_price_pct / 100.0
                    if action == "LONG":
                        be_sl = pos["entry_price"] * (1.0 + _lock_f)
                    else:
                        be_sl = pos["entry_price"] * (1.0 - _lock_f)
                    pos["stop_loss"] = round(be_sl, 8)
                    pos["sl_kind"] = "be_lock"
                    sl = pos["stop_loss"]
                    # After TP1: full trail_pct (biarkan winner jalan)
                    trail_pct = self._calc_trail_pct(atr_pct, pos["leverage"])
                    trail_pct = max(round(trail_pct, 2), 0.5)  # floor 0.5%
                    pos["trailing_trail_pct"] = trail_pct
                    pos["trailing_active"]    = True
                    pos["peak_price"]         = price       # reset peak
                    logger.info(
                        f"[PnL] {sym} TP1 @ {tp1:.4f} → "
                        f"SL=BE+{_lock_label}({be_sl:.4f}), trail={trail_pct:.1f}% "
                        f"(ATR={atr_pct:.2f}%, full trail)"
                    )
                    events.append({
                        "symbol": sym, "action": action,
                        "entry_price": pos["entry_price"],
                        "exit_price": price, "pnl_usdt": 0.0, "pnl_pct": 0.0,
                        "leverage": pos["leverage"],
                        "reason": "be_activated", "partial": True, "milestone": True,
                        "trail_pct": trail_pct,
                    })

                    # P2: Emit partial-close request at TP1
                    # Guard 1: skip partial if TP1 too close to entry (fees would
                    # net negative). Default 0.2% covers Bybit taker fee × 2.
                    # Guard 2 (audit 2026-07-26): TP1 harus membayar minimal
                    # TP1_PARTIAL_MIN_R × risiko awal. Juli: 126 partial TP1
                    # avg +$0.55 — booking 15% di ~0.3-0.5R itu murni generator
                    # fee; BE-lock sudah jadi asuransi downside-nya.
                    tp1_move_pct = abs(tp1 - entry) / entry * 100
                    _init_sl = float(pos.get("original_sl", 0) or 0)
                    _init_risk_pct = (
                        abs(entry - _init_sl) / entry * 100 if _init_sl > 0 else 0.0
                    )
                    _tp1_pays_enough = (
                        _init_risk_pct <= 0
                        or tp1_move_pct >= _tp1_min_r * _init_risk_pct
                    )
                    if (
                        _partial_enabled
                        and _tp1_close_pct > 0
                        and tp1_move_pct >= _partial_min_move
                        and _tp1_pays_enough
                    ):
                        events.append({
                            "symbol": sym, "action": action,
                            "entry_price": pos["entry_price"],
                            "exit_price": tp1,
                            "leverage": pos["leverage"],
                            "reason": "tp1_partial",
                            "requires_partial_close": True,
                            "close_pct": _tp1_close_pct,
                            "tp_index": 0,
                        })
                    elif _partial_enabled and _tp1_close_pct > 0:
                        logger.info(
                            f"[PnL] {sym} TP1 partial SKIPPED: move {tp1_move_pct:.3f}% "
                            f"(min {_partial_min_move:.2f}%, min R "
                            f"{_tp1_min_r:.2f}×{_init_risk_pct:.2f}%). "
                            f"BE+trail still active."
                        )

            # ── TP2 → Advance SL to TP1 level ────────────────
            if len(tps) > 1 and 1 not in tp_hits and 0 in tp_hits:
                tp2 = tps[1]
                tp2_ok = (action == "LONG" and tp2 > entry) or (action == "SHORT" and tp2 < entry)
                if not tp2_ok:
                    logger.warning(f"[PnL] {sym} TP2={tp2} invalid for {action} (entry={entry}) — skipped")
                    tp_hits.append(1)
                    pos["tp_hits"] = tp_hits
                hit = tp2_ok and ((action == "LONG" and price >= tp2) or (action == "SHORT" and price <= tp2))
                if hit:
                    tp_hits.append(1)
                    pos["tp_hits"] = tp_hits
                    new_sl = tps[0]
                    advanced = False
                    if action == "LONG" and new_sl > pos["stop_loss"]:
                        pos["stop_loss"] = new_sl
                        pos["sl_kind"] = "tp2_lock"
                        advanced = True
                    elif action == "SHORT" and new_sl < pos["stop_loss"]:
                        pos["stop_loss"] = new_sl
                        pos["sl_kind"] = "tp2_lock"
                        advanced = True
                    sl = pos["stop_loss"]
                    # Setelah TP2: trail diperketat TP2_TRAIL_TIGHTEN_MULT
                    # (knob ini dulu dead config — docstring janji tighten,
                    # kode tidak pernah memakainya).
                    trail_pct = self._calc_trail_pct(atr_pct, pos["leverage"])
                    trail_pct = max(round(trail_pct * _tp2_trail_mult, 2), 0.5)
                    pos["trailing_trail_pct"] = trail_pct
                    logger.info(
                        f"[PnL] {sym} TP2 @ {tp2:.4f} → "
                        f"SL→TP1({new_sl:.4f}), trail={trail_pct:.1f}%"
                    )
                    events.append({
                        "symbol": sym, "action": action,
                        "entry_price": pos["entry_price"],
                        "exit_price": price, "pnl_usdt": 0.0, "pnl_pct": 0.0,
                        "leverage": pos["leverage"],
                        "reason": "sl_advanced", "partial": True, "milestone": True,
                        "new_sl": new_sl, "trail_pct": trail_pct,
                    })

                    # P2: Emit partial-close request at TP2
                    tp2_move_pct = abs(tp2 - entry) / entry * 100
                    if (
                        _partial_enabled
                        and _tp2_close_pct > 0
                        and tp2_move_pct >= _partial_min_move
                    ):
                        events.append({
                            "symbol": sym, "action": action,
                            "entry_price": pos["entry_price"],
                            "exit_price": tp2,
                            "leverage": pos["leverage"],
                            "reason": "tp2_partial",
                            "requires_partial_close": True,
                            "close_pct": _tp2_close_pct,
                            "tp_index": 1,
                        })
                    elif _partial_enabled and _tp2_close_pct > 0:
                        logger.info(
                            f"[PnL] {sym} TP2 partial SKIPPED: move {tp2_move_pct:.3f}% "
                            f"< min {_partial_min_move:.2f}% (fees would net negative). "
                            f"SL still advanced to TP1."
                        )

            # ── TP3 → Full close ──────────────────────────────
            if len(tps) > 2 and 2 not in tp_hits:
                tp3 = tps[2]
                tp3_ok = (action == "LONG" and tp3 > entry) or (action == "SHORT" and tp3 < entry)
                if not tp3_ok:
                    logger.warning(f"[PnL] {sym} TP3={tp3} invalid for {action} (entry={entry}) — skipped")
                    tp_hits.append(2)
                    pos["tp_hits"] = tp_hits
                hit = tp3_ok and ((action == "LONG" and price >= tp3) or (action == "SHORT" and price <= tp3))
                if hit and _tp_trail_on:
                    # TP3 bukan lagi titik exit — jadi titik ARM trailing TP.
                    # Alasan: TP3 adalah tebakan AI dari jam-jam sebelumnya.
                    # Runner (±64.6% posisi dengan default TP1/TP2) selalu
                    # dipotong di situ walau tren masih jalan. Sekarang target
                    # ikut bergeser mengikuti peak; exit saat harga memberi
                    # balik `giveback` dari peak, kena dinding ekstensi, atau
                    # mandek (stall guard).
                    tp_hits.append(2)
                    pos["tp_hits"] = tp_hits
                    _give = self._calc_tp_giveback_pct(atr_pct)
                    pos["tp_trail_active"] = True
                    pos["tp_trail_giveback_pct"] = _give
                    pos["tp_trail_armed_at"] = time.time()
                    pos["tp_trail_arm_price"] = tp3
                    # Peak di-reset ke harga sekarang: floor giveback harus
                    # dihitung dari titik arming, bukan dari peak lama yang
                    # bisa jauh di atas/bawah dan langsung stop-out.
                    pos["peak_price"] = price
                    pos["peak_at"] = time.time()
                    peak = price
                    _step_f = max(atr_pct, 0.0) * _tp_trail_step_mult / 100.0
                    pos["tp_trail_target"] = round(
                        price * (1.0 + _step_f) if action == "LONG"
                        else price * (1.0 - _step_f), 8
                    )
                    # Floor langsung dipasang — ratchet-only, tidak akan
                    # pernah menurunkan proteksi yang sudah ada.
                    _floor = (
                        price * (1.0 - _give / 100.0) if action == "LONG"
                        else price * (1.0 + _give / 100.0)
                    )
                    if (
                        (action == "LONG" and _floor > pos["stop_loss"]) or
                        (action == "SHORT" and (pos["stop_loss"] <= 0 or _floor < pos["stop_loss"]))
                    ):
                        pos["stop_loss"] = round(_floor, 8)
                        pos["sl_kind"] = "tp_trail"
                        sl = pos["stop_loss"]
                    logger.info(
                        f"[PnL] {sym} TP3 @ {tp3:.6f} → TRAILING TP ARMED "
                        f"(giveback={_give:.2f}%, floor={pos['stop_loss']:.6f}, "
                        f"next target={pos['tp_trail_target']:.6f}, ATR={atr_pct:.2f}%) "
                        f"— position stays open, target follows price"
                    )
                    events.append({
                        "symbol": sym, "action": action,
                        "entry_price": pos["entry_price"],
                        "exit_price": price, "pnl_usdt": 0.0, "pnl_pct": 0.0,
                        "leverage": pos["leverage"],
                        "reason": "tp_trail_armed", "partial": True, "milestone": True,
                        "giveback_pct": _give,
                        "new_sl": pos["stop_loss"],
                        "tp_trail_target": pos["tp_trail_target"],
                    })
                elif hit:
                    # Perilaku lama (TRAILING_TP_ENABLED=false): full close di
                    # harga TP3. Emit close event, main.py tutup di Bybit dulu.
                    logger.info(
                        f"[PnL] {sym} TP3 hit @ {tp3:.6f} (price={price:.6f}) "
                        f"— requesting Bybit close"
                    )
                    events.append({
                        "symbol": sym,
                        "action": action,
                        "entry_price": pos["entry_price"],
                        "exit_price": tp3,
                        "leverage": pos["leverage"],
                        "reason": "take_profit",
                        "requires_close": True,
                        "close_pct": 100.0,
                    })
                    continue

            _save_store(self._store)

        return events

    # ── Computed metrics ──────────────────────────────────────────

    def unrealized_pnl(self, market_data: List[Dict]) -> float:
        """Calculate total unrealized PnL across all open dry-run positions."""
        prices = {d["symbol"]: d["current_price"] for d in market_data}
        total = 0.0
        for sym, pos in self._store["open_positions"].items():
            price = prices.get(sym)
            if price is None or pos["entry_price"] <= 0:
                continue
            notional = pos["notional"]
            entry = pos["entry_price"]
            if pos["action"] == "LONG":
                pnl = (price - entry) / entry * notional
            else:
                pnl = (entry - price) / entry * notional
            total += pnl
        return round(total, 4)

    def open_positions_summary(self, market_data: List[Dict]) -> List[Dict]:
        """Return enriched open position list with current PnL."""
        prices = {d["symbol"]: d["current_price"] for d in market_data}
        summary = []
        for sym, pos in self._store["open_positions"].items():
            price = prices.get(sym, 0.0)
            entry = pos["entry_price"]
            notional = pos["notional"]
            upnl = 0.0
            upnl_pct = 0.0
            if price > 0 and entry > 0:
                if pos["action"] == "LONG":
                    upnl = (price - entry) / entry * notional
                else:
                    upnl = (entry - price) / entry * notional
                upnl_pct = upnl / (notional / pos["leverage"]) * 100
            summary.append({
                **pos,
                "current_price": price,
                "unrealized_pnl": round(upnl, 4),
                "unrealized_pnl_pct": round(upnl_pct, 2),
            })
        return summary

    def get_stats(self) -> Dict:
        s = self._store["stats"]
        from utils.trade_analytics import calculate_position_metrics

        metrics = calculate_position_metrics(self._store.get("closed_trades", []))
        total = metrics["completed_trades"]
        wins = metrics["wins"]
        losses = metrics["losses"]
        flats = metrics["flats"]
        win_rate = (wins / total * 100) if total > 0 else 0.0
        best = metrics.get("best_lifecycle") or {}
        worst = metrics.get("worst_lifecycle") or {}
        return {
            "realized_pnl": s["total_realized"],
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "win_rate": round(win_rate, 1),
            "best_trade": best.get("position_pnl_usdt", 0.0),
            "worst_trade": worst.get("position_pnl_usdt", 0.0),
            "best_symbol": best.get("symbol", ""),
            "worst_symbol": worst.get("symbol", ""),
            "profit_factor": metrics["profit_factor"],
            "expectancy_usdt": metrics["expectancy_usdt"],
            "max_drawdown_usdt": metrics["max_drawdown_usdt"],
            "open_count": len(self._store["open_positions"]),
        }

    def get_recent_trades(self, n: int = 5) -> List[Dict]:
        from utils.trade_analytics import aggregate_position_trades

        positions = aggregate_position_trades(self._store.get("closed_trades", []))
        recent = []
        for position in positions[-n:]:
            item = dict(position)
            item["final_leg_pnl_usdt"] = item.get("pnl_usdt", 0.0)
            item["pnl_usdt"] = item.get("position_pnl_usdt", 0.0)
            if "position_pnl_pct" in item:
                item["pnl_pct"] = item["position_pnl_pct"]
            recent.append(item)
        return recent

    def get_performance_breakdown(self) -> Dict:
        """
        Detailed perf breakdown: by AI tier, by symbol, win rate, avg win/loss.
        Useful for diagnosing which AI/symbol generates profitable signals.
        """
        from collections import defaultdict
        from utils.trade_analytics import aggregate_position_trades

        closed = aggregate_position_trades(self._store.get("closed_trades", []))

        by_tier   = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "flats": 0, "pnl": 0.0,
                                          "best": 0.0, "worst": 0.0})
        by_symbol = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "flats": 0, "pnl": 0.0,
                                          "best": 0.0, "worst": 0.0})
        by_strategy = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "flats": 0, "pnl": 0.0,
                                            "best": 0.0, "worst": 0.0})

        for t in closed:
            pnl = float(t.get("position_pnl_usdt", 0))
            tier = t.get("ai_tier", "?")
            sym = t.get("symbol", "?")
            strategy = t.get("strategy_version", "legacy")
            for bucket in (by_tier[tier], by_symbol[sym], by_strategy[strategy]):
                bucket["trades"] += 1
                bucket["pnl"]    = round(bucket["pnl"] + pnl, 4)
                if pnl > 0:
                    bucket["wins"] += 1
                    if pnl > bucket["best"]:
                        bucket["best"] = pnl
                elif pnl < 0:
                    bucket["losses"] += 1
                    if pnl < bucket["worst"]:
                        bucket["worst"] = pnl
                else:
                    bucket["flats"] += 1

        # Compute win rates
        for d in (by_tier, by_symbol, by_strategy):
            for k, v in d.items():
                tot = v["trades"]
                v["win_rate"] = round(v["wins"]/tot*100, 1) if tot > 0 else 0.0
                v["avg_pnl"]  = round(v["pnl"]/tot, 4) if tot > 0 else 0.0

        return {
            "by_tier":   dict(by_tier),
            "by_symbol": dict(by_symbol),
            "by_strategy": dict(by_strategy),
        }

    # ── Live Position Sync ─────────────────────────────────────────

    def sync_positions(
        self,
        bybit_positions: List[Dict],
        closed_pnl_list: List[Dict] = None,
    ) -> Dict:
        """
        Reconcile pnl_store with actual Bybit open positions.

        - Positions on Bybit but not in store → add them
        - Positions in store but not on Bybit → mark closed (with real PnL if available)
        - Both have the position → update entry_price / leverage to actual

        Returns: {"added": [...], "updated": [...], "closed": [...]}
        """
        bybit_map = {p["symbol"]: p for p in bybit_positions}
        store_map = self._store["open_positions"]
        result = {"added": [], "updated": [], "closed": []}

        # Build lookup of recently closed PnL per symbol
        closed_map: Dict[str, Dict] = {}
        if closed_pnl_list:
            for c in closed_pnl_list:
                sym = c.get("symbol", "")
                # Keep the most recent close per symbol
                if sym and sym not in closed_map:
                    closed_map[sym] = c

        # 1. Positions in store but NOT on Bybit → closed on exchange
        for sym in list(store_map.keys()):
            if sym in bybit_map:
                continue

            pos = store_map[sym]
            closed_data = closed_map.get(sym)

            if closed_data and closed_data.get("exit_price", 0) > 0:
                exit_price = closed_data["exit_price"]
                real_pnl = closed_data["realized_pnl"]

                # Use record_close to properly update stats
                trade = self.record_close(sym, exit_price, reason="closed_on_exchange")
                if trade:
                    # Correct PnL to actual Bybit realized PnL
                    diff = real_pnl - trade["pnl_usdt"]
                    if abs(diff) > 0.001:
                        bot_position_pnl = float(
                            trade.get("position_pnl_usdt", trade["pnl_usdt"])
                        )

                        trade["pnl_usdt"] = round(real_pnl, 4)
                        # FIX #3: pakai original_notional agar koreksi pct konsisten dengan Bybit
                        orig_n = pos.get("original_notional") or pos["notional"]
                        trade["pnl_pct"] = round(
                            real_pnl / max(orig_n / pos["leverage"], 0.01) * 100, 2
                        )
                        partial_pnl = float(pos.get("total_pnl_realized", 0.0) or 0.0)
                        actual_position_pnl = round(partial_pnl + real_pnl, 4)
                        trade["position_pnl_usdt"] = actual_position_pnl
                        trade["position_pnl_pct"] = round(
                            actual_position_pnl
                            / max(orig_n / pos["leverage"], 0.01)
                            * 100,
                            2,
                        )
                        # closedPnl is the authoritative net result; this sync
                        # endpoint does not expose a reliable final fee split.
                        trade["position_gross_pnl_usdt"] = actual_position_pnl
                        trade["position_fees_usdt"] = float(
                            pos.get("total_fees_realized", 0.0) or 0.0
                        )
                        bot_pnl_was_positive = bot_position_pnl > 0
                        bybit_pnl_positive = actual_position_pnl > 0
                        # Fix the last closed trade entry
                        if self._store["closed_trades"]:
                            self._store["closed_trades"][-1] = trade
                        # Correct cumulative stats
                        s = self._store["stats"]
                        s["total_realized"] = round(s["total_realized"] + diff, 4)

                        # FIX WIN RATE: jika sign PnL berubah setelah koreksi Bybit,
                        # flip wins/losses agar win rate sinkron dengan Bybit.
                        if bot_pnl_was_positive != bybit_pnl_positive:
                            if bybit_pnl_positive:
                                # bot hitung loss, Bybit bilang win → koreksi
                                s["wins"]   = max(0, s["wins"] + 1)
                                s["losses"] = max(0, s["losses"] - 1)
                                logger.info(f"[Sync] {sym} win/loss corrected: loss→win (Bybit PnL={real_pnl:+.4f})")
                            else:
                                # bot hitung win, Bybit bilang loss → koreksi
                                s["losses"] = max(0, s["losses"] + 1)
                                s["wins"]   = max(0, s["wins"] - 1)
                                logger.info(f"[Sync] {sym} win/loss corrected: win→loss (Bybit PnL={real_pnl:+.4f})")
                logger.info(
                    f"[Sync] Closed {sym}: exit={exit_price:.4f} pnl={real_pnl:+.4f}"
                )
            else:
                # No exit data — just remove from store
                logger.warning(
                    f"[Sync] {sym} missing on exchange but has no closed-PnL proof; "
                    "keeping tracker state for retry"
                )
                continue

            result["closed"].append(sym)

        # 2. Positions on Bybit but NOT in store → add them
        # Cooldown: skip add if symbol was just closed by tracker (<60s ago).
        # Bybit may take a few seconds to reflect the close, OR our local
        # SL/TP just fired but Bybit close-order is still in-flight.
        # Without cooldown: tracker closes → sync re-adds → loop forever.
        COOLDOWN_S = 300.0  # 5 min — long enough to prevent open→close→reopen loop
        now_t = time.time()
        # Cleanup stale cooldown entries
        self._recent_closes = {
            s: t for s, t in self._recent_closes.items()
            if now_t - t < COOLDOWN_S
        }

        for sym, bp in bybit_map.items():
            # Skip re-add if just closed locally (cooldown)
            if sym in self._recent_closes:
                age = now_t - self._recent_closes[sym]
                logger.info(
                    f"[Sync] Skip re-add {sym} (closed by tracker {age:.0f}s ago, "
                    f"cooldown {COOLDOWN_S:.0f}s) — Bybit position may still be settling"
                )
                continue

            action = "LONG" if bp["side"] == "long" else "SHORT"
            entry = float(bp["entryPrice"])
            leverage = int(float(bp.get("leverage", 1)))
            contracts = float(bp.get("contracts", 0))
            notional = round(contracts * entry, 2)

            if sym not in store_map:
                # Inherit SL/TP from Bybit if set (avoid orphan position with
                # no protection). bp.stopLoss=0 means Bybit has no SL → DANGER.
                bybit_sl = float(bp.get("stopLoss", 0) or 0)
                bybit_tp = float(bp.get("takeProfit", 0) or 0)
                tps_inherited = [bybit_tp] if bybit_tp > 0 else []

                store_map[sym] = {
                    "symbol": sym,
                    "position_id": f"external-{uuid.uuid4().hex[:12]}",
                    "action": action,
                    "entry_price": entry,
                    "stop_loss": bybit_sl,
                    "original_sl": bybit_sl,
                    "take_profit": tps_inherited,
                    "leverage": leverage,
                    "notional": notional,
                    "remaining_pct": 1.0,
                    "size_pct": 0.0,
                    "open_time": _now_str(),
                    "status": "synced_from_exchange",
                    "peak_price": entry,
                    "peak_at": 0.0,
                    "trough_price": entry,
                    "sl_kind": "initial",
                    "trailing_active": False,
                    "trailing_activation_price": 0.0,
                    "trailing_trail_pct": 3.0,
                    "tp_trail_active": False,
                    "tp_trail_target": 0.0,
                    "tp_trail_giveback_pct": 0.0,
                    "tp_trail_armed_at": 0.0,
                    "tp_trail_arm_price": 0.0,
                    "tp_hits": [],
                    "dca_count": 0,
                    "needs_sl_attach": bybit_sl <= 0,   # flag for main.py to set SL
                }
                sl_status = (
                    f"SL={bybit_sl:.6f}" if bybit_sl > 0
                    else "NO_SL_ON_BYBIT (DANGER — needs attach)"
                )
                logger.info(
                    f"[Sync] Added {action} {sym} from Bybit "
                    f"@ {entry:.4f} (lev={leverage}x, notional={notional:.2f}, {sl_status})"
                )
                result["added"].append(sym)
            else:
                # 3. Both have it → update selectively
                pos = store_map[sym]
                old_entry = pos["entry_price"]
                changed = False
                is_synced = pos.get("status") == "synced_from_exchange"

                # Only update entry_price for synced positions — never overwrite
                # bot-opened entry (Bybit avgPrice can differ due to merge/DCA)
                if is_synced and abs(old_entry - entry) / max(entry, 1) > 0.0001:
                    pos["entry_price"] = entry
                    changed = True

                if pos.get("leverage") != leverage:
                    pos["leverage"] = leverage
                    changed = True

                if pos["action"] != action:
                    pos["action"] = action
                    changed = True

                # Update notional to reflect actual size on exchange
                if abs(pos["notional"] - notional) / max(notional, 1) > 0.01:
                    pos["notional"] = notional
                    changed = True

                if changed:
                    entry_str = (
                        f"entry {old_entry:.4f}→{entry:.4f} "
                        if is_synced else f"entry={old_entry:.4f}(kept) "
                    )
                    logger.info(
                        f"[Sync] Updated {sym}: "
                        f"{entry_str}lev={leverage}x"
                    )
                    result["updated"].append(sym)

        _save_store(self._store)
        return result

    def reset(self) -> None:
        """Clear all data (use carefully)."""
        self._store = _load_store()
        self._store["open_positions"] = {}
        self._store["closed_trades"] = []
        self._store["stats"] = {
            "total_realized": 0.0, "total_trades": 0,
            "wins": 0, "losses": 0,
            "best_trade": 0.0, "worst_trade": 0.0,
            "best_symbol": "", "worst_symbol": "",
        }
        _save_store(self._store)
