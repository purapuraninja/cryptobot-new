"""
execution/cex_executor.py — Bybit USDT Perpetual Futures execution layer.

CEX mode = futures trading only (Bybit USDT linear perpetuals).
Supports One-Way mode (positionIdx=0) for Unified Trading Account.

Uses `requests` (sync HTTP) via asyncio.to_thread to avoid
aiohttp DNS resolution issues with VPN/ISP blocking.
"""

import asyncio
import hashlib
import hmac
import math
import time
import urllib.parse
from typing import Dict, List, Optional

import requests

from config import Config
from execution.base_executor import BaseExecutor
from utils.logger import logger



class CEXExecutor(BaseExecutor):

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.verify = True
        self._session.headers.update({"Content-Type": "application/json"})
        self._base_url = (
            "https://api-testnet.bybit.com"
            if Config.BYBIT_TESTNET
            else "https://api.bybit.com"
        )
        self._markets: Dict = {}  # symbol -> market info cache
        self._markets_loaded: bool = False
        self._recv_window = "5000"

    # ── Auth helpers ──────────────────────────────────────────────

    def _sign(self, params: Dict, query_str: str = "") -> Dict[str, str]:
        """Generate Bybit v5 HMAC-SHA256 signature headers."""
        ts = str(int(time.time() * 1000))
        api_key = Config.BYBIT_API_KEY
        secret = Config.BYBIT_SECRET

        # Use provided query string or build from params (keep original order)
        param_str = query_str
        if not param_str and params:
            param_str = urllib.parse.urlencode(params)

        sign_str = f"{ts}{api_key}{self._recv_window}{param_str}"
        signature = hmac.new(
            secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self._recv_window,
        }

    def _sign_post(self, body_str: str) -> Dict[str, str]:
        """Generate signature for POST requests with JSON body."""
        ts = str(int(time.time() * 1000))
        api_key = Config.BYBIT_API_KEY
        secret = Config.BYBIT_SECRET

        sign_str = f"{ts}{api_key}{self._recv_window}{body_str}"
        signature = hmac.new(
            secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self._recv_window,
            "Content-Type": "application/json",
        }

    # ── HTTP wrappers ─────────────────────────────────────────────

    def _get(self, endpoint: str, params: Dict = None, auth: bool = False) -> Optional[Dict]:
        """Sync GET request to Bybit v5 API."""
        url = f"{self._base_url}{endpoint}"
        query_str = urllib.parse.urlencode(params) if params else ""
        headers = self._sign(params or {}, query_str) if auth else {}
        try:
            # Use pre-built query string for consistent signing
            full_url = f"{url}?{query_str}" if query_str else url
            resp = self._session.get(full_url, headers=headers, timeout=15)
            # Guard against empty / non-JSON responses (e.g. 401 on missing auth)
            if not resp.text or not resp.text.strip():
                logger.error(
                    f"[CEX] GET {endpoint} empty response (status={resp.status_code}, "
                    f"auth={auth}) — likely missing/invalid signature"
                )
                return None
            try:
                data = resp.json()
            except ValueError:
                snippet = (resp.text or "")[:200].replace("\n", " ")
                logger.error(
                    f"[CEX] GET {endpoint} non-JSON response "
                    f"(status={resp.status_code}, auth={auth}): {snippet!r}"
                )
                return None
            if data.get("retCode") != 0:
                logger.error(f"[CEX] API error {endpoint}: {data.get('retMsg')}")
                return None
            return data.get("result", {})
        except Exception as exc:
            logger.error(f"[CEX] GET {endpoint} error: {exc}")
            return None

    def _post(self, endpoint: str, body: Dict) -> Optional[Dict]:
        """Sync POST request to Bybit v5 API."""
        import json
        url = f"{self._base_url}{endpoint}"
        body_str = json.dumps(body)
        headers = self._sign_post(body_str)
        try:
            resp = self._session.post(url, data=body_str, headers=headers, timeout=15)
            data = resp.json()
            if data.get("retCode") != 0:
                logger.error(f"[CEX] API error {endpoint}: {data.get('retMsg')} body={body}")
                return None
            return data.get("result", {})
        except Exception as exc:
            logger.error(f"[CEX] POST {endpoint} error: {exc}")
            return None

    # ── Market info ───────────────────────────────────────────────

    def _load_markets_sync(self) -> None:
        """Load linear USDT market info from Bybit."""
        cursor = ""
        all_markets = {}
        while True:
            params = {"category": "linear", "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            result = self._get("/v5/market/instruments-info", params)
            if result is None:
                break
            for item in result.get("list", []):
                sym = item["symbol"]  # e.g. "BTCUSDT"
                all_markets[sym] = {
                    "symbol": sym,
                    "base": item.get("baseCoin", ""),
                    "quote": item.get("quoteCoin", ""),
                    "tick_size": float(item.get("priceFilter", {}).get("tickSize", "0.01")),
                    "qty_step": float(item.get("lotSizeFilter", {}).get("qtyStep", "0.001")),
                    "min_qty": float(item.get("lotSizeFilter", {}).get("minOrderQty", "0.001")),
                    "min_notional": float(item.get("lotSizeFilter", {}).get("minNotionalValue", "5")),
                }
            cursor = result.get("nextPageCursor", "")
            if not cursor:
                break
        self._markets = all_markets
        self._markets_loaded = bool(all_markets)
        if not all_markets:
            logger.error("[CEX] Market metadata load failed; live orders remain blocked")
        logger.debug(f"[CEX] Markets loaded: {len(all_markets)} linear USDT pairs")

    async def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            await asyncio.to_thread(self._load_markets_sync)

    # ── Symbol helpers ────────────────────────────────────────────

    @staticmethod
    def _to_bybit(symbol: str) -> str:
        """Convert any format to Bybit format (e.g. BTCUSDT)."""
        s = symbol.strip().upper()
        # Remove ccxt-style formatting
        s = s.replace("/", "").replace(":USDT", "")
        return s

    # ── SL helpers ────────────────────────────────────────────────
    # _calc_roi_sl / _qty_dp / _price_dp now live on BaseExecutor so Bybit and
    # OKX derive an identical stop from identical inputs. Inherited, so the
    # existing CEXExecutor._calc_roi_sl(...) call in risk/decision_engine.py
    # keeps resolving.

    def _place_sl_sync(self, bybit_symbol: str, action: str,
                       qty: float, sl_price: float) -> Optional[Dict]:
        """Place a single stop-loss order. Returns result or None."""
        close_side = "Sell" if action == "LONG" else "Buy"
        trigger_dir = 2 if action == "LONG" else 1
        body = {
            "category":        "linear",
            "symbol":          bybit_symbol,
            "side":            close_side,
            "orderType":       "Market",
            "qty":             str(qty),
            "positionIdx":     0,
            "reduceOnly":      True,
            "triggerPrice":    str(sl_price),
            "triggerDirection": trigger_dir,
            "triggerBy":       "MarkPrice",
            "orderFilter":     "StopOrder",
        }
        return self._post("/v5/order/create", body)

    def _cancel_all_stop_orders_sync(self, bybit_symbol: str) -> bool:
        """Cancel ALL stop orders (StopOrder filter) for a symbol."""
        result = self._post("/v5/order/cancel-all", {
            "category":    "linear",
            "symbol":      bybit_symbol,
            "orderFilter": "StopOrder",
        })
        return result is not None

    async def update_sl_roi(self, symbol: str, action: str,
                            new_entry: float, leverage: float) -> Optional[float]:
        """
        Called after DCA: cancel existing SL, place new SL at -100% ROI
        from the updated average entry price.

        Returns new SL price, or None on failure.
        """
        bybit_symbol = self._to_bybit(symbol)
        if Config.DRY_RUN:
            sl = self._calc_roi_sl(action, new_entry, leverage)
            logger.info(
                f"[CEX][DRY RUN] update_sl_roi {symbol}: "
                f"new_entry={new_entry:.6f} new_sl={sl:.6f}"
            )
            return sl

        await self._ensure_markets()
        mkt = self._markets.get(bybit_symbol, {})
        tick = mkt.get("tick_size", 0.0001)
        price_dp = self._price_dp(tick)
        qty_step = mkt.get("qty_step", 1)
        min_qty = mkt.get("min_qty", 1)
        qdp = self._qty_dp(qty_step)

        # Get current open position qty from Bybit
        pos_info = await asyncio.to_thread(
            self._get, "/v5/position/list",
            {"category": "linear", "symbol": bybit_symbol}, True
        )
        qty = 0.0
        if pos_info and pos_info.get("list"):
            qty = float(pos_info["list"][0].get("size", 0))

        if qty <= 0:
            logger.warning(f"[CEX] update_sl_roi: no open qty for {symbol}")
            return None

        new_sl = self._calc_roi_sl(action, new_entry, leverage, price_dp)

        # Step 1: cancel existing stop orders
        cancelled = await asyncio.to_thread(
            self._cancel_all_stop_orders_sync, bybit_symbol
        )
        if cancelled:
            logger.info(f"[CEX] Cancelled stop orders for {symbol}")
        else:
            logger.warning(f"[CEX] Could not cancel stop orders for {symbol} — placing anyway")

        # Step 2: place new SL
        sl_result = await asyncio.to_thread(
            self._place_sl_sync, bybit_symbol, action, qty, new_sl
        )
        if sl_result:
            logger.info(
                f"[CEX] New SL placed @ {new_sl:.6f} "
                f"({symbol} {action} avg_entry={new_entry:.6f} {leverage:.0f}x)"
            )
            return new_sl
        else:
            logger.warning(f"[CEX] Failed to place new SL for {symbol}")
            return None

    # ── Manual partial/full close ─────────────────────────────────

    def _get_fill_sync(
        self, bybit_symbol: str, order_id: str, expected_qty: float,
        max_wait_s: float = 8.0,
    ) -> Optional[Dict]:
        """
        Query /v5/execution/list for actual filled price of a market order.
        Returns confirmed full-fill quantity and VWAP, or None on partial,
        rejected, cancelled, or ambiguous timeout.

        Bybit market orders typically populate execution-list within ~0.5-2s.
        We poll up to max_wait_s before giving up.
        """
        import time as _time
        if not order_id:
            return None
        deadline = _time.time() + max_wait_s
        while _time.time() < deadline:
            result = self._get(
                "/v5/execution/list",
                {"category": "linear", "symbol": bybit_symbol, "orderId": order_id},
                auth=True,
            )
            if result and result.get("list"):
                fills = result["list"]
                total_qty = 0.0
                weighted_px = 0.0
                total_fee = 0.0
                for f in fills:
                    try:
                        q = float(f.get("execQty", 0))
                        p = float(f.get("execPrice", 0))
                    except (TypeError, ValueError):
                        continue
                    if q > 0 and p > 0:
                        total_qty += q
                        weighted_px += p * q
                        # execFee: fee aktual per fill dari Bybit — dulu
                        # dibuang, membuat PnL live tercatat gross (~0.11%
                        # round-trip lebih bagus dari kenyataan).
                        try:
                            total_fee += float(f.get("execFee", 0) or 0)
                        except (TypeError, ValueError):
                            pass
                if total_qty > 0:
                    tolerance = max(abs(expected_qty) * 1e-8, 1e-12)
                    if total_qty + tolerance >= expected_qty:
                        return {
                            "price": round(weighted_px / total_qty, 8),
                            "filled_qty": total_qty,
                            "fees": round(total_fee, 6),
                        }
            _time.sleep(0.5)
        logger.warning(
            f"[CEX] Full fill not confirmed within {max_wait_s}s "
            f"for orderId={order_id} ({bybit_symbol})"
        )
        return None

    async def partial_close(
        self, symbol: str, action: str, close_pct: float
    ) -> Optional[Dict]:
        """
        Close close_pct% (1-100) of an open position at market price.

        Args:
            symbol   : e.g. "SIRENUSDT"
            action   : "LONG" or "SHORT" (side of the OPEN position)
            close_pct: percentage to close (25, 50, 75, 100)

        Returns dict with {closed_qty, close_side, symbol, pct, price} or None on failure.
        """
        close_pct = max(1.0, min(100.0, close_pct))
        bybit_symbol = self._to_bybit(symbol)

        # Load market info for qty_step
        if not self._markets_loaded:
            await asyncio.to_thread(self._load_markets_sync)
        mkt = self._markets.get(bybit_symbol, {})
        qty_step = mkt.get("qty_step", 1.0)
        min_qty  = mkt.get("min_qty",  1.0)
        qdp      = self._qty_dp(qty_step)

        if Config.DRY_RUN:
            logger.info(
                f"[CEX][DRY RUN] partial_close {symbol} {close_pct:.0f}% — skipped"
            )
            return {"symbol": symbol, "pct": close_pct, "dry_run": True}

        # 1. Get current position size from Bybit
        pos_info = await asyncio.to_thread(
            self._get, "/v5/position/list",
            {"category": "linear", "symbol": bybit_symbol}, True
        )
        if not pos_info or not pos_info.get("list"):
            logger.warning(f"[CEX] partial_close: no position found for {symbol}")
            return None

        current_qty = float(pos_info["list"][0].get("size", 0))
        if current_qty <= 0:
            logger.warning(f"[CEX] partial_close: position size = 0 for {symbol}")
            return None

        # 2. Calculate qty to close (snap to qty_step, floor so we never over-close)
        raw_close = current_qty * close_pct / 100.0
        close_qty = math.floor(raw_close / qty_step) * qty_step
        close_qty = round(close_qty, qdp)

        # For 100% close, use full qty to avoid rounding leaving a dust position
        if close_pct >= 99.9:
            close_qty = current_qty

        if close_qty < min_qty:
            logger.warning(
                f"[CEX] partial_close: computed qty {close_qty} < min_qty {min_qty} "
                f"({symbol} {close_pct:.0f}% of {current_qty})"
            )
            return None

        # 3. Market close order (reduceOnly)
        close_side = "Sell" if action == "LONG" else "Buy"
        body = {
            "category":    "linear",
            "symbol":      bybit_symbol,
            "side":        close_side,
            "orderType":   "Market",
            "qty":         str(close_qty),
            "positionIdx": 0,
            "reduceOnly":  True,
        }

        result = await asyncio.to_thread(self._post, "/v5/order/create", body)
        if result:
            order_id = result.get("orderId", "")
            # Query actual VWAP fill price from execution list
            # (Bybit market order response does NOT include price)
            fill = await asyncio.to_thread(
                self._get_fill_sync, bybit_symbol, order_id, close_qty
            )
            if not fill:
                # Audit 2026-07-26: order market reduce-only hampir selalu
                # terisi — yang gagal biasanya cuma KONFIRMASINYA. Return None
                # di sini dulu memicu retry queue tanpa batas → order reduce-
                # only KEDUA untuk slice yang sudah tertutup (double-close).
                # Verifikasi via delta ukuran posisi sebelum menyerah.
                pos_after = await asyncio.to_thread(
                    self._get, "/v5/position/list",
                    {"category": "linear", "symbol": bybit_symbol}, True
                )
                if pos_after and pos_after.get("list"):
                    qty_after = float(pos_after["list"][0].get("size", 0))
                    qty_delta = current_qty - qty_after
                    if qty_delta + max(qty_step, 1e-12) >= close_qty:
                        # Posisi memang berkurang sebesar close_qty — anggap
                        # filled. Harga pakai ticker (VWAP tak tersedia),
                        # ditandai unreconciled untuk recount-stats.
                        tick = await asyncio.to_thread(
                            self._get, "/v5/market/tickers",
                            {"category": "linear", "symbol": bybit_symbol}, False
                        )
                        _px = 0.0
                        try:
                            _px = float(tick["list"][0]["lastPrice"]) if tick and tick.get("list") else 0.0
                        except (KeyError, TypeError, ValueError):
                            _px = 0.0
                        logger.warning(
                            f"[CEX] partial_close fill unconfirmed but position "
                            f"size dropped {current_qty}→{qty_after} — treating "
                            f"as filled @ ticker {_px} (unreconciled)"
                        )
                        return {
                            "symbol":      symbol,
                            "action":      action,
                            "close_side":  close_side,
                            "pct":         min(qty_delta / current_qty * 100.0, 100.0),
                            "requested_pct": close_pct,
                            "closed_qty":  qty_delta,
                            "total_qty":   current_qty,
                            "order_id":    order_id,
                            "price":       _px,
                            "unreconciled": True,
                        }
                logger.error(
                    f"[CEX] partial_close ambiguous/not fully filled for {symbol}; "
                    "tracker will not be mutated"
                )
                return None
            fill_price = float(fill["price"])
            filled_qty = float(fill["filled_qty"])
            actual_pct = min(filled_qty / current_qty * 100.0, 100.0)
            logger.info(
                f"[CEX] partial_close OK: {symbol} {close_side} {close_qty} "
                f"({close_pct:.0f}% of {current_qty})"
                + (f" @ {fill_price}" if fill_price else " (fill price pending)")
            )
            return {
                "symbol":      symbol,
                "action":      action,
                "close_side":  close_side,
                "pct":         actual_pct,
                "requested_pct": close_pct,
                "closed_qty":  filled_qty,
                "total_qty":   current_qty,
                "order_id":    order_id,
                "price":       fill_price,
            }
        else:
            logger.error(f"[CEX] partial_close FAILED for {symbol} {close_pct:.0f}%")
            return None

    # ── Pre-trade ─────────────────────────────────────────────────

    def _set_leverage_sync(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol. Skip margin mode switch on Unified Account."""
        import json as _json
        url = f"{self._base_url}/v5/position/set-leverage"
        body = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        body_str = _json.dumps(body)
        headers = self._sign_post(body_str)
        try:
            resp = self._session.post(url, data=body_str, headers=headers, timeout=15)
            data = resp.json()
            code = data.get("retCode", -1)
            if code == 0:
                logger.debug(f"[CEX] Leverage {leverage}x set for {symbol}")
                return True
            elif code == 110043:
                # "leverage not modified" — already at target, OK
                logger.debug(f"[CEX] Leverage already {leverage}x for {symbol}")
                return True
            else:
                logger.warning(f"[CEX] set_leverage {symbol}: {data.get('retMsg')}")
                return False
        except Exception as exc:
            logger.warning(f"[CEX] set_leverage {symbol}: {exc}")
            return False

    # ── BaseExecutor ──────────────────────────────────────────────

    async def execute_signal(self, signal: Dict) -> Optional[Dict]:
        action = signal.get("action", "HOLD")
        if action not in ("LONG", "SHORT"):
            return None

        raw_symbol: str = signal.get("symbol", "")
        bybit_symbol = self._to_bybit(raw_symbol)
        executor_cap = max(
            Config.LEVERAGE_CAP_DEFENSIVE,
            Config.LEVERAGE_CAP_CONSERVATIVE,
            Config.LEVERAGE_CAP_NORMAL,
            Config.LEVERAGE_CAP_AGGRESSIVE,
        )
        if str(signal.get("risk_level", "MEDIUM")).upper() == "HIGH":
            executor_cap = min(executor_cap, Config.LEVERAGE_CAP_HIGH_RISK)
        leverage = max(1, min(int(signal.get("leverage", 5)), executor_cap))
        entry_price = float(signal.get("entry_price", 0))
        stop_loss = float(signal.get("stop_loss", 0))
        take_profits: List[float] = [
            float(v) for v in signal.get("take_profit", [])
            if isinstance(v, (int, float)) and float(v) > 0
        ]
        position_pct = float(signal.get("position_size_percent", 1.0))

        if entry_price <= 0 or stop_loss <= 0:
            logger.warning(f"[CEX] Invalid levels entry={entry_price} sl={stop_loss}")
            return None

        # Materialise the legacy ROI stop before the DRY_RUN branch as a final
        # defence-in-depth.  Normal main.py flow already does this before RR
        # and max-loss sizing, but direct executor callers must behave equally.
        if Config.SL_MODE == "roi":
            stop_loss = self._calc_roi_sl(
                action,
                entry_price,
                leverage,
                price_dp=8,
                max_price_move_pct=Config.SL_MAX_PRICE_MOVE_PCT,
                atr_pct=float(signal.get("_atr_pct", 0.0) or 0.0),
                mode=str(signal.get("_mode", Config.TRADING_MODE)),
            )

        # DRY RUN
        if Config.DRY_RUN:
            # Audit 2026-07-26: fill dry-run dulu memakai harga snapshot pra-AI
            # (basi 30d-2m saat AI + filter selesai). Karena sinyal momentum,
            # harga riil biasanya sudah bergerak SEARAH sinyal → edge dry-run
            # menggembung. Re-price pakai ticker publik terbaru supaya forward
            # test jujur; slippage model tracker tetap di atasnya.
            try:
                tick = await asyncio.to_thread(
                    self._get, "/v5/market/tickers",
                    {"category": "linear", "symbol": bybit_symbol}, False
                )
                _fresh = (
                    float(tick["list"][0]["lastPrice"])
                    if tick and tick.get("list") else 0.0
                )
                if _fresh > 0:
                    if abs(_fresh - entry_price) / entry_price * 100 > 0.05:
                        logger.info(
                            f"[CEX][DRY RUN] {raw_symbol} entry re-priced "
                            f"{entry_price} → {_fresh} "
                            f"({(_fresh - entry_price) / entry_price * 100:+.3f}% drift)"
                        )
                    entry_price = _fresh
            except Exception:
                pass  # ticker gagal → pakai harga snapshot (perilaku lama)
            logger.info(
                f"[CEX][DRY RUN] {action} {raw_symbol} "
                f"entry={entry_price:,}  sl={stop_loss:,}  lev={leverage}x"
            )
            return {
                "status": "dry_run",
                "exchange": "bybit_futures",
                "symbol": raw_symbol,
                "action": action,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "leverage": leverage,
                "position_size_percent": position_pct,
            }

        # LIVE
        await self._ensure_markets()

        if bybit_symbol not in self._markets:
            logger.error(f"[CEX] {bybit_symbol} not in Bybit linear markets")
            return None

        mkt = self._markets[bybit_symbol]
        qty_step = mkt["qty_step"]
        min_qty = mkt["min_qty"]
        qdp = self._qty_dp(qty_step)

        # Set leverage
        leverage_ok = await asyncio.to_thread(
            self._set_leverage_sync, bybit_symbol, leverage
        )
        if not leverage_ok:
            logger.error(f"[CEX] Entry aborted: failed to set {leverage}x on {bybit_symbol}")
            return None

        # Calculate qty
        balance = await self.get_balance()
        if balance is None or balance <= 0:
            logger.error(f"[CEX] Entry aborted: live balance unavailable for {bybit_symbol}")
            return None
        notional = balance * (position_pct / 100.0) * leverage
        raw_qty = notional / entry_price
        # Round down so exchange constraints can never increase risk above the
        # deterministic size cap.
        qty = round(math.floor(raw_qty / qty_step) * qty_step, qdp)
        if qty < min_qty:
            logger.warning(
                f"[CEX] {bybit_symbol} risk-sized qty={qty} below min_qty={min_qty}; "
                "trade rejected instead of forcing a larger position"
            )
            return None

        side = "Buy" if action == "LONG" else "Sell"
        close_side = "Sell" if action == "LONG" else "Buy"
        pos_idx = 0  # One-Way mode (Unified Trading Account)

        # Entry order
        entry_body = {
            "category": "linear",
            "symbol": bybit_symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "positionIdx": pos_idx,
        }
        entry_result = await asyncio.to_thread(
            self._post, "/v5/order/create", entry_body
        )
        if entry_result is None:
            logger.error(f"[CEX] Entry order failed for {bybit_symbol}")
            return None

        order_id = entry_result.get("orderId", "")
        fill = await asyncio.to_thread(
            self._get_fill_sync, bybit_symbol, order_id, qty
        )
        entry_fees = 0.0
        if fill:
            actual_entry = float(fill["price"])
            filled_qty = float(fill["filled_qty"])
            entry_fees = float(fill.get("fees", 0.0) or 0.0)
            logger.info(
                f"[CEX] Entry {action} {filled_qty} {bybit_symbol} @ {actual_entry}: {order_id}"
            )
            entry_price = actual_entry
        else:
            # Audit 2026-07-26: order market sudah TERKIRIM — kalau kita return
            # None di sini padahal sebenarnya terisi, posisi live nyata duduk
            # di Bybit TANPA SL dan TANPA tracking sampai sync scan berikutnya.
            # Verifikasi via position list dulu; kalau posisinya ada, lanjutkan
            # dengan harga ticker (unreconciled) supaya SL tetap terpasang.
            pos_chk = await asyncio.to_thread(
                self._get, "/v5/position/list",
                {"category": "linear", "symbol": bybit_symbol}, True
            )
            chk_qty = 0.0
            if pos_chk and pos_chk.get("list"):
                try:
                    chk_qty = float(pos_chk["list"][0].get("size", 0))
                except (TypeError, ValueError):
                    chk_qty = 0.0
            if chk_qty > 0:
                filled_qty = min(chk_qty, qty)
                try:
                    _avg = float(pos_chk["list"][0].get("avgPrice", 0) or 0)
                except (TypeError, ValueError):
                    _avg = 0.0
                if _avg > 0:
                    entry_price = _avg
                logger.warning(
                    f"[CEX] Entry fill unconfirmed but position exists on Bybit "
                    f"(size={chk_qty}) — proceeding with avgPrice={entry_price} "
                    f"(unreconciled) so SL still gets attached"
                )
            else:
                logger.error(
                    f"[CEX] Entry fill ambiguous/not fully confirmed for {bybit_symbol} "
                    "and no position on Bybit; refusing to create local position"
                )
                return None

        result: Dict = {
            "status": "executed",
            "exchange": "bybit_futures",
            "symbol": raw_symbol,
            "action": action,
            "entry_order_id": order_id,
            "qty": filled_qty,
            "actual_notional": round(filled_qty * entry_price, 8),
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "leverage": leverage,
            "fees": entry_fees,   # fee entry aktual (execFee) untuk pnl_tracker
            "sl_order_id": None,
            "tp1_order_id": None,
        }

        # ── PREVENT "10 working stop orders" Bybit limit ──
        # Cancel any leftover SL/TP from previous positions on this symbol
        # before placing new ones. Each entry creates 4 orders (1 SL + 3 TP);
        # without cleanup, after 2-3 entries → hit limit → TP fails to place →
        # position runs without TP exit (relies on trail/SL only).
        try:
            cleanup_ok = await asyncio.to_thread(
                self._cancel_all_stop_orders_sync, bybit_symbol
            )
            if cleanup_ok:
                logger.info(f"[CEX] Cleaned old conditional orders for {bybit_symbol}")
        except Exception as exc:
            logger.warning(f"[CEX] Order cleanup failed for {bybit_symbol}: {exc}")

        # Stop-loss
        if Config.SL_MODE == "none":
            logger.info(f"[CEX] SL_MODE=none — no SL placed for {bybit_symbol}")

        else:
            # SL_MODE="roi": override AI's SL with -100% ROI price
            # + capped at SL_MAX_PRICE_MOVE_PCT to prevent catastrophic losses
            if Config.SL_MODE == "roi":
                tick = mkt.get("tick_size", 0.0001)
                price_dp = self._price_dp(tick)
                _atr_pct = float(signal.get("_atr_pct", 0.0))
                _mode = str(signal.get("_mode", "swing"))
                
                stop_loss = self._calc_roi_sl(
                    action, entry_price, leverage, price_dp,
                    max_price_move_pct=Config.SL_MAX_PRICE_MOVE_PCT,
                    atr_pct=_atr_pct,
                    mode=_mode
                )
                # Calculate actual % from entry for logging
                if action in ("LONG", "BUY"):
                    sl_pct = (stop_loss - entry_price) / entry_price * 100
                else:
                    sl_pct = (entry_price - stop_loss) / entry_price * 100
                logger.info(
                    f"[CEX] SL_MODE=roi: SL → {stop_loss:.6f} "
                    f"({sl_pct:+.2f}% from entry, cap={Config.SL_MAX_PRICE_MOVE_PCT}%, {leverage}x)"
                )
                # FIX: update result dict dengan ROI SL aktual agar pnl_tracker
                # menyimpan SL yang benar — bukan AI SL yang lebih ketat.
                # Tanpa ini, pnl_tracker memantau SL lama dan close posisi prematur.
                result["stop_loss"] = stop_loss

            sl_result = await asyncio.to_thread(
                self._place_sl_sync, bybit_symbol, action, qty, stop_loss
            )
            if sl_result:
                result["sl_order_id"] = sl_result.get("orderId", "")
                logger.info(f"[CEX] SL placed @ {stop_loss}")
            else:
                logger.error(
                    "[CEX] SL placement failed after entry; attempting emergency rollback"
                )
                rollback = await asyncio.to_thread(
                    self._post,
                    "/v5/order/create",
                    {
                        "category": "linear",
                        "symbol": bybit_symbol,
                        "side": close_side,
                        "orderType": "Market",
                        "qty": str(qty),
                        "positionIdx": pos_idx,
                        "reduceOnly": True,
                    },
                )
                if rollback:
                    logger.error(
                        f"[CEX] Unprotected {bybit_symbol} entry rolled back immediately"
                    )
                    return None
                # Preserve local tracking if both protection and rollback fail;
                # returning None here would orphan a real open position.
                logger.critical(
                    f"[CEX] CRITICAL: {bybit_symbol} is open without exchange SL "
                    "and emergency rollback failed"
                )
                result["status"] = "executed_unprotected"
                result["requires_manual_intervention"] = True
                return result

        if not Config.EXCHANGE_TP_ORDERS_ENABLED:
            logger.info(
                "[CEX] Exchange TP orders disabled; local position tracker owns partial exits"
            )
            return result

        # Match the local tracker's sequential TP allocation exactly.
        tp1_frac = max(0.0, min(Config.TP1_CLOSE_PCT / 100.0, 1.0))
        tp2_frac = (1.0 - tp1_frac) * max(
            0.0, min(Config.TP2_CLOSE_PCT / 100.0, 1.0)
        )
        tp_fractions = [tp1_frac, tp2_frac, max(0.0, 1.0 - tp1_frac - tp2_frac)]
        tp_prices = take_profits[:3]
        placed_qty = 0.0
        for i, tp_price in enumerate(tp_prices):
            if tp_price <= 0:
                continue
            if i == len(tp_prices) - 1:
                raw_tp_qty = max(0.0, qty - placed_qty)
            else:
                raw_tp_qty = qty * tp_fractions[i]
            tp_qty = round(math.floor(raw_tp_qty / qty_step) * qty_step, qdp)
            if tp_qty < min_qty:
                logger.warning(
                    f"[CEX] TP{i+1} skipped: qty={tp_qty} below min_qty={min_qty}"
                )
                continue

            tp_body = {
                "category": "linear",
                "symbol": bybit_symbol,
                "side": close_side,
                "orderType": "Market",
                "qty": str(tp_qty),
                "positionIdx": pos_idx,
                "reduceOnly": True,
                "triggerPrice": str(tp_price),
                "triggerDirection": 1 if action == "LONG" else 2,
                "triggerBy": "MarkPrice",
                "orderFilter": "StopOrder",
            }
            tp_result = await asyncio.to_thread(
                self._post, "/v5/order/create", tp_body
            )
            if tp_result:
                result[f"tp{i+1}_order_id"] = tp_result.get("orderId")
                placed_qty = round(placed_qty + tp_qty, qdp)
                logger.info(f"[CEX] TP{i+1} placed @ {tp_price:,} qty={tp_qty}")
            else:
                logger.warning(f"[CEX] TP{i+1} failed (non-fatal)")

        return result

    async def execute_dca(self, symbol: str, action: str, leverage: int,
                          dca_pct: float = 1.0) -> Optional[Dict]:
        """
        Execute a DCA (Dollar Cost Average) order — add to existing position.

        Args:
            symbol: Raw symbol (e.g. BTCUSDT)
            action: LONG or SHORT
            leverage: Current leverage
            dca_pct: % of balance to add (default 1%)
        """
        bybit_symbol = self._to_bybit(symbol)

        if Config.DRY_RUN:
            logger.info(f"[CEX][DRY RUN] DCA {action} {symbol} +{dca_pct}%")
            return {
                "status": "dry_run_dca",
                "symbol": symbol,
                "action": action,
                "dca_pct": dca_pct,
            }

        await self._ensure_markets()

        if bybit_symbol not in self._markets:
            logger.error(f"[CEX] DCA: {bybit_symbol} not in markets")
            return None

        mkt = self._markets[bybit_symbol]
        qty_step = mkt["qty_step"]
        min_qty = mkt["min_qty"]
        qdp = self._qty_dp(qty_step)

        # Get current price
        ticker = await asyncio.to_thread(
            self._get, "/v5/market/tickers",
            {"category": "linear", "symbol": bybit_symbol}
        )
        if not ticker or not ticker.get("list"):
            logger.error(f"[CEX] DCA: cannot get price for {bybit_symbol}")
            return None
        current_price = float(ticker["list"][0].get("lastPrice", 0))
        if current_price <= 0:
            return None

        balance = await self.get_balance() or Config.ACCOUNT_BALANCE
        notional = balance * (dca_pct / 100.0) * leverage
        qty = max(round(notional / current_price, qdp), min_qty)
        qty = math.floor(qty / qty_step) * qty_step
        qty = round(qty, qdp)
        if qty < min_qty:
            qty = min_qty

        side = "Buy" if action == "LONG" else "Sell"

        dca_body = {
            "category": "linear",
            "symbol": bybit_symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "positionIdx": 0,
        }
        result = await asyncio.to_thread(
            self._post, "/v5/order/create", dca_body
        )
        if result is None:
            logger.error(f"[CEX] DCA order failed for {bybit_symbol}")
            return None

        order_id = result.get("orderId", "")
        logger.info(
            f"[CEX] DCA {action} +{qty} {bybit_symbol} @ ~{current_price:,.4f}: {order_id}"
        )
        return {
            "status": "dca_executed",
            "symbol": symbol,
            "action": action,
            "qty": qty,
            "price": current_price,
            "dca_pct": dca_pct,
            "order_id": order_id,
        }

    async def close_position(self, symbol: str) -> Optional[Dict]:
        bybit_symbol = self._to_bybit(symbol)
        if Config.DRY_RUN:
            return {"status": "dry_run_closed", "symbol": symbol}

        # Get open positions for this symbol
        result = await asyncio.to_thread(
            self._get, "/v5/position/list",
            {"category": "linear", "symbol": bybit_symbol}, True
        )
        if result is None:
            return None

        for pos in result.get("list", []):
            size = float(pos.get("size", 0))
            if size > 0:
                side = pos.get("side", "")
                close_side = "Sell" if side == "Buy" else "Buy"
                pos_idx = int(pos.get("positionIdx", 0))

                close_body = {
                    "category": "linear",
                    "symbol": bybit_symbol,
                    "side": close_side,
                    "orderType": "Market",
                    "qty": str(size),
                    "positionIdx": pos_idx,
                    "reduceOnly": True,
                }
                close_result = await asyncio.to_thread(
                    self._post, "/v5/order/create", close_body
                )
                if close_result:
                    logger.info(f"[CEX] Closed {symbol}: {close_result.get('orderId')}")
                    return {"status": "closed", "symbol": symbol,
                            "order_id": close_result.get("orderId")}

        logger.info(f"[CEX] No open position for {symbol}")
        return None

    async def get_open_positions(self) -> List[Dict]:
        if Config.DRY_RUN:
            return []

        result = await asyncio.to_thread(
            self._get, "/v5/position/list",
            {"category": "linear", "settleCoin": "USDT", "limit": "200"},
            True
        )
        if result is None:
            raise RuntimeError("Bybit open-position request failed")

        positions = []
        for pos in result.get("list", []):
            size = float(pos.get("size", 0))
            if size > 0:
                side = pos.get("side", "")
                # Bybit returns SL/TP as strings, "0" if not set
                _sl = pos.get("stopLoss", "0") or "0"
                _tp = pos.get("takeProfit", "0") or "0"
                try: sl_val = float(_sl)
                except (ValueError, TypeError): sl_val = 0.0
                try: tp_val = float(_tp)
                except (ValueError, TypeError): tp_val = 0.0
                positions.append({
                    "symbol": pos.get("symbol", ""),
                    "side": "long" if side == "Buy" else "short",
                    "contracts": size,
                    "entryPrice": float(pos.get("avgPrice", 0)),
                    "leverage": float(pos.get("leverage", 1)),
                    "unrealizedPnl": float(pos.get("unrealisedPnl", 0)),
                    "stopLoss": sl_val,        # 0 = no SL on Bybit (DANGER)
                    "takeProfit": tp_val,
                })
        return positions

    async def get_closed_pnl(self, symbol: str = None, limit: int = 10) -> List[Dict]:
        """
        Fetch recently closed positions with realized PnL from Bybit.
        Used by live position sync to reconcile closed trades.
        """
        if Config.DRY_RUN:
            return []

        params: Dict = {"category": "linear", "limit": str(limit)}
        if symbol:
            params["symbol"] = self._to_bybit(symbol)

        result = await asyncio.to_thread(
            self._get, "/v5/position/closed-pnl", params, True
        )
        if result is None:
            raise RuntimeError("Bybit closed-PnL request failed")

        closed = []
        for item in result.get("list", []):
            closed.append({
                "symbol":        item.get("symbol", ""),
                "side":          "long" if item.get("side") == "Buy" else "short",
                "qty":           float(item.get("qty", 0)),
                "entry_price":   float(item.get("avgEntryPrice", 0)),
                "exit_price":    float(item.get("avgExitPrice", 0)),
                "realized_pnl":  float(item.get("closedPnl", 0)),
                "updated_time":  item.get("updatedTime", ""),
            })
        return closed

    async def get_last_price(self, symbol: str) -> Optional[float]:
        """Last traded price, or None.

        Exists so callers never have to reach into a provider-specific REST
        path. agent/commands.py used to call executor._get("/v5/market/tickers")
        directly, which silently produced no exit price on any non-Bybit CEX.
        """
        result = await asyncio.to_thread(
            self._get, "/v5/market/tickers",
            {"category": "linear", "symbol": self._to_bybit(symbol)}, False
        )
        try:
            if result and result.get("list"):
                px = float(result["list"][0].get("lastPrice", 0) or 0)
                return px if px > 0 else None
        except (KeyError, TypeError, ValueError):
            pass
        return None

    async def get_balance(self) -> Optional[float]:
        """Fetch USDT equity from Bybit unified account."""
        if Config.DRY_RUN:
            return None

        result = await asyncio.to_thread(
            self._get, "/v5/account/wallet-balance",
            {"accountType": "UNIFIED"}, True
        )
        if result is None:
            return None

        for account in result.get("list", []):
            total_equity = float(account.get("totalEquity", 0))
            if total_equity > 0:
                return total_equity
            # Fallback: look in individual coins
            for coin in account.get("coin", []):
                if coin.get("coin") == "USDT":
                    equity = float(coin.get("equity", 0))
                    if equity > 0:
                        return equity
        return None

    async def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
