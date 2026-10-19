"""Regression tests for the trailing take-profit mechanism.

TP3 no longer closes the position at a fixed price.  It arms a trailing TP:
the target ratchets with price and the exit happens when price gives back
`giveback_pct` from the peak, hits the max-extension wall, or stalls.
"""

from __future__ import annotations

import copy
import time
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
            "total_realized": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "best_symbol": "",
            "worst_symbol": "",
        },
    }


class TrailingTakeProfitTests(unittest.TestCase):
    """LONG entry @100, SL 90 (risk = 10), TPs at 110 / 120 / 130."""

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
        # Costs off keeps entry at exactly 100.0 so risk math stays readable.
        self.config_patch = patch.multiple(
            Config,
            DRY_RUN=True,
            DRY_RUN_INCLUDE_COSTS=False,
            DRY_RUN_SLIPPAGE_PCT=0.0,
            TAKER_FEE_PCT=0.05,
            TRADING_MODE="swing",
            STAGNANT_HOLD_HOURS=6.0,
            STAGNANT_ROI_PCT=5.0,
            MAX_HOLD_HOURS_INTRADAY=12.0,
            MAX_HOLD_HOURS_SWING=72.0,
            PARTIAL_CLOSE_ENABLED=True,
            TP1_CLOSE_PCT=15.0,
            TP2_CLOSE_PCT=24.0,
            PARTIAL_CLOSE_MIN_MOVE_PCT=0.20,
            TP1_PARTIAL_MIN_R=0.5,
            BE_PROFIT_LOCK_ROI_PCT=5.0,
            BE_PROFIT_LOCK_PCT=0.30,
            TP2_TRAIL_TIGHTEN_MULT=0.75,
            TRAILING_TP_ENABLED=True,
            TRAILING_TP_GIVEBACK_ATR_MULT=1.5,
            TRAILING_TP_MIN_GIVEBACK_PCT=0.60,
            TRAILING_TP_MAX_GIVEBACK_PCT=5.0,
            TRAILING_TP_STEP_ATR_MULT=1.0,
            TRAILING_TP_MAX_EXTENSION_R=4.0,
            TRAILING_TP_STALL_HOURS=8.0,
        )

        self.load_patch.start()
        self.mock_save = self.save_patch.start()
        self.now_patch.start()
        self.log_patch.start()
        self.config_patch.start()
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.save_patch.stop)
        self.addCleanup(self.now_patch.stop)
        self.addCleanup(self.log_patch.stop)
        self.addCleanup(self.config_patch.stop)

    @staticmethod
    def _signal(symbol: str = "BTCUSDT", action: str = "LONG") -> dict:
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

    def _tracker(self, symbol: str = "BTCUSDT", action: str = "LONG") -> PnLTracker:
        tracker = PnLTracker(account_balance=10_000.0)
        tracker.record_entry(
            self._signal(symbol, action),
            {"status": "dry_run", "entry_price": 100.0, "stop_loss": 90.0},
        )
        return tracker

    @staticmethod
    def _mkt(price: float, symbol: str = "BTCUSDT", atr_pct: float = 1.0) -> list:
        return [{"symbol": symbol, "current_price": price, "atr_pct": atr_pct}]

    def _arm(self, symbol: str = "BTCUSDT", action: str = "LONG"):
        """Drive price to TP3 so the trailing TP arms.  Returns (tracker, events)."""
        tracker = self._tracker(symbol, action)
        tp3 = 130.0 if action == "LONG" else 70.0
        events = tracker.check_sl_tp(self._mkt(tp3, symbol))
        return tracker, events

    # ── Arming ────────────────────────────────────────────────

    def test_tp3_arms_trailing_tp_instead_of_closing(self):
        tracker, events = self._arm()

        self.assertNotIn(
            "BTCUSDT",
            {e["symbol"] for e in events if e.get("requires_close")},
            "TP3 must no longer request a full close",
        )
        self.assertIn("BTCUSDT", tracker._store["open_positions"])

        armed = [e for e in events if e.get("reason") == "tp_trail_armed"]
        self.assertEqual(len(armed), 1)
        self.assertTrue(armed[0]["milestone"])
        self.assertEqual(armed[0]["pnl_usdt"], 0.0)

        pos = tracker._store["open_positions"]["BTCUSDT"]
        self.assertTrue(pos["tp_trail_active"])
        self.assertIn(2, pos["tp_hits"])
        # giveback = 1.0% ATR x 1.5 = 1.5%
        self.assertAlmostEqual(pos["tp_trail_giveback_pct"], 1.5)
        # floor = 130 x (1 - 1.5%),  next target = 130 x (1 + 1.0%)
        self.assertAlmostEqual(pos["stop_loss"], 128.05, places=4)
        self.assertEqual(pos["sl_kind"], "tp_trail")
        self.assertAlmostEqual(pos["tp_trail_target"], 131.3, places=4)

    def test_giveback_is_clamped_to_configured_bounds(self):
        # Huge ATR would otherwise hand back a double-digit percentage.
        self.assertAlmostEqual(PnLTracker._calc_tp_giveback_pct(20.0), 5.0)
        # Near-zero ATR must still leave room for noise.
        self.assertAlmostEqual(PnLTracker._calc_tp_giveback_pct(0.01), 0.60)

    def test_arming_never_lowers_existing_protection(self):
        tracker, _ = self._arm()
        pos = tracker._store["open_positions"]["BTCUSDT"]
        self.assertGreater(
            pos["stop_loss"],
            pos["original_sl"],
            "trailing TP floor must ratchet above the initial SL",
        )
        self.assertGreater(pos["stop_loss"], pos["entry_price"])

    # ── Ratcheting ────────────────────────────────────────────

    def test_target_and_floor_follow_price_upward(self):
        tracker, _ = self._arm()

        tracker.check_sl_tp(self._mkt(140.0))
        pos = tracker._store["open_positions"]["BTCUSDT"]
        self.assertAlmostEqual(pos["peak_price"], 140.0)
        self.assertAlmostEqual(pos["tp_trail_target"], 141.4, places=4)
        self.assertAlmostEqual(pos["stop_loss"], 137.9, places=4)

    def test_target_and_floor_never_retreat_on_pullback(self):
        tracker, _ = self._arm()
        tracker.check_sl_tp(self._mkt(140.0))
        target_at_peak = tracker._store["open_positions"]["BTCUSDT"]["tp_trail_target"]
        floor_at_peak = tracker._store["open_positions"]["BTCUSDT"]["stop_loss"]

        # Pull back, but stay above the floor.
        events = tracker.check_sl_tp(self._mkt(138.5))
        pos = tracker._store["open_positions"]["BTCUSDT"]

        self.assertEqual(pos["tp_trail_target"], target_at_peak)
        self.assertEqual(pos["stop_loss"], floor_at_peak)
        self.assertFalse([e for e in events if e.get("requires_close")])
        self.assertIn("BTCUSDT", tracker._store["open_positions"])

    # ── Exits ─────────────────────────────────────────────────

    def test_giveback_breach_exits_as_profit_not_stop_loss(self):
        tracker, _ = self._arm()
        tracker.check_sl_tp(self._mkt(140.0))   # floor -> 137.9

        events = tracker.check_sl_tp(self._mkt(137.0))
        closes = [e for e in events if e.get("requires_close")]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "trailing_take_profit")
        self.assertEqual(closes[0]["close_pct"], 100.0)
        # Fill is the worse of (floor, observed) — price gapped through.
        self.assertAlmostEqual(closes[0]["exit_price"], 137.0, places=4)

    def test_max_extension_wall_takes_the_profit(self):
        with patch.object(Config, "TRAILING_TP_MAX_EXTENSION_R", 0.5):
            tracker, _ = self._arm()
            # cap = tp3 130 + 0.5 x risk 10 = 135
            events = tracker.check_sl_tp(self._mkt(136.0))

        closes = [e for e in events if e.get("requires_close")]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "tp_trail_max_extension")
        self.assertEqual(closes[0]["close_pct"], 100.0)

    def test_target_stops_advancing_past_the_extension_cap(self):
        with patch.object(Config, "TRAILING_TP_MAX_EXTENSION_R", 1.0):
            tracker, _ = self._arm()
            # cap = 130 + 1.0 x 10 = 140.  Peak 139 would imply target 140.39.
            tracker.check_sl_tp(self._mkt(139.0))
            pos = tracker._store["open_positions"]["BTCUSDT"]
            self.assertAlmostEqual(pos["tp_trail_target"], 140.0, places=4)

    def test_stalled_runner_is_closed_even_though_it_is_profitable(self):
        tracker, _ = self._arm()
        pos = tracker._store["open_positions"]["BTCUSDT"]
        # No new peak for 9h (> TRAILING_TP_STALL_HOURS=8).
        pos["peak_at"] = time.time() - (9 * 3600)

        # 129 is below the peak (130) but above the floor (128.05), so the
        # only thing that can close this position is the stall guard.
        events = tracker.check_sl_tp(self._mkt(129.0))
        closes = [e for e in events if e.get("requires_close")]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "tp_trail_stall")

    def test_floor_breach_outranks_the_stall_guard(self):
        """A stalled runner that also broke the floor is a TTP exit, not a stall.

        The stall reason would both mis-attribute the exit and fill at the
        observed price instead of worse-of(floor, observed).
        """
        tracker, _ = self._arm()
        pos = tracker._store["open_positions"]["BTCUSDT"]
        pos["peak_at"] = time.time() - (9 * 3600)

        # 127 is below the floor (128.05) and the position is also stalled.
        events = tracker.check_sl_tp(self._mkt(127.0))
        closes = [e for e in events if e.get("requires_close")]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "trailing_take_profit")
        self.assertAlmostEqual(closes[0]["exit_price"], 127.0, places=4)

    def test_new_peak_resets_the_stall_clock(self):
        tracker, _ = self._arm()
        pos = tracker._store["open_positions"]["BTCUSDT"]
        pos["peak_at"] = time.time() - (9 * 3600)

        events = tracker.check_sl_tp(self._mkt(135.0))   # new peak
        self.assertFalse([e for e in events if e.get("requires_close")])
        self.assertIn("BTCUSDT", tracker._store["open_positions"])

    # ── SHORT side ────────────────────────────────────────────

    def test_short_side_arms_and_trails_downward(self):
        tracker, events = self._arm("ETHUSDT", "SHORT")
        self.assertFalse([e for e in events if e.get("requires_close")])

        pos = tracker._store["open_positions"]["ETHUSDT"]
        self.assertTrue(pos["tp_trail_active"])
        self.assertAlmostEqual(pos["stop_loss"], 71.05, places=4)      # 70 x 1.015
        self.assertAlmostEqual(pos["tp_trail_target"], 69.3, places=4)  # 70 x 0.99

        tracker.check_sl_tp(self._mkt(65.0, "ETHUSDT"))
        pos = tracker._store["open_positions"]["ETHUSDT"]
        self.assertAlmostEqual(pos["peak_price"], 65.0)
        self.assertAlmostEqual(pos["stop_loss"], 65.975, places=4)
        self.assertAlmostEqual(pos["tp_trail_target"], 64.35, places=4)

        events = tracker.check_sl_tp(self._mkt(66.5, "ETHUSDT"))
        closes = [e for e in events if e.get("requires_close")]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "trailing_take_profit")

    # ── Legacy escape hatch ───────────────────────────────────

    def test_disabling_the_flag_restores_the_fixed_tp3_close(self):
        with patch.object(Config, "TRAILING_TP_ENABLED", False):
            tracker, events = self._arm()

        closes = [e for e in events if e.get("requires_close")]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["reason"], "take_profit")
        self.assertAlmostEqual(closes[0]["exit_price"], 130.0)
        self.assertFalse(
            tracker._store["open_positions"]["BTCUSDT"].get("tp_trail_active", False)
        )


    # ── Backward compatibility ────────────────────────────────

    def test_position_stored_before_the_upgrade_still_arms(self):
        """Positions already open when this shipped lack the new fields."""
        tracker = self._tracker()
        pos = tracker._store["open_positions"]["BTCUSDT"]
        for legacy_absent in (
            "tp_trail_active",
            "tp_trail_target",
            "tp_trail_giveback_pct",
            "tp_trail_armed_at",
            "tp_trail_arm_price",
            "peak_at",
        ):
            pos.pop(legacy_absent, None)

        # Jumping straight to TP3 fires all three milestones in one scan.
        events = tracker.check_sl_tp(self._mkt(130.0))
        self.assertEqual(
            [e["reason"] for e in events if e.get("milestone")],
            ["be_activated", "sl_advanced", "tp_trail_armed"],
        )
        self.assertFalse([e for e in events if e.get("requires_close")])
        pos = tracker._store["open_positions"]["BTCUSDT"]
        self.assertTrue(pos["tp_trail_active"])
        self.assertAlmostEqual(pos["stop_loss"], 128.05, places=4)

    def test_armed_position_missing_giveback_recovers_it_from_atr(self):
        """A store written mid-upgrade could have the flag but no giveback."""
        tracker = self._tracker()
        pos = tracker._store["open_positions"]["BTCUSDT"]
        pos["tp_trail_active"] = True
        pos["tp_trail_giveback_pct"] = 0.0
        pos["tp_trail_arm_price"] = 130.0
        pos["peak_price"] = 130.0
        pos["peak_at"] = time.time()

        tracker.check_sl_tp(self._mkt(130.0))
        pos = tracker._store["open_positions"]["BTCUSDT"]
        self.assertAlmostEqual(pos["tp_trail_giveback_pct"], 1.5)
        self.assertAlmostEqual(pos["stop_loss"], 128.05, places=4)


class TrailingTakeProfitConfigTests(unittest.TestCase):
    def test_inverted_giveback_bounds_are_rejected(self):
        with patch.multiple(
            Config,
            TRAILING_TP_ENABLED=True,
            TRAILING_TP_MIN_GIVEBACK_PCT=6.0,
            TRAILING_TP_MAX_GIVEBACK_PCT=5.0,
        ):
            with self.assertRaises(ValueError) as ctx:
                Config.validate()
            self.assertIn("TRAILING_TP_MIN_GIVEBACK_PCT", str(ctx.exception))

    def test_negative_stall_hours_are_rejected(self):
        with patch.multiple(
            Config,
            TRAILING_TP_ENABLED=True,
            TRAILING_TP_STALL_HOURS=-1.0,
        ):
            with self.assertRaises(ValueError) as ctx:
                Config.validate()
            self.assertIn("TRAILING_TP_STALL_HOURS", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
