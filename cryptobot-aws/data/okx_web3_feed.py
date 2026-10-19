"""
data/okx_web3_feed.py - optional OKX Web3 token intelligence.

This module is enrichment-only. It never places orders and it deliberately
requires an explicit SYMBOL -> chain/token mapping because CEX perp symbols do
not contain enough information to infer a Web3 token contract safely.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import certifi
import requests

from utils.logger import logger


OKX_WEB3_BASE = "https://web3.okx.com"
JUPITER_DATA_BASE = "https://datapi.jup.ag/v1"
OKX_PUBLIC_HEADERS = {"Ok-Access-Client-type": "agent-cli"}
EXPECTED_UNAVAILABLE = (
    "no data",
    "not found",
    "token not found",
    "does not exist",
    "not exist",
    "no record",
    "unsupported token",
    "not support",
)


class OkxWeb3Error(Exception):
    """Raised for OKX Web3 request failures."""

    def __init__(self, message: str, status: Optional[int] = None, code: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _pct(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_item(data: Any) -> Any:
    if isinstance(data, list):
        return data[0] if data else None
    return data


def _normalize_symbol(symbol: str) -> str:
    return (
        symbol.replace("/USDT:USDT", "USDT")
        .replace("/", "")
        .replace(":", "")
        .upper()
        .strip()
    )


def parse_token_map(raw: str, default_chain: str = "501") -> Dict[str, Tuple[str, str]]:
    """
    Parse OKX_WEB3_TOKEN_MAP.

    Format:
      WIFUSDT=501:EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLFnJj6SApump,
      PEPEUSDT=1:0x6982508145454ce325ddbe47a25d4ec3d2311933

    If chain is omitted, OKX_WEB3_DEFAULT_CHAIN is used.
    """
    mapping: Dict[str, Tuple[str, str]] = {}
    for part in (raw or "").split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        symbol, target = item.split("=", 1)
        symbol = _normalize_symbol(symbol)
        target = target.strip()
        if not symbol or not target:
            continue
        if ":" in target:
            chain, token = target.split(":", 1)
            chain = chain.strip() or default_chain
            token = token.strip()
        else:
            chain = default_chain
            token = target
        if token:
            mapping[symbol] = (str(chain), token)
    return mapping


class OkxWeb3Feed:
    """Small synchronous OKX Web3 client used by OnChainFeed."""

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        project_id: str = "",
        token_map: Optional[Dict[str, Tuple[str, str]]] = None,
        base_url: str = OKX_WEB3_BASE,
        timeout: float = 10.0,
        cache_ttl: int = 300,
        max_symbols: int = 5,
        default_chain: str = "501",
        auto_resolve: bool = False,
        resolve_min_liquidity_usd: float = 100000.0,
        min_interval_ms: int = 250,
        max_429_retries: int = 2,
        base_429_delay_ms: int = 1000,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.project_id = project_id
        self.token_map = token_map or {}
        self.base_url = (base_url or OKX_WEB3_BASE).rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_symbols = max(1, max_symbols)
        self.default_chain = str(default_chain or "501")
        self.auto_resolve = auto_resolve
        self.resolve_min_liquidity_usd = max(0.0, float(resolve_min_liquidity_usd or 0.0))
        self.min_interval_ms = max(0, int(min_interval_ms or 0))
        self.max_429_retries = max(0, int(max_429_retries or 0))
        self.base_429_delay_ms = max(250, int(base_429_delay_ms or 1000))
        self._last_request_started_at = 0.0
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._resolve_cache: Dict[str, Tuple[float, Optional[Tuple[str, str, Dict[str, Any]]]]] = {}
        self._session = requests.Session()
        self._session.verify = certifi.where()

    @classmethod
    def from_config(cls, config) -> "OkxWeb3Feed":
        return cls(
            api_key=getattr(config, "OKX_API_KEY", ""),
            secret_key=getattr(config, "OKX_SECRET_KEY", ""),
            passphrase=getattr(config, "OKX_PASSPHRASE", ""),
            project_id=getattr(config, "OKX_PROJECT_ID", ""),
            token_map=parse_token_map(
                getattr(config, "OKX_WEB3_TOKEN_MAP", ""),
                default_chain=getattr(config, "OKX_WEB3_DEFAULT_CHAIN", "501"),
            ),
            base_url=getattr(config, "OKX_WEB3_BASE_URL", OKX_WEB3_BASE),
            timeout=float(getattr(config, "OKX_WEB3_TIMEOUT_SECONDS", 10.0)),
            cache_ttl=int(getattr(config, "OKX_WEB3_CACHE_TTL_SECONDS", 300)),
            max_symbols=int(getattr(config, "OKX_WEB3_MAX_SYMBOLS", 5)),
            default_chain=getattr(config, "OKX_WEB3_DEFAULT_CHAIN", "501"),
            auto_resolve=bool(getattr(config, "OKX_WEB3_AUTO_RESOLVE", False)),
            resolve_min_liquidity_usd=float(getattr(config, "OKX_WEB3_RESOLVE_MIN_LIQUIDITY_USD", 100000.0)),
            min_interval_ms=int(getattr(config, "OKX_WEB3_MIN_INTERVAL_MS", 250)),
            max_429_retries=int(getattr(config, "OKX_WEB3_429_MAX_RETRIES", 2)),
            base_429_delay_ms=int(getattr(config, "OKX_WEB3_429_BASE_DELAY_MS", 1000)),
        )

    @property
    def has_auth(self) -> bool:
        return bool(self.api_key and self.secret_key and self.passphrase)

    def mapped_symbols(self) -> List[str]:
        return sorted(self.token_map.keys())

    @staticmethod
    def _base_ticker(symbol: str) -> str:
        norm = _normalize_symbol(symbol)
        for suffix in ("USDT", "USDC", "USD"):
            if norm.endswith(suffix):
                return norm[:-len(suffix)]
        return norm

    def _resolve_symbol(self, symbol: str) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        """Resolve an unmapped symbol to a Solana mint via Jupiter asset search."""
        if not self.auto_resolve or self.default_chain != "501":
            return None

        norm = _normalize_symbol(symbol)
        cached = self._resolve_cache.get(norm)
        now = time.time()
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        ticker = self._base_ticker(norm)
        if not ticker:
            self._resolve_cache[norm] = (now, None)
            return None

        try:
            response = self._session.get(
                f"{JUPITER_DATA_BASE}/assets/search",
                params={"query": ticker},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            candidates = payload if isinstance(payload, list) else [payload]
        except Exception as exc:
            logger.debug(f"[OKXWeb3] auto-resolve {norm} failed: {exc}")
            self._resolve_cache[norm] = (now, None)
            return None

        exact = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if str(item.get("symbol", "")).strip().upper() != ticker:
                continue
            mint = item.get("id")
            if not mint:
                continue
            liquidity = _pct(item.get("liquidity")) or 0.0
            if liquidity < self.resolve_min_liquidity_usd:
                continue
            exact.append(item)

        if not exact:
            self._resolve_cache[norm] = (now, None)
            return None

        exact.sort(
            key=lambda item: (
                _pct(item.get("liquidity")) or 0.0,
                _pct(item.get("mcap")) or 0.0,
                _pct(item.get("usdPrice")) or 0.0,
            ),
            reverse=True,
        )
        best = exact[0]
        resolved = (
            self.default_chain,
            str(best["id"]),
            {
                "resolver": "jupiter-assets-search",
                "query": ticker,
                "name": best.get("name"),
                "symbol": best.get("symbol"),
                "liquidity": _pct(best.get("liquidity")),
                "market_cap": _pct(best.get("mcap")),
                "holders": _int(best.get("holderCount")),
            },
        )
        self._resolve_cache[norm] = (now, resolved)
        logger.info(
            f"[OKXWeb3] auto-resolved {norm} -> {resolved[1]} "
            f"({resolved[2].get('name') or ticker}, liq={resolved[2].get('liquidity')})"
        )
        return resolved

    def _auth_headers(self, method: str, path: str, body_text: str = "") -> Dict[str, str]:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        prehash = f"{timestamp}{method.upper()}{path}{body_text}"
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": base64.b64encode(digest).decode("ascii"),
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "OK-ACCESS-TIMESTAMP": timestamp,
        }
        if self.project_id:
            headers["OK-ACCESS-PROJECT"] = self.project_id
        return headers

    def _headers(self, method: str, path: str, body_text: str = "") -> Dict[str, str]:
        if self.has_auth:
            return self._auth_headers(method, path, body_text)
        return dict(OKX_PUBLIC_HEADERS)

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        method = method.upper()
        body_text = "" if body is None else json.dumps(body, separators=(",", ":"))
        headers = self._headers(method, path, body_text)
        if body is not None:
            headers["Content-Type"] = "application/json"

        wait_s = max(0.0, (self._last_request_started_at + self.min_interval_ms / 1000.0) - time.time())
        if wait_s > 0:
            time.sleep(wait_s)

        for attempt in range(self.max_429_retries + 1):
            self._last_request_started_at = time.time()
            response = self._session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                data=body_text if body is not None else None,
                timeout=self.timeout,
            )
            if response.status_code != 429 or attempt >= self.max_429_retries:
                break
            retry_after = response.headers.get("retry-after")
            try:
                delay_s = float(retry_after) if retry_after else 0.0
            except (TypeError, ValueError):
                delay_s = 0.0
            if delay_s <= 0:
                delay_s = (self.base_429_delay_ms / 1000.0) * (2 ** attempt)
            logger.warning(
                f"[OKXWeb3] rate limited (429), retry {attempt + 1}/"
                f"{self.max_429_retries} after {delay_s:.1f}s"
            )
            time.sleep(delay_s)

        try:
            payload = response.json()
        except ValueError as exc:
            raise OkxWeb3Error(f"OKX Web3 returned invalid JSON ({path})", response.status_code) from exc

        if not isinstance(payload, dict):
            raise OkxWeb3Error(f"OKX Web3 returned unexpected payload ({path})", response.status_code)

        if not response.ok:
            reason = payload.get("msg") or payload.get("message") or payload.get("error") or response.text[:160]
            raise OkxWeb3Error(f"OKX Web3 HTTP {response.status_code}: {reason}", response.status_code, payload.get("code"))

        code = payload.get("code")
        if code not in (None, 0, "0"):
            reason = payload.get("msg") or payload.get("message") or "unknown"
            raise OkxWeb3Error(f"OKX Web3 error {code}: {reason}", response.status_code, code)

        return payload.get("data")

    @staticmethod
    def _is_expected_unavailable(exc: Exception) -> bool:
        text = str(exc).lower()
        status = getattr(exc, "status", None)
        return status == 404 or any(part in text for part in EXPECTED_UNAVAILABLE)

    def _get(self, endpoint: str, params: Dict[str, Any]) -> Any:
        return self._request("GET", f"{endpoint}?{urlencode(params)}")

    def _post(self, endpoint: str, body: Any) -> Any:
        return self._request("POST", endpoint, body=body)

    def get_risk_flags(self, token_address: str, chain_id: str) -> Optional[Dict[str, Any]]:
        params = {
            "chainId": chain_id,
            "tokenContractAddress": token_address,
            "t": int(time.time() * 1000),
        }
        data = _first_item(self._get("/priapi/v1/dx/market/v2/risk/new/check", params))
        if not isinstance(data, dict):
            return None

        def collect(section: Any) -> List[Dict[str, Any]]:
            if not isinstance(section, dict):
                return []
            entries: List[Dict[str, Any]] = []
            for key in ("highRiskList", "middleRiskList", "lowRiskList"):
                values = section.get(key)
                if isinstance(values, list):
                    entries.extend(v for v in values if isinstance(v, dict))
            return entries

        entries: List[Dict[str, Any]] = []
        for section in ("allAnalysis", "swapAnalysis", "contractAnalysis", "extraAnalysis"):
            entries.extend(collect(data.get(section)))

        def has_risk(risk_key: str) -> bool:
            for entry in entries:
                label = str(entry.get("newRiskLabel", "")).strip().lower()
                if entry.get("riskKey") == risk_key and label == "yes":
                    return True
            return False

        return {
            "is_rugpull": has_risk("isLiquidityRemoval"),
            "is_wash": has_risk("isWash"),
            "risk_level": _int(data.get("riskLevel") or data.get("riskControlLevel")),
            "source": "okx-risk-check",
        }

    def get_advanced_info(self, token_address: str, chain_index: str) -> Optional[Dict[str, Any]]:
        data = _first_item(self._get(
            "/api/v6/dex/market/token/advanced-info",
            {"chainIndex": chain_index, "tokenContractAddress": token_address},
        ))
        if not isinstance(data, dict):
            return None

        tags = data.get("tokenTags") if isinstance(data.get("tokenTags"), list) else []
        return {
            "risk_level": _int(data.get("riskControlLevel")),
            "bundle_pct": _pct(data.get("bundleHoldingPercent")),
            "sniper_pct": _pct(data.get("sniperHoldingPercent")),
            "suspicious_pct": _pct(data.get("suspiciousHoldingPercent")),
            "dev_holding_pct": _pct(data.get("devHoldingPercent")),
            "top10_pct": _pct(data.get("top10HoldPercent")),
            "lp_burned_pct": _pct(data.get("lpBurnedPercent")),
            "total_fee_sol": _pct(data.get("totalFee")),
            "dev_rug_count": _int(data.get("devRugPullTokenCount")),
            "dev_token_count": _int(data.get("devCreateTokenCount")),
            "creator": data.get("creatorAddress") or None,
            "tags": tags,
            "is_honeypot": "honeypot" in tags,
            "smart_money_buy": "smartMoneyBuy" in tags,
            "dev_sold_all": "devHoldingStatusSellAll" in tags,
            "dev_buying_more": "devHoldingStatusBuy" in tags,
            "low_liquidity": "lowLiquidity" in tags,
            "dex_boost": "dexBoost" in tags,
            "dex_screener_paid": "dexScreenerPaid" in tags or "dsPaid" in tags,
        }

    def get_cluster_list(self, token_address: str, chain_index: str, limit: int = 5) -> List[Dict[str, Any]]:
        data = self._get(
            "/api/v6/dex/market/token/cluster/list",
            {"chainIndex": chain_index, "tokenContractAddress": token_address},
        )
        if isinstance(data, dict):
            raw = data.get("clusterList") or data.get("clustList") or []
        else:
            d0 = _first_item(data)
            raw = d0.get("clustList", []) if isinstance(d0, dict) else []
        if not isinstance(raw, list):
            return []

        clusters = []
        for cluster in raw[:limit]:
            if not isinstance(cluster, dict):
                continue
            addresses = cluster.get("clusterAddressList") or []
            has_kol = any(isinstance(a, dict) and a.get("isKol") for a in addresses)
            trend_type = cluster.get("trendType")
            if isinstance(trend_type, dict):
                trend_type = trend_type.get("trendType")
            hold_seconds = _pct(cluster.get("averageHoldingPeriod"))
            clusters.append({
                "holding_pct": _pct(cluster.get("holdingPercent")),
                "trend": trend_type,
                "avg_hold_days": round(hold_seconds / 86400) if hold_seconds is not None else None,
                "pnl_pct": _pct(cluster.get("pnlPercent")),
                "buy_vol_usd": _pct(cluster.get("buyVolume")),
                "sell_vol_usd": _pct(cluster.get("sellVolume")),
                "avg_buy_price": _pct(cluster.get("averageBuyPriceUsd")),
                "has_kol": has_kol,
                "address_count": len(addresses) if isinstance(addresses, list) else 0,
            })
        return clusters

    def get_price_info(self, token_address: str, chain_index: str) -> Optional[Dict[str, Any]]:
        data = _first_item(self._post(
            "/api/v6/dex/market/price-info",
            [{"chainIndex": chain_index, "tokenContractAddress": token_address}],
        ))
        if not isinstance(data, dict):
            return None

        price = _pct(data.get("price")) or 0.0
        max_price = _pct(data.get("maxPrice")) or 0.0
        return {
            "price": price,
            "ath": max_price,
            "atl": _pct(data.get("minPrice")),
            "price_vs_ath_pct": round(price / max_price * 100, 1) if max_price > 0 else None,
            "price_change_5m": _pct(data.get("priceChange5M")),
            "price_change_1h": _pct(data.get("priceChange1H")),
            "volume_5m": _pct(data.get("volume5M")),
            "volume_1h": _pct(data.get("volume1H")),
            "holders": _int(data.get("holders")),
            "market_cap": _pct(data.get("marketCap")),
            "liquidity": _pct(data.get("liquidity")),
        }

    def get_full_token_analysis(self, token_address: str, chain_index: str) -> Dict[str, Any]:
        cache_key = f"{chain_index}:{token_address}"
        cached = self._cache.get(cache_key)
        now = time.time()
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        result: Dict[str, Any] = {
            "chain_index": chain_index,
            "token_address": token_address,
            "risk": None,
            "advanced": None,
            "clusters": [],
            "price": None,
            "errors": [],
        }

        calls = (
            ("risk", lambda: self.get_risk_flags(token_address, chain_index)),
            ("advanced", lambda: self.get_advanced_info(token_address, chain_index)),
            ("clusters", lambda: self.get_cluster_list(token_address, chain_index)),
            ("price", lambda: self.get_price_info(token_address, chain_index)),
        )
        for name, fn in calls:
            try:
                result[name] = fn()
            except Exception as exc:
                if not self._is_expected_unavailable(exc):
                    result["errors"].append(f"{name}: {exc}")

        self._cache[cache_key] = (now, result)
        return result

    def fetch_for_symbols(self, symbols: List[str]) -> Dict[str, Any]:
        target = []
        resolved_meta: Dict[str, Dict[str, Any]] = {}
        seen = set()
        for symbol in symbols:
            norm = _normalize_symbol(symbol)
            if norm in seen:
                continue
            if norm not in self.token_map:
                resolved = self._resolve_symbol(norm)
                if resolved is None:
                    continue
                chain, token, meta = resolved
                self.token_map[norm] = (chain, token)
                resolved_meta[norm] = meta
            seen.add(norm)
            target.append(norm)
            if len(target) >= self.max_symbols:
                break

        by_symbol: Dict[str, Dict[str, Any]] = {}
        errors: Dict[str, List[str]] = {}
        for symbol in target:
            chain, token = self.token_map[symbol]
            try:
                data = self.get_full_token_analysis(token, chain)
                if symbol in resolved_meta:
                    data["resolved"] = resolved_meta[symbol]
                by_symbol[symbol] = data
                if data.get("errors"):
                    errors[symbol] = data["errors"]
            except Exception as exc:
                logger.debug(f"[OKXWeb3] {symbol} enrichment failed: {exc}")
                errors[symbol] = [str(exc)]

        return {
            "enabled": True,
            "auth": self.has_auth,
            "auto_resolve": self.auto_resolve,
            "mapped_count": len(self.token_map),
            "auto_resolved": resolved_meta,
            "requested_count": len(target),
            "available_count": len(by_symbol),
            "by_symbol": by_symbol,
            "errors": errors,
            "source": "okx-web3",
        }
