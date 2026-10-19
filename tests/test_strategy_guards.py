"""Focused regression tests for the deterministic strategy guardrails."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from agent.filters import (
    passes_vol_regime_filter,
    symbol_side_loss_streak,
    symbol_trade_count_today,
)
from config import Config
from risk.decision_engine import (
    apply_expectancy_filter_and_sizing,
    apply_max_loss_cap,
    calculate_weighted_rr,
)


class WeightedRewardRiskTests(unittest.TestCase):
    def test_tp2_is_a_fraction_of_the_remaining_position(self):
        signal = {
            "action": "LONG",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": [102.0, 104.0, 110.0],
        }

        with patch.multiple(Config, TP1_CLOSE_PCT=15.0, TP2_CLOSE_PCT=24.0):
            rr, reward, risk, weights = calculate_weighted_rr(signal)

        expected_weights = [0.15, 0.85 * 0.24, 0.85 * 0.76]
        expected_reward = sum(
            move * weight
            for move, weight in zip((2.0, 4.0, 10.0), expected_weights)
        )
        self.assertEqual(len(weights), 3)
        for actual, expected in zip(weights, expected_weights):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(sum(weights), 1.0)
        self.assertAlmostEqual(risk, 2.0)
        self.assertAlmostEqual(reward, expected_reward)
        self.assertAlmostEqual(rr, expected_reward / risk)


class ExpectancySizingTests(unittest.TestCase):
    def _config_patch(self, **overrides):
        values = {
            "EXPECTANCY_FILTER_ENABLED": True,
            "BLOCK_HIGH_RISK_TRADES": True,
            "HIGH_RISK_SIZE_MULT": 0.5,
            "SCORE_LT_75_SIZE_MULT": 0.5,
            "SCORE_85_95_MEDIUM_SIZE_MULT": 1.0,
            "SCORE_HIGH_THRESHOLD": 9.0,
            "SCORE_HIGH_SIZE_MULT": 0.75,
            "SHORT_DOWNTREND_SIZE_MULT": 1.0,
            "LONG_SIDEWAYS_SIZE_MULT": 1.0,
            "LATE_CHASE_SIZE_MULT": 0.25,
            "MIN_POSITION_SIZE_PCT": 0.1,
            "MAX_POSITION_SIZE_PCT": 3.0,
        }
        values.update(overrides)
        return patch.multiple(Config, **values)

    def test_high_score_is_downsized_without_late_chase(self):
        signal = {
            "symbol": "BTCUSDT",
            "action": "LONG",
            "risk_level": "MEDIUM",
            "score": 9.2,
            "position_size_percent": 2.0,
        }
        snapshot = {
            "market_structure": "uptrend",
            "premium_discount": "equilibrium",
            "ema20_distance_pct": 0.5,
        }

        with self._config_patch():
            result = apply_expectancy_filter_and_sizing(signal, snapshot, 10_000.0)

        self.assertTrue(result.allowed)
        self.assertFalse(result.details["late_chase"])
        self.assertAlmostEqual(result.size_after, 1.5)
        self.assertAlmostEqual(signal["position_size_percent"], 1.5)

    def test_late_chase_is_downsized_without_a_hard_reject(self):
        signal = {
            "symbol": "ETHUSDT",
            "action": "LONG",
            "risk_level": "MEDIUM",
            "score": 8.0,
            "position_size_percent": 2.0,
        }
        snapshot = {
            "market_structure": "uptrend",
            "premium_discount": "premium",
            "ema20_distance_pct": 3.5,
            "swept_prev_low": False,
            "bos_bullish": False,
            "long_retest_zone": False,
        }

        with self._config_patch():
            result = apply_expectancy_filter_and_sizing(signal, snapshot, 10_000.0)

        self.assertTrue(result.allowed)
        self.assertTrue(result.details["late_chase"])
        self.assertIn("late_chase", result.reason)
        self.assertAlmostEqual(result.size_after, 0.5)
        self.assertAlmostEqual(signal["position_size_percent"], 0.5)


class MaxLossCapTests(unittest.TestCase):
    def test_equity_cap_and_round_trip_costs_both_reduce_size(self):
        signal = {
            "symbol": "SOLUSDT",
            "action": "LONG",
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "leverage": 10,
            "position_size_percent": 2.0,
        }
        config_values = {
            "MAX_LOSS_USDT_PER_TRADE": 10.0,
            # 0.05% of $10,000 is $5, tighter than the absolute $10 cap.
            "MAX_RISK_PCT_PER_TRADE": 0.05,
            "TAKER_FEE_PCT": 0.055,
            "DRY_RUN_SLIPPAGE_PCT": 0.02,
            "MIN_SL_DISTANCE_PCT": 0.4,
            "MIN_POSITION_SIZE_PCT": 0.1,
        }

        with patch.multiple(Config, **config_values):
            result = apply_max_loss_cap(signal, 10_000.0, {})

        sl_fraction = 0.01
        cost_fraction = 2.0 * (0.055 + 0.02) / 100.0
        expected_size = round(
            5.0 / (10_000.0 * 10.0 * (sl_fraction + cost_fraction)) * 100.0,
            4,
        )
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.details["round_trip_cost_frac"], cost_fraction)
        self.assertAlmostEqual(result.size_after, expected_size)
        self.assertAlmostEqual(signal["position_size_percent"], expected_size)
        self.assertIn("$5.00", result.reason)


class VolumeRegimeTests(unittest.TestCase):
    def test_falling_volume_gate_stays_active_when_neutral_band_is_disabled(self):
        signal = {"symbol": "XRPUSDT", "action": "SHORT"}
        snapshot = {
            "volume_condition": "normal",
            "volume_trend": "falling",
            "market_structure": "downtrend",
            "rsi": 35.0,
        }

        with patch.multiple(
            Config,
            REJECT_FALLING_VOLUME_ENTRIES=True,
            REGIME_RSI_NEUTRAL_BAND=0.0,
        ):
            self.assertFalse(passes_vol_regime_filter(signal, snapshot))

        with patch.multiple(
            Config,
            REJECT_FALLING_VOLUME_ENTRIES=False,
            REGIME_RSI_NEUTRAL_BAND=0.0,
        ):
            self.assertTrue(passes_vol_regime_filter(signal, snapshot))


class LifecycleCooldownTests(unittest.TestCase):
    _FMT = "%Y-%m-%d %H:%M:%S"

    @classmethod
    def _fmt(cls, value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime(cls._FMT)

    def test_daily_count_deduplicates_partial_legs_and_open_position(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        first_open = self._fmt(now - timedelta(hours=4))
        second_open = self._fmt(now - timedelta(hours=2))
        old_open = self._fmt(now - timedelta(days=1, hours=2))
        events = [
            {
                "symbol": "BTCUSDT",
                "action": "LONG",
                "open_time": first_open,
                "close_time": self._fmt(now - timedelta(hours=3, minutes=50)),
                "partial": True,
                "pnl_usdt": 2.0,
            },
            {
                "symbol": "BTCUSDT",
                "action": "LONG",
                "open_time": first_open,
                "close_time": self._fmt(now - timedelta(hours=3, minutes=30)),
                "partial": False,
                "pnl_usdt": -1.0,
            },
            {
                "symbol": "BTCUSDT",
                "action": "SHORT",
                "open_time": second_open,
                "close_time": self._fmt(now - timedelta(hours=1, minutes=30)),
                "partial": True,
                "pnl_usdt": 1.0,
            },
            {
                "symbol": "BTCUSDT",
                "action": "LONG",
                "open_time": old_open,
                "close_time": self._fmt(now - timedelta(days=1, hours=1)),
                "partial": False,
                "pnl_usdt": 1.0,
            },
            {
                "symbol": "ETHUSDT",
                "action": "LONG",
                "open_time": second_open,
                "close_time": self._fmt(now - timedelta(hours=1)),
                "partial": False,
                "pnl_usdt": 1.0,
            },
        ]
        pnl = SimpleNamespace(
            _store={
                "closed_trades": events,
                # The open position shares lifecycle two and must not add a slot.
                "open_positions": {
                    "BTCUSDT": {"open_time": second_open, "action": "SHORT"}
                },
            }
        )

        self.assertEqual(symbol_trade_count_today(pnl, "BTCUSDT"), 2)

    def test_loss_streak_is_position_level_and_scoped_to_symbol_side(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def lifecycle(
            action: str,
            close_offset: timedelta,
            partial_pnl: float,
            final_pnl: float,
        ) -> list[dict]:
            close = now - close_offset
            opened = self._fmt(close - timedelta(minutes=30))
            return [
                {
                    "symbol": "BTCUSDT",
                    "action": action,
                    "open_time": opened,
                    "close_time": self._fmt(close - timedelta(minutes=10)),
                    "partial": True,
                    "pnl_usdt": partial_pnl,
                },
                {
                    "symbol": "BTCUSDT",
                    "action": action,
                    "open_time": opened,
                    "close_time": self._fmt(close),
                    "partial": False,
                    "pnl_usdt": final_pnl,
                },
            ]

        events = []
        events += lifecycle("LONG", timedelta(hours=4), 2.0, 3.0)   # +5 resets
        events += lifecycle("LONG", timedelta(hours=3), 1.0, -6.0)  # -5
        events += lifecycle("LONG", timedelta(hours=1), 1.0, -3.0)  # -2
        # A newer win on the opposite side must not reset the LONG streak.
        events += lifecycle("SHORT", timedelta(minutes=30), 1.0, 2.0)
        pnl = SimpleNamespace(_store={"closed_trades": events})

        streak, hours_since_latest = symbol_side_loss_streak(
            pnl, "BTCUSDT", "LONG"
        )

        self.assertEqual(streak, 2)
        self.assertGreaterEqual(hours_since_latest, 0.99)
        self.assertLess(hours_since_latest, 1.02)


if __name__ == "__main__":
    unittest.main()
