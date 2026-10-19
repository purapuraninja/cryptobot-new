"""
execution/base_executor.py — Abstract base class for all exchange executors.

Required contract (enforced): execute_signal, close_position,
get_open_positions, close.

Extended contract (optional, probed with hasattr by callers): partial_close,
update_sl_roi, execute_dca, get_closed_pnl, get_balance, get_last_price.
A CEX executor that omits any of these silently loses the feature that
depends on it — see main.py / agent/positions.py for the probe sites.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from utils.logger import logger


class BaseExecutor(ABC):

    # ── Shared SL math ────────────────────────────────────────────
    # Lives on the base class so every CEX computes the SAME stop from the
    # same inputs. risk/decision_engine.py materialises this stop pre-trade
    # for RR and max-loss sizing; if an executor computed its own variant,
    # the tracker would police a level the exchange never had.

    @staticmethod
    def _calc_roi_sl(action: str, entry: float, leverage: float,
                     price_dp: int = 6,
                     max_price_move_pct: float = 15.0,
                     atr_pct: float = 0.0,
                     mode: str = "swing") -> float:
        """
        Calculate Hybrid SL price (ATR floor + ROI cap), capped at
        max_price_move_pct.
        """
        buffer = 0.995
        max_f = max_price_move_pct / 100.0

        # Default ROI target (-75%)
        sl_dist_pct = 0.75 / leverage

        # Hybrid Collar Logic
        if atr_pct > 0:
            if mode == "intraday":
                atr_floor_pct = (atr_pct / 100.0) * 1.5
                roi_cap_pct = 0.50 / leverage
            else:
                atr_floor_pct = (atr_pct / 100.0) * 2.0
                roi_cap_pct = 0.75 / leverage

            if atr_floor_pct > roi_cap_pct:
                logger.warning(
                    f"[EXEC] Leverage {leverage}x too high for ATR {atr_pct}%. "
                    f"ATR floor ({atr_floor_pct*100:.1f}%) > ROI cap "
                    f"({roi_cap_pct*100:.1f}%). "
                    f"Trade might be stopped out by noise."
                )

            sl_dist_pct = min(max(atr_floor_pct, sl_dist_pct), roi_cap_pct)

        if action == "LONG":
            sl_roi = entry * (1.0 - sl_dist_pct) * buffer
            sl_cap = entry * (1.0 - max_f)
            sl = max(sl_roi, sl_cap)   # whichever is CLOSER to entry (less loss)
        else:
            sl_roi = entry * (1.0 + sl_dist_pct) / buffer
            sl_cap = entry * (1.0 + max_f)
            sl = min(sl_roi, sl_cap)   # whichever is CLOSER to entry (less loss)

        return round(sl, price_dp)

    # ── Precision helpers ─────────────────────────────────────────

    @staticmethod
    def _qty_dp(step: float) -> int:
        import math
        if step <= 0:
            return 3
        return max(0, math.ceil(-math.log10(step)))

    @staticmethod
    def _price_dp(tick: float) -> int:
        import math
        if tick <= 0:
            return 2
        return max(0, math.ceil(-math.log10(tick)))

    @abstractmethod
    async def execute_signal(self, signal: Dict) -> Optional[Dict]:
        """
        Execute a LONG or SHORT signal on the exchange.

        Args:
            signal: Parsed signal dict from SignalEngine.

        Returns:
            Execution result dict, or None on failure / HOLD.
        """

    @abstractmethod
    async def close_position(self, symbol: str) -> Optional[Dict]:
        """
        Market-close an open position for the given symbol.

        Args:
            symbol: Exchange-native symbol string.

        Returns:
            Result dict, or None on failure / no position found.
        """

    @abstractmethod
    async def get_open_positions(self) -> List[Dict]:
        """
        Return list of currently open positions.
        In DRY_RUN mode, returns an empty list.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release resources (close HTTP sessions, exchange connections, etc.)"""
