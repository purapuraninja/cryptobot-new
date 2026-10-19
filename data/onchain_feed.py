"""
data/onchain_feed.py — Market regime & whale activity signals.

Data sources (all public endpoints, no auth required):
  - Binance USDT Futures public API:
      * topLongShortAccountRatio  — top-trader whale positioning (BTC proxy)
      * globalLongShortAccountRatio — retail + whale combined sentiment
      * takerlongshortRatio       — aggressive taker buy/sell volume momentum
  - Alternative.me Fear & Greed Index — sentiment proxy for MVRV/market phase

Note: True on-chain metrics (MVRV, SOPR, LTH/STH) require a paid Glassnode
subscription. These exchange-derived metrics are practical free proxies that
capture similar regime signals with minimal latency.

Regime mapping:
  F&G 0-25   → accumulation  (extreme fear, smart-money buying phase)
  F&G 26-45  → bear
  F&G 46-55  → neutral
  F&G 56-75  → bull
  F&G 76-100 → distribution  (extreme greed, top-formation risk)
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
import certifi

from utils.logger import logger

# ── Session (shared) ─────────────────────────────────────────────────────
_SESSION = requests.Session()
_SESSION.verify = certifi.where()
_TIMEOUT = 10

BINANCE_FUTURES_BASE = "https://fapi.binance.com"
FEAR_GREED_URL       = "https://api.alternative.me/fng/?limit=1"


class OnChainFeed:
    """
    Fetches and caches market regime + whale activity data.

    Cached for cache_ttl seconds (default 300 = 1 scan cycle) to avoid
    hammering external APIs on every symbol iteration.
    """

    def __init__(self, binance_api_key: str = "", cache_ttl: int = 300,
                 okx_web3_feed: Optional[Any] = None):
        self._api_key       = binance_api_key
        self._ttl           = cache_ttl
        self._okx_web3_feed = okx_web3_feed
        self._cache: Optional[Dict] = None
        self._cache_ts: float = 0.0
        # Multi-symbol whale activity (separate cache, same TTL)
        self._multi_cache: Optional[Dict] = None
        self._multi_cache_ts: float = 0.0

    # ── Internal helpers ──────────────────────────────────────────────────

    def _headers(self) -> Dict:
        if self._api_key:
            return {"X-MBX-APIKEY": self._api_key}
        return {}

    @staticmethod
    def _fetch_bybit_oi(symbol: str) -> Optional[Dict]:
        """
        Fetch OI change from Bybit v5 API.
        Returns {oi_value, oi_change_pct} or None.
        """
        try:
            sym = symbol.replace("/USDT:USDT", "USDT").replace("/", "").replace(":", "").upper()
            r = _SESSION.get(
                "https://api.bybit.com/v5/market/open-interest",
                params={"category": "linear", "symbol": sym, "intervalTime": "1h", "limit": 24},
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            rows = r.json().get("result", {}).get("list", [])
            if not rows:
                return None
            current = float(rows[0].get("openInterest", 0))
            old = float(rows[-1].get("openInterest", 0)) if len(rows) > 1 else current
            if old == 0:
                return {"oi_value": current, "oi_change_pct": 0.0}
            change = (current - old) / old * 100
            return {"oi_value": current, "oi_change_pct": round(change, 2)}
        except Exception:
            return None

    @staticmethod
    def _fetch_bybit_ticker(symbol: str) -> Optional[Dict]:
        """
        Fetch 24h price change from Bybit v5 API.
        Returns {price_24h_ago, price_change_pct} or None.
        """
        try:
            sym = symbol.replace("/USDT:USDT", "USDT").replace("/", "").replace(":", "").upper()
            r = _SESSION.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": sym},
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            rows = r.json().get("result", {}).get("list", [])
            if not rows:
                return None
            t = rows[0]
            last = float(t.get("lastPrice", 0))
            prev24 = float(t.get("prevPrice24h", 0))
            if prev24 == 0:
                return {"price_change_pct": 0.0}
            change = (last - prev24) / prev24 * 100
            return {"price_change_pct": round(change, 2)}
        except Exception:
            return None

    def _fetch_fear_greed(self) -> Dict:
        """Alternative.me Fear & Greed Index (0 = extreme fear, 100 = extreme greed)."""
        try:
            r = _SESSION.get(FEAR_GREED_URL, timeout=_TIMEOUT)
            r.raise_for_status()
            d = r.json()["data"][0]
            value = int(d["value"])
            label = d["value_classification"]

            if value <= 25:
                regime = "accumulation"
            elif value <= 45:
                regime = "bear"
            elif value <= 55:
                regime = "neutral"
            elif value <= 75:
                regime = "bull"
            else:
                regime = "distribution"

            return {"fng": value, "fng_label": label, "regime": regime}

        except Exception as exc:
            logger.warning(f"[OnChain] Fear&Greed fetch failed: {exc}")
            return {"fng": None, "fng_label": "unknown", "regime": "unknown"}

    def _fetch_top_ls_ratio(self, symbol: str = "BTCUSDT", period: str = "1h") -> Dict:
        """
        Binance top-trader long/short ACCOUNT ratio.
        Ratio > 1.3 = whales leaning long; < 0.77 = whales leaning short.
        """
        try:
            r = _SESSION.get(
                f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortAccountRatio",
                params={"symbol": symbol, "period": period, "limit": 1},
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                ratio     = float(data[0]["longShortRatio"])
                long_pct  = float(data[0]["longAccount"])
                short_pct = float(data[0]["shortAccount"])

                if ratio >= 1.5:
                    whale_bias = "strong_long"
                elif ratio >= 1.1:
                    whale_bias = "long"
                elif ratio <= 0.67:
                    whale_bias = "strong_short"
                elif ratio <= 0.9:
                    whale_bias = "short"
                else:
                    whale_bias = "neutral"

                return {
                    "ls_ratio":   round(ratio, 3),
                    "long_pct":   round(long_pct, 3),
                    "short_pct":  round(short_pct, 3),
                    "whale_bias": whale_bias,
                }
        except Exception as exc:
            logger.warning(f"[OnChain] Top L/S ratio fetch failed: {exc}")

        return {"ls_ratio": None, "whale_bias": "unknown"}

    def _fetch_global_ls_ratio(self, symbol: str = "BTCUSDT", period: str = "1h") -> Dict:
        """Binance global (all accounts) long/short account ratio."""
        try:
            r = _SESSION.get(
                f"{BINANCE_FUTURES_BASE}/futures/data/globalLongShortAccountRatio",
                params={"symbol": symbol, "period": period, "limit": 1},
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                return {"global_ls": round(float(data[0]["longShortRatio"]), 3)}
        except Exception as exc:
            logger.warning(f"[OnChain] Global L/S fetch failed: {exc}")

        return {"global_ls": None}

    def _fetch_taker_ratio(self, symbol: str = "BTCUSDT", period: str = "1h") -> Dict:
        """
        Binance taker buy/sell VOLUME ratio.
        > 1.2 = aggressive buyers dominate (bullish momentum)
        < 0.83 = aggressive sellers dominate (bearish momentum)
        """
        try:
            r = _SESSION.get(
                f"{BINANCE_FUTURES_BASE}/futures/data/takerlongshortRatio",
                params={"symbol": symbol, "period": period, "limit": 1},
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                ratio = float(data[0]["buySellRatio"])
                if ratio >= 1.2:
                    taker_bias = "buy_dominant"
                elif ratio <= 0.83:
                    taker_bias = "sell_dominant"
                else:
                    taker_bias = "neutral"
                return {"taker_ratio": round(ratio, 3), "taker_bias": taker_bias}
        except Exception as exc:
            logger.warning(f"[OnChain] Taker ratio fetch failed: {exc}")

        return {"taker_ratio": None, "taker_bias": "unknown"}

    def _fetch_top_ls_position_ratio(self, symbol: str = "BTCUSDT", period: str = "1h") -> Dict:
        """
        Binance top-trader long/short POSITION ratio (size-weighted).

        This is what Binance shows as "Smart Money" positioning.
        Unlike account ratio (counts heads), position ratio weights by USDT position size —
        a whale with $10M long counts far more than 1000 retail accounts.

        longPosition% > 60% → whales net long (position_whale_bias=long)
        longPosition% < 40% → whales net short (position_whale_bias=short)
        """
        try:
            r = _SESSION.get(
                f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortPositionRatio",
                params={"symbol": symbol, "period": period, "limit": 1},
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                ratio     = float(data[0]["longShortRatio"])
                long_pos  = float(data[0]["longAccount"])   # field name is longAccount even for position ratio
                short_pos = float(data[0]["shortAccount"])

                if ratio >= 1.5:
                    pos_bias = "strong_long"
                elif ratio >= 1.1:
                    pos_bias = "long"
                elif ratio <= 0.67:
                    pos_bias = "strong_short"
                elif ratio <= 0.9:
                    pos_bias = "short"
                else:
                    pos_bias = "neutral"

                return {
                    "pos_ratio":      round(ratio, 3),
                    "long_pos_pct":   round(long_pos, 3),
                    "short_pos_pct":  round(short_pos, 3),
                    "pos_whale_bias": pos_bias,
                }
        except Exception as exc:
            logger.warning(f"[OnChain] Position ratio fetch failed: {exc}")

        return {"pos_ratio": None, "pos_whale_bias": "unknown"}

    def _fetch_large_liquidations(self, lookback_ms: int = 3_600_000) -> Dict:
        """
        Fetch recent BTC liquidation orders to detect cascade risk.

        Strategy (try in order):
          1. Binance allForceOrders without symbol (more permissive)
          2. OKX public liquidation orders (reliable fallback)
          3. Silent return "calm" — liquidation is nice-to-have, not critical

        Large SELL liquidations = longs blown out → cascading sell pressure.
        Large BUY  liquidations = shorts blown out → short squeeze / buy pressure.
        """
        import time as _time

        def _parse(orders: list, qty_key: str, price_key: str, side_key: str,
                   sell_val: str) -> Dict:
            long_liq = short_liq = 0.0
            for o in orders:
                try:
                    val = float(o.get(qty_key, 0)) * float(o.get(price_key, 0))
                    if o.get(side_key) == sell_val:
                        long_liq += val
                    else:
                        short_liq += val
                except (TypeError, ValueError):
                    pass
            total = long_liq + short_liq
            if total < 500_000:
                sig = "calm"
            elif long_liq > short_liq * 2.5:
                sig = "longs_squeezed"
            elif short_liq > long_liq * 2.5:
                sig = "shorts_squeezed"
            else:
                sig = "mixed"
            return {
                "liq_signal":    sig,
                "liq_usdt":      round(total / 1_000_000, 2),
                "long_liq_usdt": round(long_liq / 1_000_000, 2),
                "shrt_liq_usdt": round(short_liq / 1_000_000, 2),
            }

        now_ms   = int(_time.time() * 1000)
        start_ms = now_ms - lookback_ms

        # ── Attempt 1: Binance (no symbol — broader query) ────────────────
        try:
            r = _SESSION.get(
                f"{BINANCE_FUTURES_BASE}/fapi/v1/allForceOrders",
                params={"startTime": start_ms, "endTime": now_ms, "limit": 200},
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            orders = r.json()
            if isinstance(orders, list):
                # Filter BTC only from all-symbol response
                btc = [o for o in orders if "BTC" in o.get("symbol", "")]
                return _parse(btc or orders, "origQty", "price", "side", "SELL")
        except Exception as exc:
            logger.debug(f"[OnChain] Binance liquidation unavailable: {exc}")

        # ── Attempt 2: OKX public liquidation endpoint ────────────────────
        try:
            r = _SESSION.get(
                "https://www.okx.com/api/v5/public/liquidation-orders",
                params={
                    "instType": "SWAP",
                    "mgnMode":  "cross",
                    "instId":   "BTC-USDT-SWAP",
                    "state":    "filled",
                },
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json().get("data", [{}])
            details = data[0].get("details", []) if data else []
            if details:
                return _parse(details, "sz", "bkPx", "side", "sell")
        except Exception as exc:
            logger.debug(f"[OnChain] OKX liquidation unavailable: {exc}")

        # ── All failed — return calm (non-critical feature) ───────────────
        return {"liq_signal": "calm", "liq_usdt": 0.0,
                "long_liq_usdt": 0.0, "shrt_liq_usdt": 0.0}

    # ── Multi-symbol whale activity ───────────────────────────────────────

    @staticmethod
    def calculate_whale_impact(
        oi_change_pct: float,
        taker_ratio: float,
        price_change_pct: float,
        pos_trend_delta: float,
    ) -> Dict:
        """
        Hitung impact whale ke harga berdasarkan:
        - OI change % (posisi baru dibuka/tutup)
        - Taker ratio (aggressive buying/selling)
        - Price change % (arah harga)
        - Position ratio trend (whale positioning direction)

        Returns:
            impact_score: float (0-10, higher = stronger impact)
            signal: str (stealth_accumulation, whale_accumulation, distribution_top,
                         whale_distribution, neutral)
            direction: str (strongly_bullish, bullish, neutral, bearish, strongly_bearish)
        """
        impact = 0.0
        signal = "neutral"
        direction = "neutral"

        # Normalize taker ratio to delta from 1.0
        taker_delta = taker_ratio - 1.0 if taker_ratio else 0.0

        # ── Whale Accumulation (OI up + buy dominant + price up) ────────
        if oi_change_pct > 3 and taker_delta > 0.1 and price_change_pct > 0:
            impact = oi_change_pct * taker_delta
            signal = "whale_accumulation"
            direction = "bullish"

        # ── Whale Distribution (OI up + sell dominant + price down) ─────
        elif oi_change_pct > 3 and taker_delta < -0.1 and price_change_pct < 0:
            impact = oi_change_pct * abs(taker_delta)
            signal = "whale_distribution"
            direction = "bearish"

        # ── Stealth Accumulation (OI up + buy dominant + price DOWN) ────
        # Whale membeli diam-diam saat harga turun = bullish divergence
        elif oi_change_pct > 3 and taker_delta > 0.1 and price_change_pct < -2:
            impact = oi_change_pct * taker_delta * 1.5  # bonus weight
            signal = "stealth_accumulation"
            direction = "strongly_bullish"

        # ── Distribution Top (OI up + sell dominant + price UP) ─────────
        # Whale menjual diam-diam saat harga naik = bearish divergence
        elif oi_change_pct > 3 and taker_delta < -0.1 and price_change_pct > 2:
            impact = oi_change_pct * abs(taker_delta) * 1.5
            signal = "distribution_top"
            direction = "strongly_bearish"

        # ── Short Squeeze Setup (OI turun + price naik tajam) ───────────
        elif oi_change_pct < -5 and price_change_pct > 5:
            impact = abs(oi_change_pct) * 0.5
            signal = "short_squeeze"
            direction = "bullish"

        # ── Long Liquidation Cascade (OI turun + price turun tajam) ─────
        elif oi_change_pct < -5 and price_change_pct < -5:
            impact = abs(oi_change_pct) * 0.5
            signal = "long_liquidation"
            direction = "bearish"

        # ── Whale Position Trend Confirmation ───────────────────────────
        # Jika pos_ratio trend searah dengan taker, boost impact
        if abs(pos_trend_delta) > 0.05:
            if (pos_trend_delta > 0 and taker_delta > 0) or \
               (pos_trend_delta < 0 and taker_delta < 0):
                impact *= 1.3  # whale trend + taker searah = stronger signal

        # Cap impact score at 10
        impact = min(round(impact, 2), 10.0)

        return {
            "whale_impact": impact,
            "whale_signal": signal,
            "whale_direction": direction,
        }

    def _fetch_symbol_whale_metrics_sync(
        self, symbol: str, period: str = "1h"
    ) -> Optional[Dict]:
        """
        Fetch pos_ratio (3 periods for trend), acct_ratio, taker_ratio for one symbol.

        Uses limit=3 so we get a short trend window from a single API call —
        no persistent history needed. Returns None if symbol is not on Binance
        Futures (many altcoins aren't).
        """
        try:
            # ── Position ratio (primary — size-weighted, limit=3 for trend) ──
            r = _SESSION.get(
                f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortPositionRatio",
                params={"symbol": symbol, "period": period, "limit": 3},
                headers=self._headers(),
                timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                return None
            pos_data = r.json()
            if not pos_data or not isinstance(pos_data, list) or len(pos_data) == 0:
                return None

            # Latest reading
            latest   = pos_data[-1]
            pos_ratio = round(float(latest["longShortRatio"]), 4)
            long_pos  = round(float(latest["longAccount"]), 4)
            short_pos = round(float(latest["shortAccount"]), 4)

            # Absolute bias
            if pos_ratio >= 1.5:   pos_bias = "strong_long"
            elif pos_ratio >= 1.1: pos_bias = "long"
            elif pos_ratio <= 0.67: pos_bias = "strong_short"
            elif pos_ratio <= 0.9: pos_bias = "short"
            else:                  pos_bias = "neutral"

            # Trend: change from oldest to latest in the 3-period window
            if len(pos_data) >= 2:
                oldest = float(pos_data[0]["longShortRatio"])
                trend_delta = round(pos_ratio - oldest, 4)
            else:
                trend_delta = 0.0

            if trend_delta > 0.05:    trend_dir = "rising"
            elif trend_delta < -0.05: trend_dir = "falling"
            else:                     trend_dir = "flat"

            result: Dict = {
                "pos_ratio":   pos_ratio,
                "long_pos":    long_pos,
                "short_pos":   short_pos,
                "pos_bias":    pos_bias,
                "trend_delta": trend_delta,
                "trend_dir":   trend_dir,
            }

            # ── Account ratio (secondary — head count) ───────────────────────
            try:
                r2 = _SESSION.get(
                    f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortAccountRatio",
                    params={"symbol": symbol, "period": period, "limit": 1},
                    headers=self._headers(),
                    timeout=_TIMEOUT,
                )
                if r2.status_code == 200:
                    d2 = r2.json()
                    if d2 and isinstance(d2, list):
                        ratio2 = float(d2[0]["longShortRatio"])
                        if ratio2 >= 1.5:   acct_bias = "strong_long"
                        elif ratio2 >= 1.1: acct_bias = "long"
                        elif ratio2 <= 0.67: acct_bias = "strong_short"
                        elif ratio2 <= 0.9: acct_bias = "short"
                        else:              acct_bias = "neutral"
                        result["acct_ratio"] = round(ratio2, 4)
                        result["acct_bias"]  = acct_bias
            except Exception:
                pass

            # ── Taker ratio (momentum) ────────────────────────────────────────
            try:
                r3 = _SESSION.get(
                    f"{BINANCE_FUTURES_BASE}/futures/data/takerlongshortRatio",
                    params={"symbol": symbol, "period": period, "limit": 1},
                    headers=self._headers(),
                    timeout=_TIMEOUT,
                )
                if r3.status_code == 200:
                    d3 = r3.json()
                    if d3 and isinstance(d3, list):
                        ratio3 = float(d3[0]["buySellRatio"])
                        if ratio3 >= 1.2:   taker_bias = "buy_dominant"
                        elif ratio3 <= 0.83: taker_bias = "sell_dominant"
                        else:               taker_bias = "neutral"
                        result["taker_ratio"] = round(ratio3, 4)
                        result["taker_bias"]  = taker_bias
            except Exception:
                pass

            # ── Inflow Score (composite, per symbol) ─────────────────────────
            # Combines all 3 signals into a single comparable score.
            #
            # Components (all scale ≈ −1 to +1):
            #   trend_delta × 3.0  → direction of whale position change (primary)
            #   pos_bias           → current absolute positioning level (+1/+0.5/…)
            #   taker_bias         → aggressive volume momentum (+0.75/0/−0.75)
            #   acct_bias          → head-count corroboration (+0.25/0/−0.25)
            #
            # Positive score = accumulation; negative = distribution.
            score = 0.0

            # Position ratio trend (main signal)
            score += result["trend_delta"] * 3.0

            # Absolute position bias
            _pb = result.get("pos_bias", "neutral")
            if _pb == "strong_long":   score += 1.0
            elif _pb == "long":        score += 0.5
            elif _pb == "strong_short": score -= 1.0
            elif _pb == "short":       score -= 0.5

            # Taker momentum
            _tb = result.get("taker_bias", "neutral")
            if _tb == "buy_dominant":   score += 0.75
            elif _tb == "sell_dominant": score -= 0.75

            # Account ratio (corroboration)
            _ab = result.get("acct_bias", "neutral")
            if _ab == "strong_long":   score += 0.25
            elif _ab == "long":        score += 0.125
            elif _ab == "strong_short": score -= 0.25
            elif _ab == "short":       score -= 0.125

            result["inflow_score"] = round(score, 3)

            # ── Fetch OI + Price from Bybit for whale impact calculation ──
            oi_data = self._fetch_bybit_oi(symbol)
            price_data = self._fetch_bybit_ticker(symbol)

            oi_change = oi_data.get("oi_change_pct", 0.0) if oi_data else 0.0
            price_change = price_data.get("price_change_pct", 0.0) if price_data else 0.0
            taker_ratio_val = result.get("taker_ratio", 1.0)

            result["oi_change_pct"] = oi_change
            result["price_change_pct"] = price_change

            # ── Calculate Whale Impact ────────────────────────────────────
            impact_data = self.calculate_whale_impact(
                oi_change_pct=oi_change,
                taker_ratio=taker_ratio_val,
                price_change_pct=price_change,
                pos_trend_delta=trend_delta,
            )
            result["whale_impact"] = impact_data["whale_impact"]
            result["whale_signal"] = impact_data["whale_signal"]
            result["whale_direction"] = impact_data["whale_direction"]

            return result

        except Exception as exc:
            logger.debug(f"[OnChain] {symbol} whale metrics failed: {exc}")
            return None

    def fetch_multi_symbol_whale_activity(
        self,
        symbols: List[str],
        period: str = "1h",
        max_symbols: int = 10,
    ) -> Dict:
        """
        Parallel-fetch whale activity for up to max_symbols symbols and compute
        three smart money analyses:

        1. **Smart Money Rotation Detector**
           Compare each symbol's inflow_score delta relative to BTC.
           Large divergence (symbol rising while BTC flat/falling) = rotation signal.

        2. **Whale Position Trend**
           Symbols where pos_ratio is rising/falling over the last 3 periods.

        3. **Smart Money Inflow Score**
           Combined score: trend_delta × 3 + pos_bias + taker_bias + acct_bias.
           Top-scoring symbols are flagged as ACCUMULATE; bottom = DISTRIBUTE.

        Returns dict:
          "by_symbol"      : {SYM: full metrics dict}
          "accumulating"   : [up to 3 (sym, score) tuples, descending]
          "distributing"   : [up to 3 (sym, score) tuples, ascending]
          "rotation_to"    : symbol gaining most vs BTC (or None)
          "rotation_from"  : symbol losing most vs BTC (or None)
          "available_count": how many symbols had Binance Futures data
        """
        now = time.time()
        if self._multi_cache and (now - self._multi_cache_ts) < self._ttl:
            return self._multi_cache

        # Cap and deduplicate
        target = list(dict.fromkeys(s.upper() for s in symbols))[:max_symbols]

        logger.info(
            f"[OnChain] Fetching multi-symbol whale data for "
            f"{len(target)} symbol(s) (period={period}) ..."
        )

        by_symbol: Dict[str, Dict] = {}

        # ── Parallel fetch (5 workers — IO-bound) ────────────────────────────
        def _fetch_one(sym: str):
            return sym, self._fetch_symbol_whale_metrics_sync(sym, period)

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_one, sym): sym for sym in target}
            for fut in as_completed(futures):
                sym, metrics = fut.result()
                if metrics is not None:
                    by_symbol[sym] = metrics

        if not by_symbol:
            empty = {
                "by_symbol": {},
                "accumulating": [],
                "distributing": [],
                "rotation_to":   None,
                "rotation_from": None,
                "available_count": 0,
            }
            self._multi_cache    = empty
            self._multi_cache_ts = now
            return empty

        # ── Sort by inflow score ──────────────────────────────────────────────
        scored = sorted(
            by_symbol.items(),
            key=lambda kv: kv[1].get("inflow_score", 0.0),
            reverse=True,
        )

        accumulating = [(s, d["inflow_score"]) for s, d in scored if d["inflow_score"] > 0.1][:3]
        distributing = [(s, d["inflow_score"]) for s, d in reversed(scored) if d["inflow_score"] < -0.1][:3]

        # ── Rotation Detector: compare each symbol vs BTC baseline ───────────
        rotation_to   = None
        rotation_from = None
        btc_score = by_symbol.get("BTCUSDT", {}).get("inflow_score", 0.0)

        if len(by_symbol) >= 2:
            # Best non-BTC symbol gaining vs BTC
            non_btc = [(s, d) for s, d in scored if s != "BTCUSDT"]
            if non_btc:
                top_sym, top_d = non_btc[0]
                # Rotation signal: top symbol outflows BTC by > 0.5 score units
                if top_d["inflow_score"] - btc_score > 0.5:
                    rotation_to = top_sym

                bot_sym, bot_d = non_btc[-1]
                if btc_score - bot_d["inflow_score"] > 0.5:
                    rotation_from = bot_sym

        result: Dict = {
            "by_symbol":       by_symbol,
            "accumulating":    accumulating,
            "distributing":    distributing,
            "rotation_to":     rotation_to,
            "rotation_from":   rotation_from,
            "available_count": len(by_symbol),
        }

        avail = ", ".join(by_symbol.keys())
        logger.info(
            f"[OnChain] Multi-symbol done — {len(by_symbol)}/{len(target)} available: {avail}"
        )
        top_3 = ", ".join(f"{s}({sc:+.2f})" for s, sc in accumulating)
        if top_3:
            logger.info(f"[OnChain] Accumulating: {top_3}")

        self._multi_cache    = result
        self._multi_cache_ts = now
        return result

    # ── Public API ────────────────────────────────────────────────────────

    def fetch(self, trading_mode: str = "swing", symbols: Optional[List[str]] = None) -> Dict:
        """
        Fetch all regime/whale data. Returns cached result if within TTL.

        trading_mode affects API period selection:
          "intraday" → 1h periods (faster signal, matches 15M–1H hold)
          "swing"    → 4h periods for position ratio (smoother whale signal,
                       matches 4H–Daily hold; reduces 1h noise)

        Liquidation data is always real-time (no period param).
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._ttl:
            return self._cache

        # Period selection by mode
        # Intraday: 1h granularity (reactive)
        # Swing: 4h for position/account ratio (stable whale view), 1h taker
        ls_period  = "1h"
        pos_period = "1h" if trading_mode == "intraday" else "4h"
        tk_period  = "1h"

        logger.info(
            f"[OnChain] Fetching regime data "
            f"(mode={trading_mode}, pos_period={pos_period}) ..."
        )

        fg  = self._fetch_fear_greed()
        ls  = self._fetch_top_ls_ratio(period=ls_period)
        pr  = self._fetch_top_ls_position_ratio(period=pos_period)
        gl  = self._fetch_global_ls_ratio(period=ls_period)
        tk  = self._fetch_taker_ratio(period=tk_period)
        liq = self._fetch_large_liquidations()

        result: Dict = {**fg, **ls, **pr, **gl, **tk, **liq}
        result["ts"] = datetime.now(timezone.utc).strftime("%H:%M UTC")

        # ── Composite regime score ────────────────────────────────────────
        # Weights: position_ratio (Smart Money) > account_ratio > F&G > taker
        # Liquidation cascades are applied as urgency overrides, not score voters.
        score = 0.0
        regime    = result.get("regime", "unknown")
        acct_bias = result.get("whale_bias", "unknown")     # account ratio
        pos_bias  = result.get("pos_whale_bias", "unknown") # position ratio (Smart Money)
        taker_bias = result.get("taker_bias", "unknown")
        liq_signal = result.get("liq_signal", "unknown")

        # F&G regime (weight 1.0)
        if regime == "accumulation":
            score += 1.0
        elif regime == "bull":
            score += 0.5
        elif regime == "bear":
            score -= 0.5
        elif regime == "distribution":
            score -= 1.0

        # Smart Money position ratio (weight 2.0 — highest, size-weighted)
        if pos_bias == "strong_long":
            score += 2.0
        elif pos_bias == "long":
            score += 1.0
        elif pos_bias == "strong_short":
            score -= 2.0
        elif pos_bias == "short":
            score -= 1.0

        # Account ratio (weight 1.0 — confirms Smart Money or diverges)
        if acct_bias == "strong_long":
            score += 1.0
        elif acct_bias == "long":
            score += 0.5
        elif acct_bias == "strong_short":
            score -= 1.0
        elif acct_bias == "short":
            score -= 0.5

        # Taker ratio (weight 0.5 — momentum signal)
        if taker_bias == "buy_dominant":
            score += 0.5
        elif taker_bias == "sell_dominant":
            score -= 0.5

        if score >= 2.5:
            composite = "strongly_bullish"
        elif score >= 1.0:
            composite = "bullish"
        elif score <= -2.5:
            composite = "strongly_bearish"
        elif score <= -1.0:
            composite = "bearish"
        else:
            composite = "neutral"

        result["composite"] = composite
        result["composite_score"] = round(score, 2)

        # ── Multi-symbol whale activity (optional) ────────────────────────
        if symbols:
            ms_period = "1h"  # always 1h — taker ratio only available in 1h/4h
            flows = self.fetch_multi_symbol_whale_activity(symbols, period=ms_period)
            result["smart_money_flows"] = flows
        else:
            result["smart_money_flows"] = {}

        # ── OKX Web3 token intelligence (optional, fail-open) ─────────────
        if self._okx_web3_feed is not None and symbols:
            try:
                result["okx_web3"] = self._okx_web3_feed.fetch_for_symbols(symbols)
                okx = result["okx_web3"]
                by_symbol = okx.get("by_symbol", {})
                auto_resolved = okx.get("auto_resolved") or {}
                summary = []
                for sym, item in sorted(by_symbol.items()):
                    risk = item.get("risk") or {}
                    advanced = item.get("advanced") or {}
                    price = item.get("price") or {}
                    risk_level = risk.get("risk_level") or advanced.get("risk_level") or "N/A"
                    flags = []
                    if risk.get("is_wash"):
                        flags.append("WASH")
                    if risk.get("is_rugpull"):
                        flags.append("RUGPULL")
                    if advanced.get("is_honeypot"):
                        flags.append("HONEYPOT")
                    if advanced.get("low_liquidity"):
                        flags.append("LOW_LIQ")
                    if advanced.get("smart_money_buy"):
                        flags.append("SM_BUY")
                    flag_str = ",".join(flags) if flags else "ok"
                    liq = price.get("liquidity")
                    liq_str = f"liq=${float(liq):,.0f}" if liq is not None else "liq=N/A"
                    resolved_tag = " auto" if sym in auto_resolved else ""
                    summary.append(f"{sym}{resolved_tag}(risk={risk_level}, {flag_str}, {liq_str})")

                if summary:
                    detail = "; ".join(summary)
                elif okx.get("mapped_count", 0) > 0:
                    detail = "no mapped symbols in current scan" if okx.get("requested_count", 0) == 0 else "no OKX data returned"
                else:
                    detail = "no token map configured"

                logger.info(
                    f"[OKXWeb3] mapped={okx.get('mapped_count', 0)}  "
                    f"requested={okx.get('requested_count', 0)}  "
                    f"available={okx.get('available_count', 0)}  "
                    f"auto_resolved={len(auto_resolved)}  {detail}"
                )

                errors = okx.get("errors") or {}
                if errors:
                    err_bits = []
                    for sym, err_list in sorted(errors.items()):
                        if err_list:
                            err_bits.append(f"{sym}: {err_list[0]}")
                    if err_bits:
                        logger.warning(f"[OKXWeb3] partial errors: {' | '.join(err_bits[:3])}")
            except Exception as exc:
                logger.debug(f"[OKXWeb3] Enrichment skipped: {exc}")
                result["okx_web3"] = {"enabled": True, "by_symbol": {}, "errors": {"_feed": [str(exc)]}}
        else:
            result["okx_web3"] = {}

        self._cache    = result
        self._cache_ts = now

        logger.info(
            f"[OnChain] F&G={result.get('fng')} ({regime})  "
            f"SmartMoney(pos)={pos_bias}  acct={acct_bias}  "
            f"taker={taker_bias}  liq={liq_signal}  "
            f"composite={composite} (score={score:.2f})"
        )
        return result

    @staticmethod
    def format_for_prompt(data: Dict, trading_mode: str = "swing") -> str:
        """
        Build a text block to append to the AI system prompt.

        trading_mode="swing"    : full regime modifiers (±0.4/±0.7).
                                  F&G drives score — regime lasts days/weeks.
        trading_mode="intraday" : reduced modifiers (±0.2 max).
                                  Only taker ratio is directionally actionable;
                                  F&G is informational context only — a daily
                                  metric is too coarse to veto a 30-min setup.
        """
        if not data:
            return ""

        is_intraday = trading_mode == "intraday"

        # ── Smart money flows section (built once, appended to both modes) ──
        flows_section = OnChainFeed._format_smart_money_flows(
            data.get("smart_money_flows", {})
        )
        okx_web3_section = OnChainFeed._format_okx_web3(data.get("okx_web3", {}))

        fng        = data.get("fng")
        fng_label  = data.get("fng_label", "?")
        regime     = data.get("regime", "unknown")
        composite  = data.get("composite", "neutral")
        comp_sc    = data.get("composite_score", 0)

        # Account ratio (head count)
        ls         = data.get("ls_ratio")
        whale      = data.get("whale_bias", "unknown")
        # Position ratio (Smart Money — size weighted)
        pos_r      = data.get("pos_ratio")
        pos_bias   = data.get("pos_whale_bias", "unknown")
        long_pos   = data.get("long_pos_pct")
        short_pos  = data.get("short_pos_pct")
        # Global + taker
        gl         = data.get("global_ls")
        taker      = data.get("taker_ratio")
        tk_bias    = data.get("taker_bias", "unknown")
        # Liquidations
        liq_signal = data.get("liq_signal", "unknown")
        liq_usdt   = data.get("liq_usdt", 0.0)
        long_liq   = data.get("long_liq_usdt", 0.0)
        shrt_liq   = data.get("shrt_liq_usdt", 0.0)
        ts         = data.get("ts", "")

        fng_str    = f"{fng}/100 ({fng_label})" if fng is not None else "N/A"
        ls_str     = f"{ls} ({whale})" if ls is not None else "N/A"
        gl_str     = str(gl) if gl is not None else "N/A"
        tk_str     = f"{taker} ({tk_bias})" if taker is not None else "N/A"

        # Smart Money position ratio string
        if pos_r is not None and long_pos is not None:
            pos_str = (
                f"{pos_r} ({pos_bias})  "
                f"long={long_pos*100:.1f}% / short={short_pos*100:.1f}%"
            )
        else:
            pos_str = "N/A"

        # Liquidation cascade string
        if liq_signal == "longs_squeezed":
            liq_str = f"LONGS SQUEEZED ${liq_usdt:.1f}M (long_liq=${long_liq:.1f}M > short_liq=${shrt_liq:.1f}M) — cascading SELL pressure"
        elif liq_signal == "shorts_squeezed":
            liq_str = f"SHORTS SQUEEZED ${liq_usdt:.1f}M (short_liq=${shrt_liq:.1f}M > long_liq=${long_liq:.1f}M) — cascading BUY pressure"
        elif liq_signal == "mixed":
            liq_str = f"mixed ${liq_usdt:.1f}M (long=${long_liq:.1f}M / short=${shrt_liq:.1f}M)"
        elif liq_signal == "calm":
            liq_str = "calm (< $0.5M recent liquidations)"
        else:
            liq_str = "N/A"

        # Liquidation urgency note (applies to both modes)
        liq_note = ""
        if liq_signal == "longs_squeezed":
            liq_note = (
                "\n⚡ LONG LIQUIDATION CASCADE: Large longs being wiped — "
                "avoid new LONGs until cascade settles. SHORT momentum elevated."
            )
        elif liq_signal == "shorts_squeezed":
            liq_note = (
                "\n⚡ SHORT SQUEEZE ACTIVE: Large shorts being blown out — "
                "avoid new SHORTs. LONG momentum elevated, but may be exhaustion top."
            )

        if is_intraday:
            # ── INTRADAY: taker + position ratio are primary signals ───────────
            # F&G is backdrop only. Smart Money position ratio is the key whale signal.
            # Liquidation cascades ARE actionable intraday — apply as urgency override.

            if composite in ("strongly_bullish", "bullish"):
                mod_long  = "+0.3 (Smart Money net long — supports LONG momentum)"
                mod_short = "-0.3 (Smart Money net long — raise bar for SHORT entries)"
            elif composite in ("strongly_bearish", "bearish"):
                mod_long  = "-0.3 (Smart Money net short — raise bar for LONG entries)"
                mod_short = "+0.3 (Smart Money net short — supports SHORT momentum)"
            else:
                mod_long  = "no modifier"
                mod_short = "no modifier"

            if tk_bias == "buy_dominant":
                taker_note = "  Taker buyers aggressive → LONG momentum continuation signal."
            elif tk_bias == "sell_dominant":
                taker_note = "  Taker sellers aggressive → SHORT momentum continuation signal."
            else:
                taker_note = "  Taker balanced → no momentum edge."

            return (
                f"\n\nMARKET CONTEXT ({ts}) [INTRADAY]:\n"
                f"- Fear & Greed: {fng_str}  (macro backdrop, informational)\n"
                f"- Smart Money Position Ratio: {pos_str}  ← PRIMARY whale signal\n"
                f"- Top-Trader Account Ratio: {ls_str}\n"
                f"- Global L/S Ratio: {gl_str}\n"
                f"- Taker Buy/Sell Ratio: {tk_str}  ← momentum signal\n"
                f"- Recent Liquidations (BTC): {liq_str}\n"
                f"- COMPOSITE: {composite} (score={comp_sc})"
                f"{liq_note}\n\n"
                f"REGIME MODIFIER (intraday — tiebreaker, do NOT block valid setups):\n"
                f"  LONG:  {mod_long}\n"
                f"  SHORT: {mod_short}\n"
                f"{taker_note}\n"
                f"  F&G extreme values do NOT veto intraday entries — momentum can persist hours."
                f"{flows_section}"
                f"{okx_web3_section}"
            )

        else:
            # ── SWING: full regime modifiers ──────────────────────────────────
            if composite == "strongly_bullish":
                mod_long  = "+0.7 for LONG setups"
                mod_short = "-0.7 for SHORT setups"
            elif composite == "bullish":
                mod_long  = "+0.4 for LONG setups"
                mod_short = "-0.4 for SHORT setups"
            elif composite == "strongly_bearish":
                mod_long  = "-0.7 for LONG setups"
                mod_short = "+0.7 for SHORT setups"
            elif composite == "bearish":
                mod_long  = "-0.4 for LONG setups"
                mod_short = "+0.4 for SHORT setups"
            else:
                mod_long  = "no modifier"
                mod_short = "no modifier"

            # Hard warnings for extreme regimes (relevant for multi-day holds)
            extra = ""
            if regime == "distribution":
                extra = (
                    "\n⚠ EXTREME GREED (F&G>75): Distribution phase. "
                    "High reversal risk for multi-day holds. "
                    "Prefer SHORT or HOLD. Penalise new LONGs -0.5 additional."
                )
            elif regime == "accumulation":
                extra = (
                    "\n⚠ EXTREME FEAR (F&G<25): Capitulation/accumulation phase. "
                    "Bottom-formation risk. "
                    "Prefer LONG or HOLD. Penalise new SHORTs -0.5 additional."
                )

            return (
                f"\n\nMARKET REGIME DATA ({ts}) [SWING MODE]:\n"
                f"- Fear & Greed: {fng_str}  →  phase={regime}\n"
                f"- Smart Money Position Ratio: {pos_str}  ← PRIMARY whale signal (size-weighted)\n"
                f"- Top-Trader Account Ratio: {ls_str}\n"
                f"- Global L/S Ratio: {gl_str}\n"
                f"- Taker Buy/Sell Ratio: {tk_str}\n"
                f"- Recent Liquidations (BTC): {liq_str}\n"
                f"- COMPOSITE: {composite} (score={comp_sc})"
                f"{extra}"
                f"{liq_note}\n\n"
                f"REGIME SCORING MODIFIER (apply to final score before decision):\n"
                f"  LONG:  {mod_long}\n"
                f"  SHORT: {mod_short}"
                f"{flows_section}"
                f"{okx_web3_section}"
            )

    @staticmethod
    def _format_okx_web3(okx_data: Dict) -> str:
        """Format optional OKX Web3 token intelligence for the AI prompt."""
        if not okx_data or not okx_data.get("by_symbol"):
            return ""

        def money(value) -> str:
            try:
                n = float(value)
            except (TypeError, ValueError):
                return "N/A"
            if n >= 1_000_000:
                return f"${n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"${n / 1_000:.1f}K"
            return f"${n:.0f}"

        def pct(value) -> str:
            try:
                return f"{float(value):.1f}%"
            except (TypeError, ValueError):
                return "N/A"

        lines = ["\n\nOKX WEB3 TOKEN INTELLIGENCE (configured tokens only):"]
        by_symbol = okx_data.get("by_symbol", {})
        for symbol, item in sorted(by_symbol.items()):
            risk = item.get("risk") or {}
            advanced = item.get("advanced") or {}
            price = item.get("price") or {}
            clusters = item.get("clusters") or []

            risk_level = risk.get("risk_level") or advanced.get("risk_level")
            flags = []
            if risk.get("is_wash"):
                flags.append("WASH")
            if risk.get("is_rugpull"):
                flags.append("RUGPULL")
            if advanced.get("is_honeypot"):
                flags.append("HONEYPOT")
            if advanced.get("low_liquidity"):
                flags.append("LOW_LIQ")
            if advanced.get("dev_sold_all"):
                flags.append("DEV_SOLD_ALL")
            if advanced.get("smart_money_buy"):
                flags.append("SMART_MONEY_BUY")
            flag_str = ",".join(flags) if flags else "none"

            lines.append(
                f"  {symbol:<14s} risk={risk_level if risk_level is not None else 'N/A'} "
                f"flags={flag_str} liq={money(price.get('liquidity'))} "
                f"mcap={money(price.get('market_cap'))} holders={price.get('holders') or 'N/A'} "
                f"1h={pct(price.get('price_change_1h'))} vsATH={pct(price.get('price_vs_ath_pct'))} "
                f"top10={pct(advanced.get('top10_pct'))} bundle={pct(advanced.get('bundle_pct'))} "
                f"sniper={pct(advanced.get('sniper_pct'))} dev_rugs={advanced.get('dev_rug_count') if advanced.get('dev_rug_count') is not None else 'N/A'}"
            )

            if clusters:
                compact = []
                for c in clusters[:3]:
                    compact.append(
                        f"hold={pct(c.get('holding_pct'))}/trend={c.get('trend') or '?'}"
                        f"/pnl={pct(c.get('pnl_pct'))}/kol={'Y' if c.get('has_kol') else 'N'}"
                    )
                lines.append(f"    clusters: {' | '.join(compact)}")

        lines.append(
            "  INSTRUCTION: Treat OKX Web3 as risk/enrichment only, not an execution signal. "
            "Hard-avoid tokens flagged WASH, RUGPULL, HONEYPOT, or severe low-liquidity. "
            "If a scanned symbol is missing here, do not infer safety or risk."
        )
        return "\n".join(lines)

    @staticmethod
    def _format_smart_money_flows(flows: Dict) -> str:
        """
        Format multi-symbol smart money data into an AI-readable text block.

        Returns empty string if no flow data is available.
        """
        if not flows or not flows.get("by_symbol"):
            return ""

        by_sym       = flows.get("by_symbol", {})
        accumulating = flows.get("accumulating", [])
        distributing = flows.get("distributing", [])
        rot_to       = flows.get("rotation_to")
        rot_from     = flows.get("rotation_from")

        lines = ["\n\nSMART MONEY FLOW (multi-symbol, 1h Binance Futures):"]

        # ── Accumulation / Distribution ────────────────────────────────────
        if accumulating:
            acc_str = " | ".join(f"{s} ({sc:+.2f})" for s, sc in accumulating)
            lines.append(f"  ACCUMULATING (whale inflow): {acc_str}")
        if distributing:
            dis_str = " | ".join(f"{s} ({sc:+.2f})" for s, sc in distributing)
            lines.append(f"  DISTRIBUTING (whale outflow): {dis_str}")

        # ── Rotation signal ────────────────────────────────────────────────
        if rot_to and rot_from:
            lines.append(
                f"  ROTATION: {rot_from} → {rot_to}  "
                f"(whales leaving {rot_from}, entering {rot_to})"
            )
        elif rot_to:
            lines.append(
                f"  ROTATION: Capital rotating INTO {rot_to}  "
                f"(significantly outperforming BTC whale positioning)"
            )
        elif rot_from:
            lines.append(
                f"  ROTATION: Capital rotating OUT of {rot_from}  "
                f"(underperforming BTC whale positioning)"
            )

        # ── Per-symbol breakdown (compact, sorted by score) ───────────────
        scored_syms = sorted(
            by_sym.items(),
            key=lambda kv: kv[1].get("inflow_score", 0.0),
            reverse=True,
        )

        sym_lines = []
        for sym, d in scored_syms:
            pr       = d.get("pos_ratio", "?")
            pb       = d.get("pos_bias", "?")
            td       = d.get("trend_delta", 0.0)
            tdir     = d.get("trend_dir", "?")
            tb       = d.get("taker_bias", "neutral")
            sc       = d.get("inflow_score", 0.0)
            impact   = d.get("whale_impact", 0.0)
            signal   = d.get("whale_signal", "neutral")
            oi_chg   = d.get("oi_change_pct", 0.0)
            px_chg   = d.get("price_change_pct", 0.0)
            td_sign  = "+" if td >= 0 else ""
            taker_icon = "BUY" if tb == "buy_dominant" else ("SELL" if tb == "sell_dominant" else "flat")

            # Impact indicator
            if impact >= 3.0:
                impact_icon = "HIGH"
            elif impact >= 1.5:
                impact_icon = "MED"
            else:
                impact_icon = "low"

            sym_lines.append(
                f"    {sym:<14s} pos={pr} ({pb}, {tdir} {td_sign}{td:.3f})"
                f"  taker={taker_icon}  score={sc:+.2f}"
                f"  impact={impact_icon}({impact:.1f}) OI={oi_chg:+.1f}% px={px_chg:+.1f}%"
            )

        lines.append("  Symbol breakdown:")
        lines.extend(sym_lines)

        # ── High Impact Alerts ───────────────────────────────────────────
        high_impact = [
            (s, d) for s, d in scored_syms
            if d.get("whale_impact", 0) >= 2.0
        ]
        if high_impact:
            lines.append("\n  HIGH IMPACT ALERTS:")
            for sym, d in high_impact:
                signal = d.get("whale_signal", "neutral")
                direction = d.get("whale_direction", "neutral")
                impact = d.get("whale_impact", 0)
                lines.append(
                    f"    {sym}: {signal.upper()} (impact={impact:.1f}, direction={direction})"
                )

        # ── Trading instruction ────────────────────────────────────────────
        lines.append(
            "\n  INSTRUCTION: Use whale impact as SCORE MODIFIER:\n"
            "  - stealth_accumulation (strongly_bullish): LONG score +1.5\n"
            "  - whale_accumulation (bullish): LONG score +1.0\n"
            "  - distribution_top (strongly_bearish): SHORT score +1.5\n"
            "  - whale_distribution (bearish): SHORT score +1.0\n"
            "  - If whale_direction opposes your trade direction, reduce score by 0.5\n"
            "  - If no whale data for symbol, skip this factor."
        )

        return "\n".join(lines)
