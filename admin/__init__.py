"""
admin — operator tooling for the trading bot.

A single entry point (admin.py at the project root) exposes:
  - maintenance commands that mutate pnl_store.json (recount-stats,
    rollback-close, inject-sltp, fix-pnl)
  - launchers for the web dashboard and the MCP server

Every maintenance command backs up pnl_store.json before writing and supports
--dry-run so changes can be previewed safely.
"""
