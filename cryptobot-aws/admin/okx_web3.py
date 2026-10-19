"""OKX Web3 diagnostics for the operator CLI."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import Config
from data.okx_web3_feed import OkxWeb3Feed


def inspect_okx_web3(symbols: Optional[List[str]] = None, fetch: bool = True) -> Dict[str, Any]:
    """Return sanitized OKX Web3 config and optional enrichment result."""
    feed = OkxWeb3Feed.from_config(Config)
    requested = symbols or Config.SYMBOLS[:feed.max_symbols]
    result: Dict[str, Any] = {
        "enabled": Config.OKX_WEB3_ENABLED,
        "auth": feed.has_auth,
        "base_url": feed.base_url,
        "default_chain": feed.default_chain,
        "max_symbols": feed.max_symbols,
        "cache_ttl": feed.cache_ttl,
        "auto_resolve": feed.auto_resolve,
        "resolve_min_liquidity_usd": feed.resolve_min_liquidity_usd,
        "min_interval_ms": feed.min_interval_ms,
        "max_429_retries": feed.max_429_retries,
        "base_429_delay_ms": feed.base_429_delay_ms,
        "mapped_symbols": feed.mapped_symbols(),
        "requested_symbols": requested,
        "fetched": None,
    }
    if fetch:
        result["fetched"] = feed.fetch_for_symbols(requested)
    return result


def summarize_symbol(item: Dict[str, Any]) -> str:
    risk = item.get("risk") or {}
    advanced = item.get("advanced") or {}
    price = item.get("price") or {}
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
    if advanced.get("smart_money_buy"):
        flags.append("SMART_MONEY_BUY")
    flag_text = ",".join(flags) if flags else "none"
    return (
        f"risk={risk_level if risk_level is not None else 'N/A'} "
        f"flags={flag_text} holders={price.get('holders') or 'N/A'} "
        f"liq={price.get('liquidity') if price.get('liquidity') is not None else 'N/A'} "
        f"mcap={price.get('market_cap') if price.get('market_cap') is not None else 'N/A'}"
    )
