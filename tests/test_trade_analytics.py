"""Focused tests for position-level trade analytics."""

from __future__ import annotations

import math
import unittest

from utils.trade_analytics import (
    aggregate_position_trades,
    calculate_position_metrics,
)


class AggregatePositionTradesTests(unittest.TestCase):
    def test_partial_and_final_legs_are_one_lifecycle_with_position_totals(self):
        events = [
            {
                "symbol": "BTCUSDT",
                "action": "LONG",
                "open_time": "2026-07-01T00:00:00+00:00",
                "close_time": "2026-07-01T00:20:00+00:00",
                "partial": False,
                "pnl_usdt": -1.0,
                "notional": 300.0,
                "leverage": 10,
                "reason": "final stop",
            },
            {
                "symbol": "BTCUSDT",
                "action": "LONG",
                "open_time": "2026-07-01T00:00:00+00:00",
                "close_time": "2026-07-01T00:10:00+00:00",
                "partial": True,
                "pnl_usdt": 4.0,
                "notional": 200.0,
                "leverage": 10,
                "reason": "tp1",
            },
        ]

        positions = aggregate_position_trades(events)

        self.assertEqual(len(positions), 1)
        position = positions[0]
        self.assertEqual(position["reason"], "final stop")
        self.assertEqual(position["exit_legs"], 2)
        self.assertTrue(position["completed"])
        self.assertAlmostEqual(position["partial_pnl_usdt"], 4.0)
        self.assertAlmostEqual(position["position_pnl_usdt"], 3.0)
        self.assertAlmostEqual(position["position_margin_usdt"], 50.0)
        self.assertAlmostEqual(position["position_pnl_pct"], 6.0)

        # Aggregation must not rewrite event-level values in the input.
        self.assertNotIn("position_pnl_usdt", events[0])
        self.assertEqual(events[0]["pnl_usdt"], -1.0)

    def test_incomplete_partial_lifecycle_is_excluded_unless_requested(self):
        incomplete = {
            "symbol": "ETHUSDT",
            "action": "SHORT",
            "open_time": "2026-07-02T00:00:00+00:00",
            "close_time": "2026-07-02T00:10:00+00:00",
            "partial": True,
            "remaining_pct_after": 0.75,
            "pnl_usdt": 2.5,
            "notional": 100.0,
            "leverage": 5,
        }

        self.assertEqual(aggregate_position_trades([incomplete]), [])

        positions = aggregate_position_trades(
            [incomplete], include_incomplete=True
        )
        self.assertEqual(len(positions), 1)
        self.assertFalse(positions[0]["completed"])
        self.assertAlmostEqual(positions[0]["position_pnl_usdt"], 2.5)
        self.assertAlmostEqual(positions[0]["position_margin_usdt"], 20.0)

    def test_final_partial_can_mark_a_lifecycle_complete(self):
        events = [
            {
                "symbol": "SOLUSDT",
                "action": "LONG",
                "open_time": "2026-07-03T00:00:00+00:00",
                "close_time": "2026-07-03T00:05:00+00:00",
                "partial": True,
                "remaining_pct_after": 0.4,
                "pnl_usdt": 1.0,
            },
            {
                "symbol": "SOLUSDT",
                "action": "LONG",
                "open_time": "2026-07-03T00:00:00+00:00",
                "close_time": "2026-07-03T00:15:00+00:00",
                "partial": True,
                "remaining_pct_after": 0.0,
                "pnl_usdt": 2.0,
            },
        ]

        positions = aggregate_position_trades(events)

        self.assertEqual(len(positions), 1)
        self.assertTrue(positions[0]["completed"])
        self.assertEqual(positions[0]["exit_legs"], 2)
        self.assertAlmostEqual(positions[0]["position_pnl_usdt"], 3.0)
        self.assertAlmostEqual(positions[0]["partial_pnl_usdt"], 3.0)

    def test_position_id_prevents_same_second_lifecycles_from_merging(self):
        base = {
            "symbol": "XRPUSDT",
            "action": "SHORT",
            "open_time": "2026-07-03 10:00:00",
            "close_time": "2026-07-03 10:05:00",
            "partial": False,
            "notional": 100.0,
            "leverage": 5,
        }
        events = [
            {**base, "position_id": "pos-a", "pnl_usdt": 2.0},
            {**base, "position_id": "pos-b", "pnl_usdt": -1.0},
        ]

        positions = aggregate_position_trades(events)

        self.assertEqual(len(positions), 2)
        self.assertEqual(
            {position["position_id"] for position in positions}, {"pos-a", "pos-b"}
        )


class CalculatePositionMetricsTests(unittest.TestCase):
    @staticmethod
    def _closed(symbol: str, close_minute: int, pnl: float) -> dict:
        return {
            "symbol": symbol,
            "action": "LONG",
            "open_time": f"2026-07-04T00:{close_minute - 1:02d}:00+00:00",
            "close_time": f"2026-07-04T00:{close_minute:02d}:00+00:00",
            "partial": False,
            "pnl_usdt": pnl,
            "notional": 100.0,
            "leverage": 10,
        }

    def test_metrics_count_flats_and_compute_profit_factor_expectancy_drawdown(self):
        # Chronological equity: 10 -> 6 -> 6 -> -2.  The peak-to-trough
        # drawdown is therefore 12, including the terminal loss.
        events = [
            self._closed("WIN", 1, 10.0),
            self._closed("LOSS1", 2, -4.0),
            self._closed("FLAT", 3, 0.0),
            self._closed("LOSS2", 4, -8.0),
        ]

        metrics = calculate_position_metrics(events)

        self.assertEqual(metrics["completed_trades"], 4)
        self.assertEqual(metrics["wins"], 1)
        self.assertEqual(metrics["losses"], 2)
        self.assertEqual(metrics["flats"], 1)
        self.assertAlmostEqual(metrics["gross_profit_usdt"], 10.0)
        self.assertAlmostEqual(metrics["gross_loss_usdt"], -12.0)
        self.assertAlmostEqual(metrics["gross_pnl_usdt"], -2.0)
        self.assertAlmostEqual(metrics["profit_factor"], 10.0 / 12.0)
        self.assertAlmostEqual(metrics["expectancy_usdt"], -0.5)
        self.assertAlmostEqual(metrics["max_drawdown_usdt"], 12.0)
        self.assertEqual(metrics["best_lifecycle"]["symbol"], "WIN")
        self.assertEqual(metrics["worst_lifecycle"]["symbol"], "LOSS2")

    def test_profit_factor_is_infinite_when_there_are_no_losses(self):
        metrics = calculate_position_metrics(
            [self._closed("WIN", 1, 3.0), self._closed("FLAT", 2, 0.0)]
        )

        self.assertTrue(math.isinf(metrics["profit_factor"]))
        self.assertAlmostEqual(metrics["max_drawdown_usdt"], 0.0)


if __name__ == "__main__":
    unittest.main()
