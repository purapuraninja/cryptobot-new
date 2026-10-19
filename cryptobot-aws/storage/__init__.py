"""
storage — persistence layer for the trading bot's canonical state.

Two backends behind one interface:
  - JSON files (pnl_store.json, trade_memory.json)  [default, unchanged]
  - SQLite (data/trading_bot.db)                     [opt-in, atomic]

Selected via Config.STORAGE_BACKEND ("json" | "sqlite"). Both return the exact
same Python dict shapes the bot already expects, so switching backends is a
config flip with no behavioral change to the trading logic.
"""
