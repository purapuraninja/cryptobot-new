"""Regression tests for dry-run costs and multi-leg PnL bookkeeping."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from config import Config
from utils.pnl_tracker import PnLTracker
from utils.trade_analytics import calculate_position_metrics


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


class DryRunCostAccountingTests(unittest.TestCase):
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
        self.config_patch = patch.multiple(
            Config,
            DRY_RUN=True,
            DRY_RUN_INCLUDE_COSTS=True,
            # Deliberately large values make direction and fee assertions clear.
            DRY_RUN_SLIPPAGE_PCT=0.10,
            TAKER_FEE_PCT=0.05,
            TRADING_MODE="intraday",
            STAGNANT_HOLD_HOURS=6.0,
            STAGNANT_ROI_PCT=5.0,
            MAX_HOLD_HOURS_INTRADAY=12.0,
            MAX_HOLD_HOURS_SWING=72.0,
            PARTIAL_CLOSE_ENABLED=True,
            TP1_CLOSE_PCT=20.0,
            TP2_CLOSE_PCT=25.0,
            PARTIAL_CLOSE_MIN_MOVE_PCT=0.20,
            BE_PROFIT_LOCK_ROI_PCT=5.0,
            BE_PROFIT_LOCK_PCT=0.30,
        )

        self.mock_load = self.load_patch.start()
        self.mock_save = self.save_patch.start()
        self.mock_now = self.now_patch.start()
        self.mock_log = self.log_patch.start()
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
                [110.0, 120.0, 130.0]
                if action == "LONG"
                else [90.0, 80.0, 70.0]
            ),
            "leverage": 10,
            "position_size_percent": 1.0,
            "score": 8.0,
            "confidence": 0.8,
        }

    def _tracker_with_entry(
        self, symbol: str = "BTCUSDT", action: str = "LONG"
    ) -> PnLTracker:
        tracker = PnLTracker(account_balance=10_000.0)
        tracker.record_entry(
            self._signal(symbol, action),
            {"status": "dry_run", "entry_price": 100.0},
        )
        return tracker

    def test_entry_slippage_is_adverse_for_both_sides(self):
        long_tracker = self._tracker_with_entry("LONGUSDT", "LONG")
        short_tracker = self._tracker_with_entry("SHORTUSDT", "SHORT")

        long_position = long_tracker._store["open_positions"]["LONGUSDT"]
        short_position = short_tracker._store["open_positions"]["SHORTUSDT"]

        self.assertAlmostEqual(long_position["planned_entry_price"], 100.0)
        self.assertAlmostEqual(long_position["entry_price"], 100.1)
        self.assertAlmostEqual(short_position["planned_entry_price"], 100.0)
        self.assertAlmostEqual(short_position["entry_price"], 99.9)
        self.assertTrue(long_position["costs_included"])
        self.assertTrue(short_position["costs_included"])

    def test_partial_then_final_close_books_fees_once_per_leg(self):
        tracker = self._tracker_with_entry()

        partial = tracker.partial_close_record(
            "BTCUSDT", exit_price=110.0, pct_to_close=20.0, reason="tp1_partial"
        )
        final = tracker.record_close(
            "BTCUSDT", exit_price=105.0, reason="trailing_stop"
        )

        self.assertIsNotNone(partial)
        self.assertIsNotNone(final)
        assert partial is not None and final is not None

        # A long exits below both requested prices after adverse slippage.
        self.assertAlmostEqual(partial["planned_exit_price"], 110.0)
        self.assertAlmostEqual(partial["exit_price"], 109.89)
        self.assertAlmostEqual(final["planned_exit_price"], 105.0)
        self.assertAlmostEqual(final["exit_price"], 104.895)

        for leg in (partial, final):
            self.assertGreater(leg["fees_usdt"], 0.0)
            self.assertAlmostEqual(
                leg["pnl_usdt"],
                round(leg["gross_pnl_usdt"] - leg["fees_usdt"], 4),
            )

        expected_net = round(partial["pnl_usdt"] + final["pnl_usdt"], 4)
        expected_gross = round(
            partial["gross_pnl_usdt"] + final["gross_pnl_usdt"], 4
        )
        expected_fees = round(partial["fees_usdt"] + final["fees_usdt"], 4)

        self.assertAlmostEqual(final["partial_pnl_usdt"], partial["pnl_usdt"])
        self.assertAlmostEqual(final["position_pnl_usdt"], expected_net)
        self.assertAlmostEqual(final["position_gross_pnl_usdt"], expected_gross)
        self.assertAlmostEqual(final["position_fees_usdt"], expected_fees)
        self.assertAlmostEqual(expected_net, round(expected_gross - expected_fees, 4))
        self.assertAlmostEqual(
            tracker._store["stats"]["total_realized"], expected_net
        )
        self.assertEqual(tracker._store["stats"]["total_trades"], 1)
        self.assertEqual(tracker._store["stats"]["wins"], 1)
        self.assertNotIn("BTCUSDT", tracker._store["open_positions"])

        metrics = calculate_position_metrics(tracker._store["closed_trades"])
        self.assertEqual(metrics["completed_trades"], 1)
        self.assertEqual(metrics["wins"], 1)
        self.assertEqual(metrics["losses"], 0)
        self.assertAlmostEqual(metrics["gross_pnl_usdt"], expected_net)
        self.assertAlmostEqual(metrics["expectancy_usdt"], expected_net)
        self.assertEqual(
            metrics["best_lifecycle"]["exit_legs"], 2
        )

    def test_failed_partial_is_persisted_and_reemitted_until_recorded(self):
        tracker = self._tracker_with_entry()
        market = [{"symbol": "BTCUSDT", "current_price": 110.0, "atr_pct": 1.0}]

        first_events = tracker.check_sl_tp(market)
        first_request = next(
            event
            for event in first_events
            if event.get("requires_partial_close")
        )
        self.assertEqual(first_request["tp_index"], 0)
        self.assertNotIn("retry", first_request)

        # This is what the caller does after the exchange reduce-only order fails.
        tracker.queue_partial_retry(
            first_request["symbol"],
            first_request["tp_index"],
            first_request["exit_price"],
            first_request["close_pct"],
            first_request["reason"],
        )
        pending = tracker._store["open_positions"]["BTCUSDT"][
            "pending_partial_closes"
        ]
        self.assertIn("0", pending)
        self.assertGreaterEqual(self.mock_save.call_count, 3)

        retry_events = tracker.check_sl_tp(market)
        retry = next(
            event
            for event in retry_events
            if event.get("requires_partial_close")
        )
        self.assertTrue(retry["retry"])
        self.assertEqual(retry["tp_index"], 0)
        self.assertEqual(retry["close_pct"], 20.0)
        self.assertIn(
            "0",
            tracker._store["open_positions"]["BTCUSDT"][
                "pending_partial_closes"
            ],
        )

        # Recording the successful retry atomically consumes the queue item.
        recorded = tracker.partial_close_record(
            retry["symbol"],
            retry["exit_price"],
            retry["close_pct"],
            retry["reason"],
        )
        self.assertIsNotNone(recorded)
        self.assertNotIn(
            "0",
            tracker._store["open_positions"]["BTCUSDT"][
                "pending_partial_closes"
            ],
        )


if __name__ == "__main__":
    unittest.main()
