"""
execution/okx_executor.py — OKX USDT-margined SWAP (perpetual) execution layer.

Selected with EXCHANGE_MODE=CEX + CEX_PROVIDER=okx. Bybit remains the default;
this is an alternative, not a replacement.

Mirrors CEXExecutor's public contract exactly so main.py, agent/positions.py,
agent/commands.py and utils/pnl_tracker.py need no provider branching:

    execute_signal, partial_close, close_position, update_sl_roi, execute_dca,
    get_open_positions, get_closed_pnl, get_balance, get_last_price, close

Three OKX-specific hazards this module absorbs so callers never see them:

1. SIZE IS IN CONTRACTS, NOT COINS. Bybit sizes orders in base coin; OKX SWAP
   sizes in contracts where 1 contract = ctVal of the base currency. Every
   inbound coin quantity is converted via ctVal and snapped to lotSz, and every
   outbound quantity is converted back to coins — so the tracker keeps storing
   coin quantities and PnL math stays comparable across providers.

2. STOPS ARE ALGO ORDERS. OKX keeps conditional orders in a separate namespace
   (/trade/order-algo) with algoId, not ordId. They are not returned by the
   regular order endpoints and must be cancelled through cancel-algos.

3. DEMO TRADING IS A HEADER, NOT A HOST. Bybit testnet is a different domain;
   OKX uses x-simulated-trading: 1 against the production host.

Uses `requests` (sync HTTP) via asyncio.to_thread, matching CEXExecutor — this
avoids the aiohttp DNS issues noted there with VPN/ISP blocking.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from config import Config
from execution.base_executor import BaseExecutor
from utils.logger import logger


class OKXExecutor(BaseExecutor):

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.verify = True
        self._session.headers.update({"Content-Type": "application/json"})
        self._base_url = Config.OKX_BASE_URL.rstrip("/")
        self._markets: Dict = {}      # instId -> instrument info cache
        self._markets_loaded: bool = False
        # HAZARD 4: position mode. In "long_short_mode" OKX REQUIRES posSide on
        # every order and rejects it otherwise; in "net_mode" it must be absent
        # or "net". Detected once from the account, never guessed.
        self._pos_mode: Optional[str] = None
        # OKX rejects tdMode it does not expect for the account's position mode.
        self._td_mode = (
            "isolated" if Config.OKX_MARGIN_MODE == "isolated" else "cross"
        )

    # ── Auth helpers ──────────────────────────────────────────────

    @staticmethod
    def _timestamp() -> str:
        """OKX wants an ISO-8601 UTC timestamp with milliseconds."""
        return (
            datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )

    def _headers(
        self, method: str, request_path: str, body: str = "", auth: bool = True
    ) -> Dict[str, str]:
        """Build OKX v5 headers.

        Signature = base64(HMAC-SHA256(ts + METHOD + requestPath + body)).
        requestPath MUST include the query string, and body MUST be the exact
        serialised string that gets sent — any mismatch is a 401.
        """
        headers = {"Content-Type": "application/json"}
        # Demo trading is a header on the production host, not a separate domain.
        if Config.OKX_DEMO:
            headers["x-simulated-trading"] = "1"
        if not auth:
            return headers

        ts = self._timestamp()
        msg = f"{ts}{method.upper()}{request_path}{body}"
        signature = base64.b64encode(
            hmac.new(
                Config.OKX_SECRET_KEY.encode("utf-8"),
                msg.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        headers.update({
            "OK-ACCESS-KEY": Config.OKX_API_KEY,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": Config.OKX_PASSPHRASE,
        })
        if Config.OKX_PROJECT_ID:
            headers["OK-ACCESS-PROJECT"] = Config.OKX_PROJECT_ID
        return headers

    # ── HTTP wrappers ─────────────────────────────────────────────
    # Return the `data` list on success and None on failure, so callers use the
    # same truthiness checks as the Bybit executor.

    def _get(
        self, endpoint: str, params: Dict = None, auth: bool = False
    ) -> Optional[List]:
        """Sync GET against OKX v5. Returns the data array, or None."""
        query = urllib.parse.urlencode(params) if params else ""
        request_path = f"{endpoint}?{query}" if query else endpoint
        url = f"{self._base_url}{request_path}"
        try:
            resp = self._session.get(
                url,
                headers=self._headers("GET", request_path, "", auth),
                timeout=15,
            )
            return self._unwrap(resp, endpoint)
        except Exception as e:
            logger.error(f"[OKX] GET {endpoint} error: {e}")
            return None

    def _post(self, endpoint: str, body: Dict | List) -> Optional[List]:
        """Sync POST against OKX v5. Returns the data array, or None."""
        # separators matter: the signed string must equal the bytes sent.
        body_str = json.dumps(body, separators=(",", ":"))
        url = f"{self._base_url}{endpoint}"
        try:
            resp = self._session.post(
                url,
                data=body_str,
                headers=self._headers("POST", endpoint, body_str, True),
                timeout=15,
            )
            return self._unwrap(resp, endpoint)
        except Exception as e:
            logger.error(f"[OKX] POST {endpoint} error: {e}")
            return None

    @staticmethod
    def _unwrap(resp: requests.Response, endpoint: str) -> Optional[List]:
        """Validate an OKX envelope: {"code":"0","msg":"","data":[...]}.

        OKX reports business failures with HTTP 200 and a non-zero `code`, and
        per-item failures inside data[].sCode even when the outer code is "0".
        Both are treated as failure so a rejected order never looks filled.
        """
        try:
            payload = resp.json()
        except ValueError:
            logger.error(
                f"[OKX] {endpoint} non-JSON response "
                f"(HTTP {resp.status_code}): {resp.text[:200]}"
            )
            return None

        code = str(payload.get("code", ""))
        data = payload.get("data", []) or []
        if code != "0":
            logger.error(
                f"[OKX] {endpoint} failed code={code} "
                f"msg={payload.get('msg', '')!r} data={str(data)[:200]}"
            )
            return None

        for item in data:
            if not isinstance(item, dict):
                continue
            s_code = str(item.get("sCode", "0") or "0")
            if s_code not in ("0", ""):
                logger.error(
                    f"[OKX] {endpoint} item rejected sCode={s_code} "
                    f"sMsg={item.get('sMsg', '')!r}"
                )
                return None
        return data

    # ── Symbol + instrument helpers ───────────────────────────────

    @staticmethod
    def _to_okx(symbol: str) -> str:
        """BTCUSDT / BTC/USDT:USDT / BTC-USDT-SWAP -> BTC-USDT-SWAP."""
        s = symbol.strip().upper().replace("/", "").replace(":USDT", "")
        if s.endswith("-SWAP"):
            return s
        if s.endswith("USDT"):
            return f"{s[:-4]}-USDT-SWAP"
        return s

    @staticmethod
    def _from_okx(inst_id: str) -> str:
        """BTC-USDT-SWAP -> BTCUSDT, so the tracker key stays provider-neutral."""
        parts = inst_id.upper().split("-")
        if len(parts) >= 2:
            return f"{parts[0]}{parts[1]}"
        return inst_id.upper()

    def _load_markets_sync(self) -> None:
        """Cache SWAP instrument specs (ctVal, lotSz, minSz, tickSz)."""
        data = self._get(
            "/api/v5/public/instruments", {"instType": "SWAP"}, auth=False
        )
        if not data:
            logger.error("[OKX] Failed to load instruments")
            return

        markets = {}
        for inst in data:
            inst_id = inst.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            try:
                markets[inst_id] = {
                    # ctVal = base currency units per contract. ctMult is
                    # almost always 1 but is applied for correctness.
                    "ct_val": float(inst.get("ctVal", 1) or 1),
                    "ct_mult": float(inst.get("ctMult", 1) or 1),
                    "lot_size": float(inst.get("lotSz", 1) or 1),
                    "min_size": float(inst.get("minSz", 1) or 1),
                    "tick_size": float(inst.get("tickSz", 0.0001) or 0.0001),
                    "max_leverage": float(inst.get("lever", 10) or 10),
                }
            except (TypeError, ValueError):
                continue

        self._markets = markets
        self._markets_loaded = True
        logger.info(f"[OKX] Loaded {len(markets)} USDT-SWAP instruments")

    async def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            await asyncio.to_thread(self._load_markets_sync)

    def _load_pos_mode_sync(self) -> None:
        """Detect account position mode once (net_mode | long_short_mode)."""
        data = self._get("/api/v5/account/config", auth=True)
        if data:
            self._pos_mode = str(data[0].get("posMode", "net_mode") or "net_mode")
            logger.info(f"[OKX] Account posMode={self._pos_mode}")
        else:
            # Fail safe: net_mode omits posSide, which is valid in net mode and
            # merely rejected in long_short_mode — a loud, recoverable error
            # rather than a silently wrong-sided position.
            self._pos_mode = "net_mode"
            logger.warning("[OKX] Could not read posMode; assuming net_mode")

    async def _ensure_pos_mode(self) -> None:
        if self._pos_mode is None:
            await asyncio.to_thread(self._load_pos_mode_sync)

    def _pos_side(self, action: str, closing: bool = False) -> Optional[str]:
        """posSide for hedge mode, or None when the account is in net mode.

        When closing, posSide names the position being reduced — NOT the side
        of the order that reduces it.
        """
        if self._pos_mode != "long_short_mode":
            return None
        return "long" if action == "LONG" else "short"

    def _spec(self, inst_id: str) -> Dict:
        """Instrument spec with conservative fallbacks."""
        return self._markets.get(inst_id, {
            "ct_val": 1.0, "ct_mult": 1.0, "lot_size": 1.0,
            "min_size": 1.0, "tick_size": 0.0001, "max_leverage": 10.0,
        })

    # ── Contract <-> coin conversion ──────────────────────────────
    # The single most dangerous difference from Bybit. Getting this wrong
    # sizes every order wrong by a factor of ctVal.

    def _coins_to_contracts(self, inst_id: str, coin_qty: float) -> float:
        """Coin quantity -> contract count, floored to lotSz.

        Floor (never round up) so a computed size cannot exceed the risk
        budget that max-loss sizing already approved.
        """
        spec = self._spec(inst_id)
        per_contract = spec["ct_val"] * spec["ct_mult"]
        if per_contract <= 0:
            return 0.0
        raw = coin_qty / per_contract
        lot = spec["lot_size"]
        if lot <= 0:
            return raw
        return math.floor(raw / lot) * lot

    def _contracts_to_coins(self, inst_id: str, contracts: float) -> float:
        """Contract count -> coin quantity (what the tracker stores)."""
        spec = self._spec(inst_id)
        return contracts * spec["ct_val"] * spec["ct_mult"]

    def _fmt_size(self, inst_id: str, contracts: float) -> str:
        """Format a contract count to lotSz precision for the API."""
        dp = self._qty_dp(self._spec(inst_id)["lot_size"])
        return f"{contracts:.{dp}f}"

    def _fmt_price(self, inst_id: str, price: float) -> str:
        """Format a price to tickSz precision for the API."""
        dp = self._price_dp(self._spec(inst_id)["tick_size"])
        return f"{price:.{dp}f}"

    # ── Fill confirmation ─────────────────────────────────────────

    def _get_fill_sync(
        self, inst_id: str, ord_id: str, expected_contracts: float,
        max_wait_s: float = 8.0,
    ) -> Optional[Dict]:
        """Poll /trade/fills for the actual VWAP of a market order.

        Returns {price, filled_qty (COINS), fees} on a confirmed full fill, or
        None on partial/ambiguous/timeout — same contract as the Bybit path so
        the caller's "unconfirmed fill" branch behaves identically.
        """
        if not ord_id:
            return None
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            data = self._get(
                "/api/v5/trade/fills",
                {"instType": "SWAP", "instId": inst_id, "ordId": ord_id},
                auth=True,
            )
            if data:
                total_contracts = 0.0
                weighted_px = 0.0
                total_fee = 0.0
                for f in data:
                    try:
                        q = float(f.get("fillSz", 0) or 0)
                        p = float(f.get("fillPx", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if q > 0 and p > 0:
                        total_contracts += q
                        weighted_px += p * q
                        # OKX reports fee as NEGATIVE when charged. Bybit's
                        # execFee is positive. abs() keeps the tracker's fee
                        # bookkeeping sign-consistent across providers.
                        try:
                            total_fee += abs(float(f.get("fee", 0) or 0))
                        except (TypeError, ValueError):
                            pass
                if total_contracts > 0:
                    tol = max(abs(expected_contracts) * 1e-8, 1e-12)
                    if total_contracts + tol >= expected_contracts:
                        return {
                            "price": round(weighted_px / total_contracts, 8),
                            "filled_qty": self._contracts_to_coins(
                                inst_id, total_contracts
                            ),
                            "filled_contracts": total_contracts,
                            "fees": round(total_fee, 6),
                        }
            time.sleep(0.5)
        logger.warning(
            f"[OKX] Full fill not confirmed within {max_wait_s}s "
            f"for ordId={ord_id} ({inst_id})"
        )
        return None

    def _position_contracts_sync(self, inst_id: str) -> float:
        """Current open contract count for an instrument (0.0 if flat)."""
        data = self._get(
            "/api/v5/account/positions",
            {"instType": "SWAP", "instId": inst_id},
            auth=True,
        )
        if not data:
            return 0.0
        for p in data:
            try:
                pos = float(p.get("pos", 0) or 0)
            except (TypeError, ValueError):
                continue
            if pos != 0:
                # Net mode reports shorts as negative; size is the magnitude.
                return abs(pos)
        return 0.0

    # ── SL helpers (algo orders) ──────────────────────────────────

    def _place_sl_sync(
        self, inst_id: str, action: str, contracts: float, sl_price: float
    ) -> Optional[List]:
        """Place a reduce-only conditional stop (OKX algo order).

        slOrdPx=-1 means "market on trigger", which matches the Bybit
        StopOrder+Market behaviour the tracker assumes.
        """
        body = {
            "instId": inst_id,
            "tdMode": self._td_mode,
            "side": "sell" if action == "LONG" else "buy",
            "ordType": "conditional",
            "sz": self._fmt_size(inst_id, contracts),
            "reduceOnly": True,
            "slTriggerPx": self._fmt_price(inst_id, sl_price),
            "slOrdPx": "-1",
            "slTriggerPxType": "mark",
        }
        return self._post("/api/v5/trade/order-algo", body)

    def _cancel_all_stop_orders_sync(self, inst_id: str) -> bool:
        """Cancel every pending conditional order for an instrument.

        Stops live in a separate algo namespace, so they must be listed and
        cancelled explicitly — the regular cancel-all endpoint ignores them.
        Returns True when nothing is left to cancel.
        """
        pending = self._get(
            "/api/v5/trade/orders-algo-pending",
            {"instType": "SWAP", "instId": inst_id, "ordType": "conditional"},
            auth=True,
        )
        if pending is None:
            return False
        algo_ids = [
            {"instId": inst_id, "algoId": o["algoId"]}
            for o in pending if o.get("algoId")
        ]
        if not algo_ids:
            return True   # nothing pending is success, not failure
        return self._post("/api/v5/trade/cancel-algos", algo_ids) is not None

    async def update_sl_roi(
        self, symbol: str, action: str, new_entry: float, leverage: float
    ) -> Optional[float]:
        """Re-anchor the stop after a DCA. Returns the new SL price, or None."""
        inst_id = self._to_okx(symbol)
        if Config.DRY_RUN:
            sl = self._calc_roi_sl(action, new_entry, leverage)
            logger.info(
                f"[OKX][DRY RUN] update_sl_roi {symbol}: "
                f"new_entry={new_entry:.6f} new_sl={sl:.6f}"
            )
            return sl

        await self._ensure_markets()
        spec = self._spec(inst_id)
        price_dp = self._price_dp(spec["tick_size"])

        contracts = await asyncio.to_thread(
            self._position_contracts_sync, inst_id
        )
        if contracts <= 0:
            logger.warning(f"[OKX] update_sl_roi: no open qty for {symbol}")
            return None

        new_sl = self._calc_roi_sl(
            action, new_entry, leverage, price_dp,
            max_price_move_pct=Config.SL_MAX_PRICE_MOVE_PCT,
        )

        if await asyncio.to_thread(self._cancel_all_stop_orders_sync, inst_id):
            logger.info(f"[OKX] Cancelled stop orders for {symbol}")
        else:
            logger.warning(
                f"[OKX] Could not cancel stop orders for {symbol} — placing anyway"
            )

        placed = await asyncio.to_thread(
            self._place_sl_sync, inst_id, action, contracts, new_sl
        )
        if placed is not None:
            logger.info(
                f"[OKX] New SL placed @ {new_sl:.6f} "
                f"({symbol} {action} avg_entry={new_entry:.6f} {leverage:.0f}x)"
            )
            return new_sl
        logger.warning(f"[OKX] Failed to place new SL for {symbol}")
        return None

    # ── Pre-trade ─────────────────────────────────────────────────

    def _set_leverage_sync(self, inst_id: str, leverage: int) -> bool:
        """Set leverage for an instrument. Non-fatal on failure."""
        body = {
            "instId": inst_id,
            "lever": str(int(leverage)),
            "mgnMode": self._td_mode,
        }
        return self._post("/api/v5/account/set-leverage", body) is not None

    # ── Entry ─────────────────────────────────────────────────────

    async def execute_signal(self, signal: Dict) -> Optional[Dict]:
        action = signal.get("action", "HOLD")
        if action not in ("LONG", "SHORT"):
            return None

        raw_symbol: str = signal.get("symbol", "")
        inst_id = self._to_okx(raw_symbol)
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
        position_pct = float(signal.get("position_size_percent", 1.0))

        if entry_price <= 0 or stop_loss <= 0:
            logger.warning(f"[OKX] Invalid levels entry={entry_price} sl={stop_loss}")
            return None

        # Defence-in-depth: main.py already materialises the ROI stop before RR
        # and max-loss sizing, but direct executor callers must behave equally.
        if Config.SL_MODE == "roi":
            stop_loss = self._calc_roi_sl(
                action, entry_price, leverage, price_dp=8,
                max_price_move_pct=Config.SL_MAX_PRICE_MOVE_PCT,
                atr_pct=float(signal.get("_atr_pct", 0.0) or 0.0),
                mode=str(signal.get("_mode", Config.TRADING_MODE)),
            )

        # DRY RUN
        if Config.DRY_RUN:
            # Re-price off the live ticker: the snapshot price is stale by the
            # time AI + filters finish, and for momentum signals it has usually
            # drifted WITH the signal, inflating dry-run edge.
            try:
                fresh = await self.get_last_price(raw_symbol)
                if fresh and fresh > 0:
                    drift = (fresh - entry_price) / entry_price * 100
                    if abs(drift) > 0.05:
                        logger.info(
                            f"[OKX][DRY RUN] {raw_symbol} entry re-priced "
                            f"{entry_price} → {fresh} ({drift:+.3f}% drift)"
                        )
                    entry_price = fresh
            except Exception:
                pass   # ticker failed → keep snapshot price
            logger.info(
                f"[OKX][DRY RUN] {action} {raw_symbol} "
                f"entry={entry_price:,}  sl={stop_loss:,}  lev={leverage}x"
            )
            return {
                "status": "dry_run",
                "exchange": "okx_swap",
                "symbol": raw_symbol,
                "action": action,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "leverage": leverage,
                "position_size_percent": position_pct,
            }

        # LIVE
        await self._ensure_markets()
        await self._ensure_pos_mode()

        if inst_id not in self._markets:
            logger.error(f"[OKX] {inst_id} not in OKX USDT-SWAP instruments")
            return None

        spec = self._spec(inst_id)
        min_sz = spec["min_size"]

        if not await asyncio.to_thread(self._set_leverage_sync, inst_id, leverage):
            logger.error(f"[OKX] Entry aborted: failed to set {leverage}x on {inst_id}")
            return None

        balance = await self.get_balance()
        if balance is None or balance <= 0:
            logger.error(f"[OKX] Entry aborted: live balance unavailable for {inst_id}")
            return None

        notional = balance * (position_pct / 100.0) * leverage
        coin_qty = notional / entry_price
        contracts = self._coins_to_contracts(inst_id, coin_qty)
        if contracts < min_sz:
            logger.warning(
                f"[OKX] {inst_id} risk-sized {contracts} contracts below "
                f"minSz={min_sz}; trade rejected instead of forcing a larger "
                "position"
            )
            return None

        side = "buy" if action == "LONG" else "sell"
        close_side = "sell" if action == "LONG" else "buy"

        entry_body = {
            "instId": inst_id,
            "tdMode": self._td_mode,
            "side": side,
            "ordType": "market",
            "sz": self._fmt_size(inst_id, contracts),
        }
        _ps = self._pos_side(action)
        if _ps:
            entry_body["posSide"] = _ps

        entry_result = await asyncio.to_thread(
            self._post, "/api/v5/trade/order", entry_body
        )
        if not entry_result:
            logger.error(f"[OKX] Entry order failed for {inst_id}")
            return None

        order_id = entry_result[0].get("ordId", "")
        fill = await asyncio.to_thread(
            self._get_fill_sync, inst_id, order_id, contracts
        )
        entry_fees = 0.0
        if fill:
            entry_price = float(fill["price"])
            filled_qty = float(fill["filled_qty"])          # coins
            filled_contracts = float(fill["filled_contracts"])
            entry_fees = float(fill.get("fees", 0.0) or 0.0)
            logger.info(
                f"[OKX] Entry {action} {filled_qty} {inst_id} @ {entry_price}: {order_id}"
            )
        else:
            # The market order was already SENT. Returning None here when it
            # actually filled would leave a real live position with no SL and
            # no tracking until the next sync scan.
            chk = await asyncio.to_thread(self._position_contracts_sync, inst_id)
            if chk > 0:
                filled_contracts = min(chk, contracts)
                filled_qty = self._contracts_to_coins(inst_id, filled_contracts)
                logger.warning(
                    f"[OKX] Entry fill unconfirmed but position exists on OKX "
                    f"(size={chk}) — proceeding with entry={entry_price} "
                    "(unreconciled) so SL still gets attached"
                )
            else:
                logger.error(
                    f"[OKX] Entry fill ambiguous for {inst_id} and no position "
                    "on OKX; refusing to create local position"
                )
                return None

        result: Dict = {
            "status": "executed",
            "exchange": "okx_swap",
            "symbol": raw_symbol,
            "action": action,
            "entry_order_id": order_id,
            "qty": filled_qty,                 # COINS, matching Bybit
            "actual_notional": round(filled_qty * entry_price, 8),
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "leverage": leverage,
            "fees": entry_fees,
            "sl_order_id": None,
            "tp1_order_id": None,
        }

        # Clear leftover conditional orders from previous positions on this
        # instrument before attaching a new stop.
        try:
            if await asyncio.to_thread(self._cancel_all_stop_orders_sync, inst_id):
                logger.info(f"[OKX] Cleaned old conditional orders for {inst_id}")
        except Exception as exc:
            logger.warning(f"[OKX] Order cleanup failed for {inst_id}: {exc}")

        if Config.SL_MODE == "none":
            logger.info(f"[OKX] SL_MODE=none — no SL placed for {inst_id}")
            return result

        if Config.SL_MODE == "roi":
            stop_loss = self._calc_roi_sl(
                action, entry_price, leverage,
                self._price_dp(spec["tick_size"]),
                max_price_move_pct=Config.SL_MAX_PRICE_MOVE_PCT,
                atr_pct=float(signal.get("_atr_pct", 0.0) or 0.0),
                mode=str(signal.get("_mode", "swing")),
            )
            sl_pct = (
                (stop_loss - entry_price) / entry_price * 100
                if action in ("LONG", "BUY")
                else (entry_price - stop_loss) / entry_price * 100
            )
            logger.info(
                f"[OKX] SL_MODE=roi: SL → {stop_loss:.6f} "
                f"({sl_pct:+.2f}% from entry, "
                f"cap={Config.SL_MAX_PRICE_MOVE_PCT}%, {leverage}x)"
            )
            # The tracker must police the stop the exchange actually has.
            result["stop_loss"] = stop_loss

        sl_result = await asyncio.to_thread(
            self._place_sl_sync, inst_id, action, filled_contracts, stop_loss
        )
        if sl_result is not None:
            result["sl_order_id"] = (
                sl_result[0].get("algoId", "") if sl_result else ""
            )
            logger.info(f"[OKX] SL placed @ {stop_loss}")
        else:
            logger.error(
                "[OKX] SL placement failed after entry; attempting emergency rollback"
            )
            rb_body = {
                "instId": inst_id,
                "tdMode": self._td_mode,
                "side": close_side,
                "ordType": "market",
                "sz": self._fmt_size(inst_id, filled_contracts),
                "reduceOnly": True,
            }
            if _ps:
                rb_body["posSide"] = _ps
            rollback = await asyncio.to_thread(
                self._post, "/api/v5/trade/order", rb_body
            )
            if rollback:
                logger.error(
                    f"[OKX] Unprotected {inst_id} entry rolled back immediately"
                )
                return None
            # Keep local tracking: returning None would orphan a real position.
            logger.critical(
                f"[OKX] CRITICAL: {inst_id} is open without exchange SL "
                "and emergency rollback failed"
            )
            result["status"] = "executed_unprotected"
            result["requires_manual_intervention"] = True
            return result

        if Config.EXCHANGE_TP_ORDERS_ENABLED:
            # Config.validate() rejects this flag outright, so this is a
            # belt-and-braces log rather than a reachable path.
            logger.warning(
                "[OKX] EXCHANGE_TP_ORDERS_ENABLED is not implemented for OKX; "
                "the local tracker owns partial exits"
            )
        else:
            logger.info(
                "[OKX] Exchange TP orders disabled; local position tracker owns "
                "partial exits"
            )
        return result

    # ── DCA ───────────────────────────────────────────────────────

    async def execute_dca(self, symbol: str, action: str, leverage: int,
                          dca_pct: float = 1.0) -> Optional[Dict]:
        """Add to an existing position (DCA).

        Returns the same shape as CEXExecutor.execute_dca; `qty` is in COINS so
        agent/positions.py can average it into the tracker unchanged.
        """
        inst_id = self._to_okx(symbol)

        if Config.DRY_RUN:
            logger.info(f"[OKX][DRY RUN] DCA {action} {symbol} +{dca_pct}%")
            return {
                "status": "dry_run_dca",
                "symbol": symbol,
                "action": action,
                "dca_pct": dca_pct,
            }

        await self._ensure_markets()
        await self._ensure_pos_mode()

        if inst_id not in self._markets:
            logger.error(f"[OKX] DCA: {inst_id} not in markets")
            return None

        spec = self._spec(inst_id)
        min_sz = spec["min_size"]

        current_price = await self.get_last_price(symbol)
        if not current_price or current_price <= 0:
            logger.error(f"[OKX] DCA: cannot get price for {inst_id}")
            return None

        balance = await self.get_balance() or Config.ACCOUNT_BALANCE
        notional = balance * (dca_pct / 100.0) * leverage
        contracts = self._coins_to_contracts(inst_id, notional / current_price)
        if contracts < min_sz:
            contracts = min_sz

        body = {
            "instId": inst_id,
            "tdMode": self._td_mode,
            "side": "buy" if action == "LONG" else "sell",
            "ordType": "market",
            "sz": self._fmt_size(inst_id, contracts),
        }
        _ps = self._pos_side(action)
        if _ps:
            body["posSide"] = _ps

        result = await asyncio.to_thread(self._post, "/api/v5/trade/order", body)
        if not result:
            logger.error(f"[OKX] DCA order failed for {inst_id}")
            return None

        order_id = result[0].get("ordId", "")
        coin_qty = self._contracts_to_coins(inst_id, contracts)
        logger.info(
            f"[OKX] DCA {action} +{coin_qty} {inst_id} @ ~{current_price:,.4f}: {order_id}"
        )
        return {
            "status": "dca_executed",
            "symbol": symbol,
            "action": action,
            "qty": coin_qty,
            "price": current_price,
            "dca_pct": dca_pct,
            "order_id": order_id,
        }

    # ── Manual partial/full close ─────────────────────────────────

    async def partial_close(
        self, symbol: str, action: str, close_pct: float
    ) -> Optional[Dict]:
        """Market-close close_pct% of an open position (reduce-only).

        Returns the same shape as CEXExecutor.partial_close, including the
        `unreconciled` flag, because main.py and pnl_tracker key off it.
        """
        close_pct = max(1.0, min(100.0, close_pct))
        inst_id = self._to_okx(symbol)

        await self._ensure_markets()
        spec = self._spec(inst_id)
        lot = spec["lot_size"]
        min_sz = spec["min_size"]

        if Config.DRY_RUN:
            logger.info(
                f"[OKX][DRY RUN] partial_close {symbol} {close_pct:.0f}% — skipped"
            )
            return {"symbol": symbol, "pct": close_pct, "dry_run": True}

        current = await asyncio.to_thread(self._position_contracts_sync, inst_id)
        if current <= 0:
            logger.warning(f"[OKX] partial_close: position size = 0 for {symbol}")
            return None

        # Floor to lotSz so we never over-close; exact qty for a full close so
        # rounding cannot leave a dust position behind.
        raw = current * close_pct / 100.0
        close_contracts = math.floor(raw / lot) * lot if lot > 0 else raw
        if close_pct >= 99.9:
            close_contracts = current

        if close_contracts < min_sz:
            logger.warning(
                f"[OKX] partial_close: computed size {close_contracts} < "
                f"minSz {min_sz} ({symbol} {close_pct:.0f}% of {current})"
            )
            return None

        close_side = "sell" if action == "LONG" else "buy"
        body = {
            "instId": inst_id,
            "tdMode": self._td_mode,
            "side": close_side,
            "ordType": "market",
            "sz": self._fmt_size(inst_id, close_contracts),
            "reduceOnly": True,
        }
        result = await asyncio.to_thread(self._post, "/api/v5/trade/order", body)
        if not result:
            logger.error(
                f"[OKX] partial_close FAILED for {symbol} {close_pct:.0f}%"
            )
            return None

        ord_id = result[0].get("ordId", "") if result else ""
        fill = await asyncio.to_thread(
            self._get_fill_sync, inst_id, ord_id, close_contracts
        )
        if not fill:
            # Reduce-only market orders almost always fill; usually only the
            # CONFIRMATION failed. Verify via position delta before giving up,
            # otherwise the retry queue can double-close a settled slice.
            after = await asyncio.to_thread(
                self._position_contracts_sync, inst_id
            )
            delta = current - after
            if delta + max(lot, 1e-12) >= close_contracts:
                px = await self.get_last_price(symbol) or 0.0
                logger.warning(
                    f"[OKX] partial_close fill unconfirmed but position size "
                    f"dropped {current}→{after} — treating as filled "
                    f"@ ticker {px} (unreconciled)"
                )
                return {
                    "symbol": symbol,
                    "action": action,
                    "close_side": close_side,
                    "pct": min(delta / current * 100.0, 100.0),
                    "requested_pct": close_pct,
                    "closed_qty": self._contracts_to_coins(inst_id, delta),
                    "total_qty": self._contracts_to_coins(inst_id, current),
                    "order_id": ord_id,
                    "price": px,
                    "unreconciled": True,
                }
            logger.error(
                f"[OKX] partial_close ambiguous/not fully filled for {symbol}; "
                "tracker will not be mutated"
            )
            return None

        actual_pct = min(
            fill["filled_contracts"] / current * 100.0, 100.0
        )
        logger.info(
            f"[OKX] partial_close OK: {symbol} {close_side} {close_contracts} "
            f"contracts ({close_pct:.0f}% of {current}) @ {fill['price']}"
        )
        return {
            "symbol": symbol,
            "action": action,
            "close_side": close_side,
            "pct": actual_pct,
            "requested_pct": close_pct,
            "closed_qty": fill["filled_qty"],
            "total_qty": self._contracts_to_coins(inst_id, current),
            "order_id": ord_id,
            "price": fill["price"],
            "fees": fill["fees"],
        }

    async def close_position(self, symbol: str) -> Optional[Dict]:
        inst_id = self._to_okx(symbol)
        if Config.DRY_RUN:
            return {"status": "dry_run_closed", "symbol": symbol}

        await self._ensure_markets()
        await self._ensure_pos_mode()

        data = await asyncio.to_thread(
            self._get, "/api/v5/account/positions",
            {"instType": "SWAP", "instId": inst_id}, True
        )
        if data is None:
            return None

        for pos in data:
            try:
                contracts = float(pos.get("pos", 0) or 0)
            except (TypeError, ValueError):
                continue
            if contracts == 0:
                continue

            # Net mode encodes a short as a negative size; hedge mode carries
            # the direction in posSide instead.
            pos_side = str(pos.get("posSide", "net") or "net")
            if pos_side == "long":
                action = "LONG"
            elif pos_side == "short":
                action = "SHORT"
            else:
                action = "LONG" if contracts > 0 else "SHORT"

            body = {
                "instId": inst_id,
                "tdMode": self._td_mode,
                "side": "sell" if action == "LONG" else "buy",
                "ordType": "market",
                "sz": self._fmt_size(inst_id, abs(contracts)),
                "reduceOnly": True,
            }
            _ps = self._pos_side(action)
            if _ps:
                body["posSide"] = _ps

            result = await asyncio.to_thread(
                self._post, "/api/v5/trade/order", body
            )
            if result:
                ord_id = result[0].get("ordId", "")
                logger.info(f"[OKX] Closed {symbol}: {ord_id}")
                return {"status": "closed", "symbol": symbol, "order_id": ord_id}

        logger.info(f"[OKX] No open position for {symbol}")
        return None

    # ── Read-only state ───────────────────────────────────────────

    def _pending_sl_map_sync(self) -> Dict[str, float]:
        """instId -> slTriggerPx for every pending conditional order.

        OKX does NOT report a position's stop on the positions endpoint (unlike
        Bybit's stopLoss field) because stops are separate algo orders. Without
        this lookup every synced position would look unprotected and the
        needs_sl_attach path would re-attach a stop that already exists.
        """
        data = self._get(
            "/api/v5/trade/orders-algo-pending",
            {"instType": "SWAP", "ordType": "conditional"},
            auth=True,
        )
        out: Dict[str, float] = {}
        if not data:
            return out
        for o in data:
            inst_id = o.get("instId", "")
            try:
                px = float(o.get("slTriggerPx", 0) or 0)
            except (TypeError, ValueError):
                continue
            if inst_id and px > 0:
                # Keep the tightest stop if several are pending.
                out[inst_id] = max(out.get(inst_id, 0.0), px)
        return out

    async def get_open_positions(self) -> List[Dict]:
        """Open positions in the same shape CEXExecutor returns.

        `contracts` is deliberately reported in COINS, not OKX contracts:
        pnl_tracker.sync_positions computes notional as contracts * entryPrice,
        so returning raw contract counts would misprice every synced position
        by a factor of ctVal.
        """
        if Config.DRY_RUN:
            return []

        await self._ensure_markets()

        data = await asyncio.to_thread(
            self._get, "/api/v5/account/positions",
            {"instType": "SWAP"}, True
        )
        if data is None:
            raise RuntimeError("OKX open-position request failed")

        sl_map = await asyncio.to_thread(self._pending_sl_map_sync)

        positions = []
        for pos in data:
            inst_id = pos.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            try:
                contracts = float(pos.get("pos", 0) or 0)
            except (TypeError, ValueError):
                continue
            if contracts == 0:
                continue

            pos_side = str(pos.get("posSide", "net") or "net")
            if pos_side == "long":
                side = "long"
            elif pos_side == "short":
                side = "short"
            else:
                side = "long" if contracts > 0 else "short"

            def _f(key: str, default: float = 0.0) -> float:
                try:
                    return float(pos.get(key, default) or default)
                except (TypeError, ValueError):
                    return default

            positions.append({
                "symbol": self._from_okx(inst_id),
                "side": side,
                # COINS, matching Bybit's base-coin size.
                "contracts": self._contracts_to_coins(inst_id, abs(contracts)),
                "entryPrice": _f("avgPx"),
                "leverage": _f("lever", 1.0) or 1.0,
                "unrealizedPnl": _f("upl"),
                "stopLoss": sl_map.get(inst_id, 0.0),   # 0 = unprotected
                "takeProfit": 0.0,   # tracker owns TP; none placed on OKX
            })
        return positions

    async def get_closed_pnl(
        self, symbol: str = None, limit: int = 10
    ) -> List[Dict]:
        """Recently closed positions with realized PnL, for live sync."""
        if Config.DRY_RUN:
            return []

        params: Dict = {"instType": "SWAP", "limit": str(limit)}
        if symbol:
            params["instId"] = self._to_okx(symbol)

        data = await asyncio.to_thread(
            self._get, "/api/v5/account/positions-history", params, True
        )
        if data is None:
            raise RuntimeError("OKX closed-PnL request failed")

        await self._ensure_markets()

        closed = []
        for item in data:
            inst_id = item.get("instId", "")

            def _f(key: str) -> float:
                try:
                    return float(item.get(key, 0) or 0)
                except (TypeError, ValueError):
                    return 0.0

            # direction: "long"/"short" on history rows.
            direction = str(item.get("direction", "") or "")
            closed.append({
                "symbol": self._from_okx(inst_id),
                "side": direction if direction in ("long", "short") else "long",
                "qty": self._contracts_to_coins(inst_id, _f("closeTotalPos")),
                "entry_price": _f("openAvgPx"),
                "exit_price": _f("closeAvgPx"),
                "realized_pnl": _f("realizedPnl") or _f("pnl"),
                "updated_time": item.get("uTime", ""),
            })
        return closed

    async def get_balance(self) -> Optional[float]:
        """USDT equity from the OKX trading account."""
        if Config.DRY_RUN:
            return None

        data = await asyncio.to_thread(
            self._get, "/api/v5/account/balance", {"ccy": "USDT"}, True
        )
        if not data:
            return None

        for account in data:
            for detail in account.get("details", []) or []:
                if detail.get("ccy") != "USDT":
                    continue
                for key in ("eq", "availEq", "cashBal"):
                    try:
                        val = float(detail.get(key, 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if val > 0:
                        return val
            # Fall back to the account-wide figure.
            try:
                total = float(account.get("totalEq", 0) or 0)
            except (TypeError, ValueError):
                total = 0.0
            if total > 0:
                return total
        return None

    async def get_last_price(self, symbol: str) -> Optional[float]:
        """Last traded price, or None."""
        data = await asyncio.to_thread(
            self._get, "/api/v5/market/ticker",
            {"instId": self._to_okx(symbol)}, False
        )
        try:
            if data:
                px = float(data[0].get("last", 0) or 0)
                return px if px > 0 else None
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        return None

    async def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
