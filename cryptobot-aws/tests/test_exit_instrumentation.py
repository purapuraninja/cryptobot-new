"""Regression tests for exit instrumentation (MFE / MAE / capture ratio).

These fields exist to make trail-width tuning measurable.  Without them,
"is the trail cutting winners too early?" is unanswerable: closed_trades only
records where a position exited, never how far price had already travelled.
"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from config import Config
from utils.pnl_tracker import PnLTracker


def _empty_store() -> dict:
    return {
        "open_positions": {},
        "closed_trades": [],
        "stats": {
            "total_realized": 0.0, "total_trades": 0, "wins": 0, "losses": 0,
            "flats": 0, "best_trade": 0.0, "worst_trade": 0.0,
            "best_symbol": "", "worst_symbol": "",
        },
    }


class ExitInstrumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_store = _empty_store()
        self.load_patch = patch(
            "utils.pnl_tracker._load_store",
            side_effect=lambda: copy.deepcopy(self.saved_store),
        )
        self.save_patch = patch("utils.pnl_tracker._save_store")
        self.now_patch = patch(
            "utils.pnl_tracker._now_str",
            return_value=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.log_patch = patch("utils.pnl_tracker.logger.info")
        # Costs off so entry/exit stay exact and the percentages are readable.
        self.config_patch = patch.multiple(
            Config,
            DRY_RUN=True,
            DRY_RUN_INCLUDE_COSTS=False,
            DRY_RUN_SLIPPAGE_PCT=0.0,
            TAKER_FEE_PCT=0.05,
            TRADING_MODE="intraday",
            MAX_HOLD_HOURS_INTRADAY=999.0,
            MAX_HOLD_HOURS_SWING=999.0,
            PARTIAL_CLOSE_ENABLED=True,
            TP1_CLOSE_PCT=15.0,
            TP2_CLOSE_PCT=24.0,
            TRAILING_TP_ENABLED=True,
        )
        self.load_patch.start()
        self.save_patch.start()
        self.now_patch.start()
        self.log_patch.start()
        self.config_patch.start()
        for p in (self.load_patch, self.save_patch, self.now_patch,
                  self.log_patch, self.config_patch):
            self.addCleanup(p.stop)

    @staticmethod
    def _signal(action: str = "LONG", symbol: str = "BTCUSDT") -> dict:
        return {
            "symbol": symbol,
            "action": action,
            "entry_price": 100.0,
            "stop_loss": 90.0 if action == "LONG" else 110.0,
            "take_profit": (
                [110.0, 120.0, 130.0] if action == "LONG" else [90.0, 80.0, 70.0]
            ),
            "leverage": 5,
            "position_size_percent": 1.0,
            "score": 8.0,
            "confidence": 0.8,
        }

    def _tracker(self, action: str = "LONG", symbol: str = "BTCUSDT") -> PnLTracker:
        tracker = PnLTracker(account_balance=10_000.0)
        tracker.record_entry(
            self._signal(action, symbol),
            {"status": "dry_run", "entry_price": 100.0,
             "stop_loss": 90.0 if action == "LONG" else 110.0},
        )
        return tracker

    @staticmethod
    def _mkt(price: float, symbol: str = "BTCUSDT", atr: float = 1.0) -> list:
        return [{"symbol": symbol, "current_price": price, "atr_pct": atr}]

    # ── Baseline fields ───────────────────────────────────────

    def test_entry_seeds_peak_and_trough_at_entry_price(self):
        tracker = self._tracker()
        pos = tracker._store["open_positions"]["BTCUSDT"]
        self.assertAlmostEqual(pos["peak_price"], 100.0)
        self.assertAlmostEqual(pos["trough_price"], 100.0)

    # ── LONG ──────────────────────────────────────────────────

    def test_long_records_mfe_mae_and_capture(self):
        tracker = self._tracker()
        tracker.check_sl_tp(self._mkt(105.0))   # favourable excursion
        tracker.check_sl_tp(self._mkt(95.0))    # adverse excursion, SL=90 safe

        pos = tracker._store["open_positions"]["BTCUSDT"]
        self.assertAlmostEqual(pos["peak_price"], 105.0)
        self.assertAlmostEqual(pos["trough_price"], 95.0)

        rec = tracker.record_close("BTCUSDT", exit_price=102.0, reason="manual")
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertAlmostEqual(rec["mfe_pct"], 5.0, places=4)
        self.assertAlmostEqual(rec["mae_pct"], 5.0, places=4)
        self.assertAlmostEqual(rec["realized_move_pct"], 2.0, places=4)
        self.assertAlmostEqual(rec["capture_ratio"], 0.4, places=4)

    def test_long_full_capture_when_exiting_at_the_peak(self):
        tracker = self._tracker()
        tracker.check_sl_tp(self._mkt(105.0))
        rec = tracker.record_close("BTCUSDT", exit_price=105.0, reason="manual")
        assert rec is not None
        self.assertAlmostEqual(rec["capture_ratio"], 1.0, places=4)
        self.assertAlmostEqual(rec["mae_pct"], 0.0, places=4)

    # ── SHORT ─────────────────────────────────────────────────

    def test_short_mirrors_the_long_maths(self):
        tracker = self._tracker("SHORT", "ETHUSDT")
        tracker.check_sl_tp(self._mkt(95.0, "ETHUSDT"))    # favourable for SHORT
        tracker.check_sl_tp(self._mkt(105.0, "ETHUSDT"))   # adverse, SL=110 safe

        pos = tracker._store["open_positions"]["ETHUSDT"]
        self.assertAlmostEqual(pos["peak_price"], 95.0)
        self.assertAlmostEqual(pos["trough_price"], 105.0)

        rec = tracker.record_close("ETHUSDT", exit_price=98.0, reason="manual")
        assert rec is not None
        self.assertAlmostEqual(rec["mfe_pct"], 5.0, places=4)
        self.assertAlmostEqual(rec["mae_pct"], 5.0, places=4)
        self.assertAlmostEqual(rec["realized_move_pct"], 2.0, places=4)
        self.assertAlmostEqual(rec["capture_ratio"], 0.4, places=4)

    # ── Edge cases ────────────────────────────────────────────

    def test_capture_ratio_is_none_when_price_never_went_favourable(self):
        """Division by a zero MFE must not produce inf/NaN in the store."""
        tracker = self._tracker()
        rec = tracker.record_close("BTCUSDT", exit_price=99.0, reason="stop_loss")
        assert rec is not None
        self.assertAlmostEqual(rec["mfe_pct"], 0.0, places=4)
        self.assertIsNone(rec["capture_ratio"])
        self.assertAlmostEqual(rec["realized_move_pct"], -1.0, places=4)

    def test_mfe_and_mae_are_never_negative(self):
        tracker = self._tracker()
        tracker.check_sl_tp(self._mkt(95.0))
        rec = tracker.record_close("BTCUSDT", exit_price=95.0, reason="stop_loss")
        assert rec is not None
        self.assertGreaterEqual(rec["mfe_pct"], 0.0)
        self.assertGreaterEqual(rec["mae_pct"], 0.0)

    def test_trail_exit_shows_the_giveback_as_lost_capture(self):
        """The whole point: a trailed-out winner must show capture < 1."""
        tracker = self._tracker()
        tracker.check_sl_tp(self._mkt(110.0))   # TP1 -> BE lock + trail armed
        tracker.check_sl_tp(self._mkt(118.0))   # peak advances

        pos = tracker._store["open_positions"]["BTCUSDT"]
        self.assertAlmostEqual(pos["peak_price"], 118.0)
        trail_sl = pos["stop_loss"]
        self.assertGreater(trail_sl, 100.0, "trail should sit in profit")

        rec = tracker.record_close("BTCUSDT", exit_price=trail_sl, reason="trailing_stop")
        assert rec is not None
        self.assertAlmostEqual(rec["mfe_pct"], 18.0, places=4)
        self.assertLess(rec["capture_ratio"], 1.0)
        self.assertGreater(rec["capture_ratio"], 0.0)

    def test_position_stored_before_the_upgrade_still_closes(self):
        """Positions already open when this shipped have no trough_price."""
        tracker = self._tracker()
        tracker.check_sl_tp(self._mkt(105.0))
        pos = tracker._store["open_positions"]["BTCUSDT"]
        pos.pop("trough_price", None)

        rec = tracker.record_close("BTCUSDT", exit_price=104.0, reason="manual")
        assert rec is not None
        # Falls back to entry, so MAE reads 0 rather than crashing.
        self.assertAlmostEqual(rec["mae_pct"], 0.0, places=4)
        self.assertAlmostEqual(rec["mfe_pct"], 5.0, places=4)


if __name__ == "__main__":
    unittest.main()
