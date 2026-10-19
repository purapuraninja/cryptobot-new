"""Regression tests for the OKX executor.

Focus is the provider-specific logic that has no Bybit equivalent and would
fail silently in live trading:

  * size is in CONTRACTS, not coins (ctVal conversion)
  * OKX signals business failures with HTTP 200 + non-zero code/sCode
  * posSide is mandatory in hedge mode and rejected in net mode
  * the SL math must stay identical to Bybit's

No network: every test drives pure helpers or patches the HTTP layer.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from config import Config
from execution.base_executor import BaseExecutor
from execution.cex_executor import CEXExecutor
from execution.okx_executor import OKXExecutor


class _FakeResponse:
    """Stand-in for requests.Response carrying an OKX envelope."""

    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        if self._payload is _INVALID_JSON:
            raise ValueError("no json")
        return self._payload


_INVALID_JSON = object()


class SymbolConversionTests(unittest.TestCase):
    def test_inbound_formats_all_normalise_to_swap(self):
        for raw in ("BTCUSDT", "btcusdt", "BTC/USDT:USDT", "BTC-USDT-SWAP"):
            self.assertEqual(OKXExecutor._to_okx(raw), "BTC-USDT-SWAP", raw)

    def test_outbound_matches_the_tracker_key_format(self):
        # The tracker keys positions as BTCUSDT; a leaked instId would create a
        # second, orphaned position for the same market.
        self.assertEqual(OKXExecutor._from_okx("BTC-USDT-SWAP"), "BTCUSDT")
        self.assertEqual(OKXExecutor._from_okx("1000PEPE-USDT-SWAP"), "1000PEPEUSDT")

    def test_round_trip_is_stable(self):
        for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            self.assertEqual(
                OKXExecutor._from_okx(OKXExecutor._to_okx(sym)), sym
            )


class ContractConversionTests(unittest.TestCase):
    """The highest-risk difference from Bybit: 1 contract != 1 coin."""

    def setUp(self) -> None:
        self.ex = OKXExecutor()
        self.ex._markets = {
            # ETH swap: 0.1 ETH per contract, whole-contract lots.
            "ETH-USDT-SWAP": {
                "ct_val": 0.1, "ct_mult": 1.0, "lot_size": 1.0,
                "min_size": 1.0, "tick_size": 0.01, "max_leverage": 10.0,
            },
            # A 10-coin-per-contract instrument with fractional lots.
            "DOGE-USDT-SWAP": {
                "ct_val": 10.0, "ct_mult": 1.0, "lot_size": 0.1,
                "min_size": 1.0, "tick_size": 0.00001, "max_leverage": 10.0,
            },
        }
        self.ex._markets_loaded = True

    def test_coins_convert_to_contracts_using_ctval(self):
        # 2.5 ETH / 0.1 per contract = 25 contracts
        self.assertAlmostEqual(
            self.ex._coins_to_contracts("ETH-USDT-SWAP", 2.5), 25.0
        )

    def test_contracts_convert_back_to_coins(self):
        self.assertAlmostEqual(
            self.ex._contracts_to_coins("ETH-USDT-SWAP", 25.0), 2.5
        )

    def test_conversion_round_trips(self):
        for coins in (0.5, 2.5, 10.0):
            c = self.ex._coins_to_contracts("ETH-USDT-SWAP", coins)
            self.assertAlmostEqual(
                self.ex._contracts_to_coins("ETH-USDT-SWAP", c), coins
            )

    def test_size_is_floored_never_rounded_up(self):
        """Rounding up would exceed the approved max-loss size."""
        # 2.79 ETH -> 27.9 contracts -> floor to 27 (lot 1.0)
        self.assertAlmostEqual(
            self.ex._coins_to_contracts("ETH-USDT-SWAP", 2.79), 27.0
        )
        # 45 DOGE -> 4.5 contracts, lot 0.1 keeps 4.5 exactly
        self.assertAlmostEqual(
            self.ex._coins_to_contracts("DOGE-USDT-SWAP", 45.0), 4.5
        )

    def test_unknown_instrument_falls_back_to_one_to_one(self):
        # Conservative: never silently scale an unknown market.
        self.assertAlmostEqual(
            self.ex._coins_to_contracts("NEW-USDT-SWAP", 7.0), 7.0
        )

    def test_formatting_respects_lot_and_tick_precision(self):
        self.assertEqual(self.ex._fmt_size("ETH-USDT-SWAP", 25.0), "25")
        self.assertEqual(self.ex._fmt_size("DOGE-USDT-SWAP", 4.5), "4.5")
        self.assertEqual(self.ex._fmt_price("ETH-USDT-SWAP", 1234.5678), "1234.57")


class EnvelopeUnwrapTests(unittest.TestCase):
    """OKX returns HTTP 200 for business failures; treating that as success
    would let a rejected order look filled."""

    def test_success_returns_the_data_array(self):
        resp = _FakeResponse({"code": "0", "msg": "", "data": [{"ordId": "1"}]})
        self.assertEqual(
            OKXExecutor._unwrap(resp, "/x"), [{"ordId": "1"}]
        )

    def test_non_zero_outer_code_is_failure(self):
        resp = _FakeResponse({"code": "51008", "msg": "insufficient", "data": []})
        self.assertIsNone(OKXExecutor._unwrap(resp, "/x"))

    def test_per_item_scode_failure_is_failure(self):
        resp = _FakeResponse({
            "code": "0", "msg": "",
            "data": [{"sCode": "51121", "sMsg": "order qty error"}],
        })
        self.assertIsNone(OKXExecutor._unwrap(resp, "/x"))

    def test_scode_zero_is_accepted(self):
        resp = _FakeResponse({
            "code": "0", "msg": "", "data": [{"sCode": "0", "ordId": "9"}],
        })
        self.assertEqual(
            OKXExecutor._unwrap(resp, "/x"), [{"sCode": "0", "ordId": "9"}]
        )

    def test_non_json_body_is_failure_not_a_crash(self):
        self.assertIsNone(
            OKXExecutor._unwrap(_FakeResponse(_INVALID_JSON, 502), "/x")
        )


class PositionModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ex = OKXExecutor()

    def test_net_mode_omits_posside(self):
        self.ex._pos_mode = "net_mode"
        self.assertIsNone(self.ex._pos_side("LONG"))
        self.assertIsNone(self.ex._pos_side("SHORT"))

    def test_hedge_mode_requires_posside(self):
        self.ex._pos_mode = "long_short_mode"
        self.assertEqual(self.ex._pos_side("LONG"), "long")
        self.assertEqual(self.ex._pos_side("SHORT"), "short")

    def test_unknown_mode_is_treated_as_net(self):
        self.ex._pos_mode = None
        self.assertIsNone(self.ex._pos_side("LONG"))


class AuthHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ex = OKXExecutor()

    def test_signed_headers_carry_every_required_field(self):
        with patch.multiple(
            Config,
            OKX_API_KEY="key", OKX_SECRET_KEY="secret",
            OKX_PASSPHRASE="pass", OKX_PROJECT_ID="", OKX_DEMO=False,
        ):
            h = self.ex._headers("GET", "/api/v5/account/balance?ccy=USDT")
        for field in (
            "OK-ACCESS-KEY", "OK-ACCESS-SIGN",
            "OK-ACCESS-TIMESTAMP", "OK-ACCESS-PASSPHRASE",
        ):
            self.assertIn(field, h)
        self.assertNotIn("x-simulated-trading", h)

    def test_demo_mode_adds_the_simulated_header(self):
        with patch.object(Config, "OKX_DEMO", True):
            h = self.ex._headers("GET", "/api/v5/account/balance", auth=False)
        self.assertEqual(h.get("x-simulated-trading"), "1")

    def test_unauthenticated_calls_are_not_signed(self):
        h = self.ex._headers("GET", "/api/v5/market/ticker", auth=False)
        self.assertNotIn("OK-ACCESS-SIGN", h)

    def test_signature_changes_with_the_signed_payload(self):
        with patch.multiple(
            Config,
            OKX_API_KEY="key", OKX_SECRET_KEY="secret",
            OKX_PASSPHRASE="pass", OKX_PROJECT_ID="",
        ):
            a = self.ex._headers("POST", "/api/v5/trade/order", '{"a":1}')
            b = self.ex._headers("POST", "/api/v5/trade/order", '{"a":2}')
        self.assertNotEqual(a["OK-ACCESS-SIGN"], b["OK-ACCESS-SIGN"])

    def test_timestamp_is_iso_with_milliseconds(self):
        ts = OKXExecutor._timestamp()
        self.assertTrue(ts.endswith("Z"))
        self.assertEqual(len(ts), 24)   # 2026-08-06T12:34:56.789Z


class SharedStopMathTests(unittest.TestCase):
    """Both CEXes must derive the same stop, because decision_engine
    materialises it pre-trade for RR and max-loss sizing."""

    def test_okx_inherits_the_identical_sl_function(self):
        self.assertIs(OKXExecutor._calc_roi_sl, BaseExecutor._calc_roi_sl)
        self.assertIs(CEXExecutor._calc_roi_sl, BaseExecutor._calc_roi_sl)

    def test_both_providers_produce_the_same_stop(self):
        for action in ("LONG", "SHORT"):
            for lev in (5, 10):
                for atr, mode in ((1.0, "swing"), (0.5, "intraday")):
                    self.assertAlmostEqual(
                        OKXExecutor._calc_roi_sl(action, 100.0, lev, 8, 15.0, atr, mode),
                        CEXExecutor._calc_roi_sl(action, 100.0, lev, 8, 15.0, atr, mode),
                        msg=f"{action} {lev}x atr={atr} {mode}",
                    )


class OpenPositionsShapeTests(unittest.TestCase):
    """sync_positions computes notional as contracts * entryPrice, so this
    must report COINS. Reporting raw contracts misprices by ctVal."""

    def setUp(self) -> None:
        self.ex = OKXExecutor()
        self.ex._markets = {
            "ETH-USDT-SWAP": {
                "ct_val": 0.1, "ct_mult": 1.0, "lot_size": 1.0,
                "min_size": 1.0, "tick_size": 0.01, "max_leverage": 10.0,
            },
        }
        self.ex._markets_loaded = True
        self.ex._pos_mode = "net_mode"

    def _run(self, positions, algos):
        def fake_get(endpoint, params=None, auth=False):
            if endpoint == "/api/v5/account/positions":
                return positions
            if endpoint == "/api/v5/trade/orders-algo-pending":
                return algos
            return []

        with patch.object(Config, "DRY_RUN", False), \
                patch.object(self.ex, "_get", side_effect=fake_get):
            return asyncio.run(self.ex.get_open_positions())

    def test_contracts_are_reported_in_coins(self):
        out = self._run(
            [{
                "instId": "ETH-USDT-SWAP", "pos": "25", "posSide": "net",
                "avgPx": "2000", "lever": "10", "upl": "12.5",
            }],
            [],
        )
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p["symbol"], "ETHUSDT")
        # 25 contracts x 0.1 = 2.5 ETH, NOT 25
        self.assertAlmostEqual(p["contracts"], 2.5)
        self.assertAlmostEqual(p["entryPrice"], 2000.0)
        self.assertEqual(p["side"], "long")

    def test_negative_size_in_net_mode_is_a_short(self):
        out = self._run(
            [{
                "instId": "ETH-USDT-SWAP", "pos": "-25", "posSide": "net",
                "avgPx": "2000", "lever": "10", "upl": "-3",
            }],
            [],
        )
        self.assertEqual(out[0]["side"], "short")
        self.assertAlmostEqual(out[0]["contracts"], 2.5)

    def test_pending_algo_stop_is_surfaced_as_stoploss(self):
        """Stops are algo orders, absent from the positions endpoint. Without
        this the sync would think the position is unprotected."""
        out = self._run(
            [{
                "instId": "ETH-USDT-SWAP", "pos": "25", "posSide": "net",
                "avgPx": "2000", "lever": "10", "upl": "0",
            }],
            [{"instId": "ETH-USDT-SWAP", "slTriggerPx": "1900"}],
        )
        self.assertAlmostEqual(out[0]["stopLoss"], 1900.0)

    def test_missing_stop_reports_zero_so_sync_can_attach_one(self):
        out = self._run(
            [{
                "instId": "ETH-USDT-SWAP", "pos": "25", "posSide": "net",
                "avgPx": "2000", "lever": "10", "upl": "0",
            }],
            [],
        )
        self.assertEqual(out[0]["stopLoss"], 0.0)

    def test_flat_and_non_usdt_rows_are_skipped(self):
        out = self._run(
            [
                {"instId": "ETH-USDT-SWAP", "pos": "0", "avgPx": "2000"},
                {"instId": "BTC-USD-SWAP", "pos": "5", "avgPx": "60000"},
            ],
            [],
        )
        self.assertEqual(out, [])

    def test_dry_run_returns_no_positions(self):
        with patch.object(Config, "DRY_RUN", True):
            self.assertEqual(asyncio.run(self.ex.get_open_positions()), [])


class ProviderSelectionTests(unittest.TestCase):
    """A typo in CEX_PROVIDER must fail loudly, not silently trade on Bybit."""

    def test_valid_providers_are_accepted(self):
        for good in ("bybit", "okx"):
            with patch.object(Config, "CEX_PROVIDER", good):
                Config.validate()   # must not raise

    def test_typo_is_rejected(self):
        for bad in ("binance", "okex", "BYBIT "):
            with patch.object(Config, "CEX_PROVIDER", bad):
                with self.assertRaises(ValueError) as ctx:
                    Config.validate()
                self.assertIn("CEX_PROVIDER", str(ctx.exception))

    def test_bad_margin_mode_is_rejected_for_okx(self):
        with patch.multiple(
            Config, CEX_PROVIDER="okx", OKX_MARGIN_MODE="weird"
        ):
            with self.assertRaises(ValueError) as ctx:
                Config.validate()
            self.assertIn("OKX_MARGIN_MODE", str(ctx.exception))

    def test_live_okx_requires_the_passphrase(self):
        """OKX needs a passphrase; Bybit has no equivalent, so it is easy to
        leave blank and only discover on the first live order."""
        with patch.multiple(
            Config,
            DRY_RUN=False, EXCHANGE_MODE="CEX", CEX_PROVIDER="okx",
            OKX_API_KEY="k", OKX_SECRET_KEY="s", OKX_PASSPHRASE="",
        ):
            with self.assertRaises(ValueError) as ctx:
                Config.validate()
            self.assertIn("OKX_PASSPHRASE", str(ctx.exception))

    def test_live_okx_does_not_require_bybit_keys(self):
        with patch.multiple(
            Config,
            DRY_RUN=False, EXCHANGE_MODE="CEX", CEX_PROVIDER="okx",
            OKX_API_KEY="k", OKX_SECRET_KEY="s", OKX_PASSPHRASE="p",
            BYBIT_API_KEY="", BYBIT_SECRET="",
        ):
            Config.validate()   # must not raise


if __name__ == "__main__":
    unittest.main()
