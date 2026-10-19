# agent package — trading-loop building blocks.
#
# Cohesive stages extracted from the former monolithic main.py:
#   signals    — dedup / extract AI signal payloads
#   filters    — the per-signal rejection gauntlet + regime gates
#   positions  — DCA, quick-exit, live sync, adaptive rules, symbol refresh
#   commands   — Telegram command processing
#
# main.py orchestrates these; each module here stays free of the top-level
# asyncio loop so the flow is easy to read and audit.
