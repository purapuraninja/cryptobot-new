"""
mcp_server.py — MCP Server for Trading Bot
Unified interface for Crypto Exchange, Database, Telegram, and Analytics.

Usage:
  # Stdio mode (for Cursor, OpenAI Codex CLI)
  python mcp_server.py

  # SSE mode (for web clients)
  python mcp_server.py --transport sse --port 8080
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import ccxt
from mcp import types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
import uvicorn

from config import Config

# ── Database Setup ────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "data" / "trading_bot.db"


def init_database():
    """Initialize SQLite database with required tables (MCP analytics + unified storage)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Ensure unified storage tables exist in the same DB
    try:
        from storage.schema import apply_schema
        apply_schema(conn)
    except Exception:
        pass

    # Trade history table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            quantity REAL,
            leverage INTEGER,
            pnl REAL,
            pnl_pct REAL,
            entry_time TIMESTAMP,
            exit_time TIMESTAMP,
            exit_reason TEXT,
            ai_score REAL,
            ai_confidence REAL,
            setup_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Performance snapshots
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS performance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_pnl REAL,
            win_rate REAL,
            total_trades INTEGER,
            winning_trades INTEGER,
            losing_trades INTEGER,
            best_trade REAL,
            worst_trade REAL,
            avg_win REAL,
            avg_loss REAL,
            profit_factor REAL,
            max_drawdown REAL,
            balance REAL
        )
    """)

    # Whale data cache
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whale_data_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            position_ratio REAL,
            account_ratio REAL,
            taker_ratio REAL,
            fear_greed_index INTEGER,
            oi_change REAL,
            whale_impact_score REAL,
            whale_signal TEXT
        )
    """)

    # Signal history
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            symbol TEXT NOT NULL,
            signal_type TEXT,
            score REAL,
            confidence REAL,
            entry_price REAL,
            sl_price REAL,
            tp1_price REAL,
            tp2_price REAL,
            tp3_price REAL,
            reason TEXT,
            executed BOOLEAN DEFAULT FALSE
        )
    """)

    conn.commit()
    conn.close()


# ── Exchange Manager ──────────────────────────────────────────────
class ExchangeManager:
    """Unified exchange interface using ccxt."""

    def __init__(self):
        self.exchanges = {}
        self._init_exchanges()

    def _init_exchanges(self):
        """Initialize configured exchanges."""
        # Bybit CEX
        if Config.BYBIT_API_KEY and Config.BYBIT_SECRET:
            self.exchanges["bybit"] = ccxt.bybit({
                "apiKey": Config.BYBIT_API_KEY,
                "secret": Config.BYBIT_SECRET,
                "sandbox": Config.BYBIT_TESTNET,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })

        # Binance (public data only)
        self.exchanges["binance"] = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })

    async def get_ticker(self, exchange: str, symbol: str) -> Dict:
        """Get current ticker data."""
        ex = self.exchanges.get(exchange)
        if not ex:
            return {"error": f"Exchange {exchange} not configured"}
        try:
            ticker = await asyncio.to_thread(ex.fetch_ticker, symbol)
            return {
                "symbol": ticker["symbol"],
                "last": ticker["last"],
                "bid": ticker["bid"],
                "ask": ticker["ask"],
                "high": ticker["high"],
                "low": ticker["low"],
                "volume": ticker["baseVolume"],
                "change": ticker["percentage"],
                "timestamp": ticker["timestamp"],
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_ohlcv(
        self, exchange: str, symbol: str, timeframe: str = "1h", limit: int = 100
    ) -> List:
        """Get OHLCV candle data."""
        ex = self.exchanges.get(exchange)
        if not ex:
            return []
        try:
            candles = await asyncio.to_thread(
                ex.fetch_ohlcv, symbol, timeframe, limit=limit
            )
            return [
                {
                    "timestamp": c[0],
                    "open": c[1],
                    "high": c[2],
                    "low": c[3],
                    "close": c[4],
                    "volume": c[5],
                }
                for c in candles
            ]
        except Exception as e:
            return []

    async def get_positions(self, exchange: str) -> List[Dict]:
        """Get open positions."""
        ex = self.exchanges.get(exchange)
        if not ex:
            return []
        try:
            positions = await asyncio.to_thread(ex.fetch_positions)
            return [
                {
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "size": p["contracts"],
                    "entry_price": p["entryPrice"],
                    "mark_price": p["markPrice"],
                    "unrealized_pnl": p["unrealizedPnl"],
                    "leverage": p["leverage"],
                    "liquidation_price": p["liquidationPrice"],
                    "margin_mode": p["marginMode"],
                }
                for p in positions
                if p["contracts"] and p["contracts"] > 0
            ]
        except Exception as e:
            return []

    async def get_balance(self, exchange: str) -> Dict:
        """Get account balance."""
        ex = self.exchanges.get(exchange)
        if not ex:
            return {}
        try:
            balance = await asyncio.to_thread(ex.fetch_balance)
            return {
                "total": balance["total"].get("USDT", 0),
                "free": balance["free"].get("USDT", 0),
                "used": balance["used"].get("USDT", 0),
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_funding_rate(self, exchange: str, symbol: str) -> Dict:
        """Get current funding rate."""
        ex = self.exchanges.get(exchange)
        if not ex:
            return {}
        try:
            funding = await asyncio.to_thread(ex.fetch_funding_rate, symbol)
            return {
                "symbol": funding["symbol"],
                "rate": funding["fundingRate"],
                "next_time": funding["fundingDatetime"],
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_orderbook(
        self, exchange: str, symbol: str, limit: int = 20
    ) -> Dict:
        """Get order book."""
        ex = self.exchanges.get(exchange)
        if not ex:
            return {}
        try:
            book = await asyncio.to_thread(ex.fetch_order_book, symbol, limit)
            return {
                "bids": book["bids"][:limit],
                "asks": book["asks"][:limit],
                "spread": (
                    (book["asks"][0][0] - book["bids"][0][0]) / book["bids"][0][0]
                    if book["bids"] and book["asks"]
                    else 0
                ),
            }
        except Exception as e:
            return {"error": str(e)}


# ── Database Manager ──────────────────────────────────────────────
class DatabaseManager:
    """SQLite database operations."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def get_trade_history(
        self, symbol: str = None, days: int = 30, limit: int = 100
    ) -> List[Dict]:
        """Get trade history from the closed_trades read model (unified storage).

        Column aliases match the legacy schema expected by callers:
        action→side, notional→quantity, open_time→entry_time,
        close_time→exit_time, reason→exit_reason.
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        query = """
            SELECT
                symbol,
                action AS side,
                entry_price,
                exit_price,
                notional AS quantity,
                leverage,
                pnl_usdt AS pnl,
                pnl_pct,
                open_time AS entry_time,
                close_time AS exit_time,
                reason AS exit_reason,
                ai_score,
                ai_confidence
            FROM closed_trades
            WHERE close_time >= ?
        """
        params = [(datetime.now() - timedelta(days=days)).isoformat()]

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        query += " ORDER BY close_time DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    async def insert_trade(self, trade: Dict) -> int:
        """DEPRECATED: the closed_trades read model is rebuilt automatically
        from the canonical pnl_store document on every save. Manual inserts
        into the old trades table are a divergence risk. Kept for backward
        compatibility only — should not be called."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO trades (symbol, side, entry_price, exit_price, quantity,
                              leverage, pnl, pnl_pct, entry_time, exit_time,
                              exit_reason, ai_score, ai_confidence, setup_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                trade.get("symbol"),
                trade.get("side"),
                trade.get("entry_price"),
                trade.get("exit_price"),
                trade.get("quantity"),
                trade.get("leverage"),
                trade.get("pnl"),
                trade.get("pnl_pct"),
                trade.get("entry_time"),
                trade.get("exit_time"),
                trade.get("exit_reason"),
                trade.get("ai_score"),
                trade.get("ai_confidence"),
                trade.get("setup_type"),
            ),
        )

        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id

    async def get_performance_stats(self, days: int = 30) -> Dict:
        """Calculate canonical position-level performance statistics."""
        from utils.trade_analytics import (
            aggregate_position_trades,
            calculate_position_metrics,
        )

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 0))
        positions = aggregate_position_trades(
            _load_pnl_store().get("closed_trades", [])
        )
        recent_positions = []
        for trade in positions:
            try:
                closed_at = datetime.strptime(
                    trade.get("close_time", ""), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if closed_at >= cutoff:
                recent_positions.append(trade)

        metrics = calculate_position_metrics(recent_positions)
        total = metrics["completed_trades"]
        best = metrics.get("best_lifecycle") or {}
        worst = metrics.get("worst_lifecycle") or {}
        return {
            "total_trades": total,
            "winning_trades": metrics["wins"],
            "losing_trades": metrics["losses"],
            "flat_trades": metrics["flats"],
            "win_rate": round(metrics["wins"] / total * 100, 2) if total else 0,
            "total_pnl": round(metrics["gross_pnl_usdt"], 2),
            "avg_win": round(metrics["avg_win_usdt"], 2),
            "avg_loss": round(metrics["avg_loss_usdt"], 2),
            "best_trade": round(best.get("position_pnl_usdt", 0), 2),
            "worst_trade": round(worst.get("position_pnl_usdt", 0), 2),
            "profit_factor": metrics["profit_factor"],
            "expectancy": round(metrics["expectancy_usdt"], 2),
            "max_drawdown": round(metrics["max_drawdown_usdt"], 2),
        }

    async def save_whale_data(self, data: Dict) -> None:
        """Cache whale data to database."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO whale_data_cache (symbol, position_ratio, account_ratio,
                                         taker_ratio, fear_greed_index, oi_change,
                                         whale_impact_score, whale_signal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data.get("symbol"),
                data.get("position_ratio"),
                data.get("account_ratio"),
                data.get("taker_ratio"),
                data.get("fear_greed_index"),
                data.get("oi_change"),
                data.get("whale_impact_score"),
                data.get("whale_signal"),
            ),
        )

        conn.commit()
        conn.close()

    async def get_whale_history(self, symbol: str, hours: int = 24) -> List[Dict]:
        """Get whale data history."""
        conn = self._get_conn()
        cursor = conn.cursor()

        since = (datetime.now() - timedelta(hours=hours)).isoformat()

        cursor.execute(
            """
            SELECT * FROM whale_data_cache
            WHERE symbol = ? AND timestamp >= ?
            ORDER BY timestamp DESC
        """,
            (symbol, since),
        )

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    async def save_signal(self, signal: Dict) -> int:
        """Save signal to history."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO signal_history (symbol, signal_type, score, confidence,
                                       entry_price, sl_price, tp1_price, tp2_price,
                                       tp3_price, reason, executed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                signal.get("symbol"),
                signal.get("signal_type"),
                signal.get("score"),
                signal.get("confidence"),
                signal.get("entry_price"),
                signal.get("sl_price"),
                signal.get("tp1_price"),
                signal.get("tp2_price"),
                signal.get("tp3_price"),
                signal.get("reason"),
                signal.get("executed", False),
            ),
        )

        signal_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return signal_id

    async def get_signal_history(
        self, symbol: str = None, limit: int = 50
    ) -> List[Dict]:
        """Get signal history."""
        conn = self._get_conn()
        cursor = conn.cursor()

        query = "SELECT * FROM signal_history"
        params = []

        if symbol:
            query += " WHERE symbol = ?"
            params.append(symbol)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]


# ── MCP Server ────────────────────────────────────────────────────
app = Server("trading-bot-mcp")

# Initialize components
exchange_manager = ExchangeManager()
db_manager = DatabaseManager(str(DB_PATH))


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json_file(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _parse_trade_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).split(".")[0], fmt)
        except ValueError:
            continue
    return None


def _load_pnl_store() -> Dict[str, Any]:
    """Load pnl_store via active storage backend (json or sqlite)."""
    try:
        from config import Config
        if Config.STORAGE_BACKEND == "sqlite":
            from storage import sqlite_backend
            return sqlite_backend.load_store()
    except Exception:
        pass
    return _read_json_file(Path(__file__).parent / "pnl_store.json", {})


def _load_trade_memory() -> Dict[str, Any]:
    """Load trade_memory via active storage backend (json or sqlite)."""
    try:
        from config import Config
        if Config.STORAGE_BACKEND == "sqlite":
            from storage import sqlite_backend
            return sqlite_backend.load_memory()
    except Exception:
        pass
    return _read_json_file(
        Path(__file__).parent / "trade_memory.json",
        {
            "trades": [],
            "symbol_stats": {},
            "setup_stats": {},
            "daily_pnl": {},
            "streak": {"current": 0, "type": "none"},
            "version": 2,
        },
    )


def _save_trade_memory(memory: Dict[str, Any]) -> None:
    """Save trade_memory via active storage backend (json or sqlite)."""
    try:
        from config import Config
        if Config.STORAGE_BACKEND == "sqlite":
            from storage import sqlite_backend
            sqlite_backend.save_memory(memory)
            return
    except Exception:
        pass
    _write_json_file(Path(__file__).parent / "trade_memory.json", memory)


def _summarize_bad_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    pnl_usdt = float(trade.get("pnl_usdt", 0) or 0)
    pnl_pct = float(trade.get("pnl_pct", 0) or 0)
    ai_score = float(trade.get("ai_score", 0) or 0)
    confidence = float(trade.get("ai_confidence", 0) or 0)
    leverage = int(float(trade.get("leverage", 0) or 0))
    reasons = []

    if pnl_usdt < 0:
        reasons.append("losing_trade")
    if pnl_pct <= -20:
        reasons.append("large_pct_loss")
    if ai_score >= 8:
        reasons.append("high_score_loss")
    if confidence >= 0.75:
        reasons.append("high_confidence_loss")
    if leverage >= 20:
        reasons.append("high_leverage_loss")
    if str(trade.get("reason", "")).lower() in {
        "failed_setup_reversal",
        "stop_loss",
        "quick_exit_reversal",
    }:
        reasons.append(str(trade.get("reason")).lower())

    return {
        "symbol": trade.get("symbol"),
        "action": trade.get("action"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "pnl_usdt": pnl_usdt,
        "pnl_pct": pnl_pct,
        "leverage": leverage,
        "reason": trade.get("reason"),
        "ai_score": ai_score,
        "ai_confidence": confidence,
        "open_time": trade.get("open_time") or trade.get("entry_time"),
        "close_time": trade.get("close_time") or trade.get("exit_time"),
        "review_flags": reasons,
    }


def _review_bad_trades(days: int, limit: int) -> Dict[str, Any]:
    since = datetime.now() - timedelta(days=max(1, days))
    pnl_trades = _load_pnl_store().get("closed_trades", [])
    mem_trades = _load_trade_memory().get("trades", [])
    combined = []

    for trade in pnl_trades + mem_trades:
        close_time = _parse_trade_time(
            trade.get("close_time") or trade.get("exit_time") or ""
        )
        if close_time and close_time < since:
            continue
        pnl_usdt = float(trade.get("pnl_usdt", 0) or 0)
        pnl_pct = float(trade.get("pnl_pct", 0) or 0)
        ai_score = float(trade.get("ai_score", 0) or 0)
        if pnl_usdt < 0 and (pnl_pct <= -10 or ai_score >= 8 or pnl_usdt <= -1):
            combined.append(_summarize_bad_trade(trade))

    combined.sort(
        key=lambda t: (
            float(t.get("pnl_usdt", 0) or 0),
            float(t.get("pnl_pct", 0) or 0),
        )
    )
    bad_trades = combined[: max(1, limit)]

    by_symbol: Dict[str, Dict[str, Any]] = {}
    for trade in bad_trades:
        sym = trade.get("symbol") or "UNKNOWN"
        item = by_symbol.setdefault(sym, {"count": 0, "pnl_usdt": 0.0, "flags": {}})
        item["count"] += 1
        item["pnl_usdt"] = round(item["pnl_usdt"] + trade["pnl_usdt"], 4)
        for flag in trade.get("review_flags", []):
            item["flags"][flag] = item["flags"].get(flag, 0) + 1

    recommendations = []
    for sym, item in sorted(by_symbol.items(), key=lambda kv: kv[1]["pnl_usdt"]):
        flags = ", ".join(f"{k}x{v}" for k, v in item["flags"].items())
        recommendations.append(
            f"{sym}: review before new entries; {item['count']} bad trade(s), "
            f"{item['pnl_usdt']:+.4f} USDT, flags={flags}"
        )

    return {
        "window_days": days,
        "bad_trade_count": len(bad_trades),
        "bad_trades": bad_trades,
        "recommendations": recommendations,
        "next_step": (
            "Use submit_strategy_feedback to add confirmed reviewer cautions "
            "to trade_memory.json for the bot LLM to consider on future scans."
        ),
    }


def _append_strategy_feedback(arguments: Dict[str, Any]) -> Dict[str, Any]:
    memory = _load_trade_memory()
    feedback = str(arguments.get("feedback", "")).strip()
    if not feedback:
        return {"error": "feedback is required"}

    item = {
        "id": f"mcp-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": arguments.get("source", "openclaw"),
        "symbol": arguments.get("symbol"),
        "severity": arguments.get("severity", "medium"),
        "status": "pending_review",
        "feedback": feedback,
        "evidence": arguments.get("evidence", {}),
    }
    memory.setdefault("strategy_feedback", []).append(item)
    memory["strategy_feedback"] = memory["strategy_feedback"][-100:]
    _save_trade_memory(memory)
    return {
        "saved": True,
        "feedback": item,
        "effect": (
            "The running bot reads pending_review strategy_feedback on the "
            "next scan, asks its internal LLM to review it, then stores "
            "bot_review and marks accepted feedback active."
        ),
    }


def _get_strategy_feedback_reviews(
    status: Optional[str] = None, limit: int = 20
) -> Dict[str, Any]:
    memory = _load_trade_memory()
    items = memory.get("strategy_feedback", [])
    if status:
        items = [item for item in items if item.get("status") == status]
    items = items[-max(1, limit):]
    return {
        "count": len(items),
        "feedback": items,
        "statuses": {
            "pending_review": sum(
                1 for item in memory.get("strategy_feedback", [])
                if item.get("status") == "pending_review"
            ),
            "active": sum(
                1 for item in memory.get("strategy_feedback", [])
                if item.get("status") == "active"
            ),
            "rejected": sum(
                1 for item in memory.get("strategy_feedback", [])
                if item.get("status") == "rejected"
            ),
        },
    }


@app.list_tools()
async def list_tools() -> List[types.Tool]:
    """List all available MCP tools."""
    return [
        # ── Crypto Exchange Tools ──────────────────────────────
        types.Tool(
            name="get_ticker",
            description="Get current price ticker for a trading pair",
            inputSchema={
                "type": "object",
                "properties": {
                    "exchange": {
                        "type": "string",
                        "enum": ["bybit", "binance"],
                        "description": "Exchange to query",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Trading pair (e.g., BTC/USDT:USDT)",
                    },
                },
                "required": ["exchange", "symbol"],
            },
        ),
        types.Tool(
            name="get_ohlcv",
            description="Get OHLCV candle data for technical analysis",
            inputSchema={
                "type": "object",
                "properties": {
                    "exchange": {
                        "type": "string",
                        "enum": ["bybit", "binance"],
                    },
                    "symbol": {"type": "string"},
                    "timeframe": {
                        "type": "string",
                        "enum": ["1m", "5m", "15m", "1h", "4h", "1d"],
                        "default": "1h",
                    },
                    "limit": {"type": "integer", "default": 100},
                },
                "required": ["exchange", "symbol"],
            },
        ),
        types.Tool(
            name="get_exchange_positions",
            description="Get open positions from exchange",
            inputSchema={
                "type": "object",
                "properties": {
                    "exchange": {
                        "type": "string",
                        "enum": ["bybit", "binance"],
                    }
                },
                "required": ["exchange"],
            },
        ),
        types.Tool(
            name="get_exchange_balance",
            description="Get account balance from exchange",
            inputSchema={
                "type": "object",
                "properties": {
                    "exchange": {
                        "type": "string",
                        "enum": ["bybit", "binance"],
                    }
                },
                "required": ["exchange"],
            },
        ),
        types.Tool(
            name="get_funding_rate",
            description="Get current funding rate for a symbol",
            inputSchema={
                "type": "object",
                "properties": {
                    "exchange": {"type": "string", "enum": ["bybit"]},
                    "symbol": {"type": "string"},
                },
                "required": ["exchange", "symbol"],
            },
        ),
        types.Tool(
            name="get_orderbook",
            description="Get order book depth",
            inputSchema={
                "type": "object",
                "properties": {
                    "exchange": {"type": "string", "enum": ["bybit", "binance"]},
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["exchange", "symbol"],
            },
        ),
        # ── Database Tools ─────────────────────────────────────
        types.Tool(
            name="get_trade_history",
            description="Get trade history from database",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "days": {"type": "integer", "default": 30},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        ),
        types.Tool(
            name="get_performance_stats",
            description="Get performance statistics (win rate, PnL, etc.)",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30},
                },
            },
        ),
        types.Tool(
            name="get_signal_history",
            description="Get AI signal history",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        types.Tool(
            name="get_whale_history",
            description="Get whale data history for a symbol",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "hours": {"type": "integer", "default": 24},
                },
                "required": ["symbol"],
            },
        ),
        # ── Bot Control Tools ──────────────────────────────────
        types.Tool(
            name="get_bot_config",
            description="Get current bot configuration",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_bot_health",
            description="Get bot health status",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_bot_status_summary",
            description="Get compact bot status, open dry-run positions, recent PnL, and active MCP feedback",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="review_bad_trades",
            description="Find recent bad trades that a monitoring agent should review before submitting strategy feedback",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "default": 7,
                        "description": "Lookback window in days",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum bad trades to return",
                    },
                },
            },
        ),
        types.Tool(
            name="submit_strategy_feedback",
            description="Submit confirmed monitoring feedback for the bot LLM to consider on future scans. This does not execute trades or edit config.",
            inputSchema={
                "type": "object",
                "properties": {
                    "feedback": {
                        "type": "string",
                        "description": "Concise reviewer caution or learning note",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Optional symbol affected by the feedback",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "default": "medium",
                    },
                    "source": {
                        "type": "string",
                        "default": "openclaw",
                    },
                    "evidence": {
                        "type": "object",
                        "description": "Optional structured evidence from bad-trade review",
                    },
                },
                "required": ["feedback"],
            },
        ),
        types.Tool(
            name="get_strategy_feedback_reviews",
            description="Read strategy feedback items and internal bot LLM review responses",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending_review", "active", "rejected"],
                        "description": "Optional status filter",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                    },
                },
            },
        ),
        types.Tool(
            name="read_pnl_store",
            description="Read PnL store JSON file",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="read_trade_memory",
            description="Read trade memory JSON file",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle tool calls."""

    # ── Crypto Exchange Tools ──────────────────────────────────
    if name == "get_ticker":
        result = await exchange_manager.get_ticker(
            arguments["exchange"], arguments["symbol"]
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_ohlcv":
        result = await exchange_manager.get_ohlcv(
            arguments["exchange"],
            arguments["symbol"],
            arguments.get("timeframe", "1h"),
            arguments.get("limit", 100),
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_exchange_positions":
        result = await exchange_manager.get_positions(arguments["exchange"])
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_exchange_balance":
        result = await exchange_manager.get_balance(arguments["exchange"])
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_funding_rate":
        result = await exchange_manager.get_funding_rate(
            arguments["exchange"], arguments["symbol"]
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_orderbook":
        result = await exchange_manager.get_orderbook(
            arguments["exchange"],
            arguments["symbol"],
            arguments.get("limit", 20),
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── Database Tools ─────────────────────────────────────────
    elif name == "get_trade_history":
        result = await db_manager.get_trade_history(
            arguments.get("symbol"),
            arguments.get("days", 30),
            arguments.get("limit", 100),
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_performance_stats":
        result = await db_manager.get_performance_stats(arguments.get("days", 30))
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_signal_history":
        result = await db_manager.get_signal_history(
            arguments.get("symbol"), arguments.get("limit", 50)
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_whale_history":
        result = await db_manager.get_whale_history(
            arguments["symbol"], arguments.get("hours", 24)
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── Bot Control Tools ──────────────────────────────────────
    elif name == "get_bot_config":
        config_data = {
            "exchange_mode": Config.EXCHANGE_MODE,
            "trading_mode": Config.TRADING_MODE,
            "dry_run": Config.DRY_RUN,
            "symbols": Config.SYMBOLS,
            "account_balance": Config.ACCOUNT_BALANCE,
            "scan_interval": Config.SCAN_INTERVAL,
            "max_open_positions": Config.MAX_OPEN_POSITIONS,
            "ai_provider": Config.AI_PROVIDER,
            "ai_model": Config.BLINK_MODEL,
            "sl_mode": Config.SL_MODE,
            "dca_enabled": Config.DCA_ENABLED,
            "auto_symbols": Config.AUTO_SYMBOLS,
            "smc_filter_mode": Config.SMC_FILTER_MODE,
        }
        return [
            types.TextContent(type="text", text=json.dumps(config_data, indent=2))
        ]

    elif name == "get_bot_health":
        health = {
            "status": "running",
            "timestamp": datetime.now().isoformat(),
            "database": "connected" if DB_PATH.exists() else "not_found",
            "exchanges": list(exchange_manager.exchanges.keys()),
            "config_valid": True,
        }
        return [types.TextContent(type="text", text=json.dumps(health, indent=2))]

    elif name == "get_bot_status_summary":
        from utils.trade_analytics import aggregate_position_trades, calculate_position_metrics

        pnl = _load_pnl_store()
        memory = _load_trade_memory()
        closed_all = pnl.get("closed_trades", [])
        closed = aggregate_position_trades(closed_all)
        metrics = calculate_position_metrics(closed_all)
        recent = closed[-10:]
        realized = round(float(pnl.get("stats", {}).get("total_realized", 0) or 0), 4)
        recent_pnl = round(
            sum(float(t.get("position_pnl_usdt", 0) or 0) for t in recent), 4
        )
        active_feedback = [
            item for item in memory.get("strategy_feedback", [])
            if item.get("status", "active") in ("active", "pending_review")
        ][-10:]
        status = {
            "timestamp": datetime.now().isoformat(),
            "dry_run": Config.DRY_RUN,
            "exchange_mode": Config.EXCHANGE_MODE,
            "trading_mode": Config.TRADING_MODE,
            "scan_interval": Config.SCAN_INTERVAL,
            "open_positions": pnl.get("open_positions", {}),
            "open_position_count": len(pnl.get("open_positions", {})),
            "realized_pnl_usdt": realized,
            "recent_10_pnl_usdt": recent_pnl,
            "closed_trade_count": len(closed),
            "win_rate_pct": round(
                metrics["wins"] / len(closed) * 100, 2
            ) if closed else 0,
            "profit_factor": metrics["profit_factor"],
            "expectancy_usdt": metrics["expectancy_usdt"],
            "max_drawdown_usdt": metrics["max_drawdown_usdt"],
            "memory_trades": len(memory.get("trades", [])),
            "memory_streak": memory.get("streak", {}).get("current", 0),
            "active_strategy_feedback": active_feedback,
        }
        return [types.TextContent(type="text", text=json.dumps(status, indent=2))]

    elif name == "review_bad_trades":
        result = _review_bad_trades(
            int(arguments.get("days", 7)),
            int(arguments.get("limit", 10)),
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "submit_strategy_feedback":
        result = _append_strategy_feedback(arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_strategy_feedback_reviews":
        result = _get_strategy_feedback_reviews(
            arguments.get("status"),
            int(arguments.get("limit", 20)),
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "read_pnl_store":
        data = _load_pnl_store()
        if data:
            return [types.TextContent(type="text", text=json.dumps(data, indent=2))]
        return [
            types.TextContent(type="text", text='{"error": "pnl_store not found"}')
        ]

    elif name == "read_trade_memory":
        data = _load_trade_memory()
        if data:
            return [types.TextContent(type="text", text=json.dumps(data, indent=2))]
        return [
            types.TextContent(
                type="text", text='{"error": "trade_memory not found"}'
            )
        ]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


@app.list_resources()
async def list_resources() -> List[types.Resource]:
    """List all available MCP resources."""
    return [
        types.Resource(
            uri="pnl://history",
            name="PnL History",
            description="Full PnL history from pnl_store.json",
            mimeType="application/json",
        ),
        types.Resource(
            uri="memory://trades",
            name="Trade Memory",
            description="Trade memory and learning data",
            mimeType="application/json",
        ),
        types.Resource(
            uri="config://current",
            name="Current Config",
            description="Current bot configuration",
            mimeType="application/json",
        ),
        types.Resource(
            uri="database://trades",
            name="Trade Database",
            description="Trade history from SQLite database",
            mimeType="application/json",
        ),
        types.Resource(
            uri="database://performance",
            name="Performance Stats",
            description="Performance statistics from database",
            mimeType="application/json",
        ),
    ]


@app.read_resource()
async def read_resource(uri: str) -> str:
    """Read resource content."""

    if uri == "pnl://history":
        data = _load_pnl_store()
        if data:
            return json.dumps(data, indent=2)
        return '{"error": "pnl_store not found"}'

    elif uri == "memory://trades":
        data = _load_trade_memory()
        if data:
            return json.dumps(data, indent=2)
        return '{"error": "trade_memory not found"}'

    elif uri == "config://current":
        config_data = {
            "exchange_mode": Config.EXCHANGE_MODE,
            "trading_mode": Config.TRADING_MODE,
            "dry_run": Config.DRY_RUN,
            "symbols": Config.SYMBOLS,
            "account_balance": Config.ACCOUNT_BALANCE,
            "scan_interval": Config.SCAN_INTERVAL,
            "max_open_positions": Config.MAX_OPEN_POSITIONS,
            "ai_provider": Config.AI_PROVIDER,
            "sl_mode": Config.SL_MODE,
            "dca_enabled": Config.DCA_ENABLED,
        }
        return json.dumps(config_data, indent=2)

    elif uri == "database://trades":
        trades = await db_manager.get_trade_history(limit=100)
        return json.dumps(trades, indent=2)

    elif uri == "database://performance":
        stats = await db_manager.get_performance_stats()
        return json.dumps(stats, indent=2)

    return '{"error": "Unknown resource"}'


# ── Main Entry Point ──────────────────────────────────────────────
async def main():
    """Run the MCP server."""
    # Initialize database
    init_database()
    print(f"[MCP] Database initialized at: {DB_PATH}")
    print(f"[MCP] Exchanges available: {list(exchange_manager.exchanges.keys())}")
    print("[MCP] Server starting in stdio mode...")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


async def run_sse(host: str, port: int):
    """Run the MCP server over SSE for HTTP clients."""
    init_database()
    print(f"[MCP] Database initialized at: {DB_PATH}")
    print(f"[MCP] Exchanges available: {list(exchange_manager.exchanges.keys())}")
    print(f"[MCP] Server starting in SSE mode at http://{host}:{port}/sse")

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,  # Starlette exposes the ASGI send callable here.
        ) as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
        return Response()

    async def health(_request):
        return JSONResponse(
            {
                "status": "ok",
                "server": "trading-bot-mcp",
                "transport": "sse",
                "sse_endpoint": "/sse",
                "message_endpoint": "/messages/",
                "dry_run": Config.DRY_RUN,
            }
        )

    starlette_app = Starlette(
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )
    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trading Bot MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE mode")
    parser.add_argument("--port", type=int, default=8080, help="Port for SSE mode")

    args = parser.parse_args()

    if args.transport == "sse":
        asyncio.run(run_sse(args.host, args.port))
    else:
        asyncio.run(main())
