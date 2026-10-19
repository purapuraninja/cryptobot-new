"""
agent/signals.py — AI signal payload helpers.

Dedup and extract the raw signal list returned by SignalEngine. No trading
side effects; pure transforms over the AI response. Extracted verbatim from
main.py.
"""

import math
from typing import Dict, List, Optional, Set

from utils.logger import logger


def dedup_signals(signals: List[Dict]) -> List[Dict]:
    """
    Remove duplicate signals for the same symbol (AI sometimes hallucinates
    repeats when input is long). Strategy:
      - Keep first occurrence of each symbol
      - But if a later occurrence has a higher score, prefer it
    """
    if not signals:
        return signals
    best_per_sym: Dict[str, Dict] = {}
    order: List[str] = []
    for s in signals:
        sym = s.get("symbol")
        if not sym:
            continue
        prev = best_per_sym.get(sym)
        if prev is None:
            best_per_sym[sym] = s
            order.append(sym)
        else:
            cur_score = float(s.get("score", 0))
            prev_score = float(prev.get("score", 0))
            if cur_score > prev_score:
                best_per_sym[sym] = s
    deduped = [best_per_sym[sym] for sym in order]
    if len(deduped) < len(signals):
        logger.warning(
            f"[Signals] Deduped {len(signals)} → {len(deduped)} "
            f"(AI returned {len(signals) - len(deduped)} duplicate symbols)"
        )
    return deduped


def _canonical_symbol(value: object) -> str:
    if not isinstance(value, str):
        return ""
    symbol = value.strip().upper().replace("/USDT:USDT", "USDT")
    symbol = symbol.replace("/USDT", "USDT").replace("/", "").replace(":", "")
    return symbol


def _finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _valid_signal(signal: object, expected: Optional[Set[str]]) -> bool:
    if not isinstance(signal, dict):
        return False
    symbol = _canonical_symbol(signal.get("symbol"))
    action = signal.get("action")
    if not symbol or action not in {"HOLD", "LONG", "SHORT"}:
        return False
    if expected is not None and symbol not in expected:
        return False
    signal["symbol"] = symbol

    if not _finite_number(signal.get("score")) or not _finite_number(signal.get("confidence")):
        return False
    score = float(signal["score"])
    confidence = float(signal["confidence"])
    if not 0.0 <= score <= 14.5 or not 0.0 <= confidence <= 1.0:
        return False
    if action == "HOLD":
        return True

    numeric_fields = ("entry_price", "stop_loss", "leverage", "position_size_percent")
    if any(not _finite_number(signal.get(field)) for field in numeric_fields):
        return False
    entry = float(signal["entry_price"])
    stop = float(signal["stop_loss"])
    leverage = float(signal["leverage"])
    size = float(signal["position_size_percent"])
    tps = signal.get("take_profit")
    if entry <= 0 or stop <= 0 or not 1 <= leverage <= 10 or size <= 0:
        return False
    if not isinstance(tps, list) or len(tps) != 3 or any(not _finite_number(tp) for tp in tps):
        return False
    tp_values = [float(tp) for tp in tps]
    if action == "LONG":
        return stop < entry and entry < tp_values[0] < tp_values[1] < tp_values[2]
    return stop > entry and entry > tp_values[0] > tp_values[1] > tp_values[2]


def extract_signals(result: Dict, expected_symbols: Optional[List[str]] = None) -> tuple:
    """Validate and normalize an AI result into (signals_list, best_candidate)."""
    if not isinstance(result, dict):
        return [], "NONE"
    expected = (
        {_canonical_symbol(symbol) for symbol in expected_symbols}
        if expected_symbols is not None else None
    )
    if "market_scan" in result:
        scan = result["market_scan"]
    elif "action" in result and expected is None:
        scan = [result]
    else:
        return [], "NONE"
    if not isinstance(scan, list) or not all(_valid_signal(signal, expected) for signal in scan):
        logger.error("[Signals] AI response rejected: invalid signal schema or symbol")
        return [], "NONE"

    deduped = dedup_signals(scan)
    actual = {signal["symbol"] for signal in deduped}
    if expected is not None and actual != expected:
        logger.error(
            f"[Signals] AI response rejected: symbol set mismatch "
            f"expected={len(expected)} actual={len(actual)}"
        )
        return [], "NONE"
    best = _canonical_symbol(result.get("best_candidate", "NONE"))
    if best not in actual or not any(
        signal["symbol"] == best and signal["action"] != "HOLD" for signal in deduped
    ):
        best = "NONE"
    return deduped, best
