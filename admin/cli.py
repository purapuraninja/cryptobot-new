"""
admin/cli.py — argparse dispatcher for all operator tooling.

Usage (from project root):
    python admin.py recount-stats [--dry-run] [--yes]
    python admin.py rollback-close SYM [SYM ...] [--reason-prefix time_exit] [--dry-run] [--yes]
    python admin.py fix-pnl SYMBOL (--real-pnl N | --delete) [--close-time "YYYY-MM-DD HH:MM:SS"]
                    [--reason-prefix P] [--dry-run] [--yes]
    python admin.py inject-sltp [--dry-run] [--yes]
    python admin.py dashboard
    python admin.py mcp [--transport stdio|sse] [--host H] [--port P]

Maintenance commands preview their effect and, unless --yes is given, ask for
confirmation before writing. --dry-run never writes and never prompts.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict

from admin import launchers, maintenance, okx_web3
from storage import migrate as storage_migrate


# ── Confirmation helper ─────────────────────────────────────────────────────

def _confirm(assume_yes: bool, prompt: str = "Apply changes?") -> bool:
    if assume_yes:
        return True
    try:
        ans = input(f"{prompt} (y/n): ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes", "ya")


# ── Result printers ─────────────────────────────────────────────────────────

def _print_recount(r: Dict[str, Any]) -> None:
    if not r.get("changed"):
        print(r.get("message", "No change."))
        return
    b, a = r["before"], r["after"]
    print("\n-- BEFORE --")
    print(f"  trades {b.get('total_trades')}  W {b.get('wins')}  L {b.get('losses')}  "
          f"realized {b.get('total_realized')}")
    print("-- AFTER (recount) --")
    print(f"  trades {a['total_trades']}  W {a['wins']}  L {a['losses']}  "
          f"realized {a['total_realized']}")
    print(f"  best  {r['best'][0]:+.4f} ({r['best'][1]})")
    print(f"  worst {r['worst'][0]:+.4f} ({r['worst'][1]})")
    print("-- PER SYMBOL --")
    for sym, v in sorted(r["by_symbol"].items(), key=lambda x: -x[1]["trades"]):
        wr = v["wins"] / max(v["trades"], 1) * 100
        print(f"  {sym:<14} {v['trades']:>3}t  {v['wins']}W/{v['losses']}L  "
              f"WR={wr:.0f}%  PnL={v['pnl']:+.4f}")


def _print_rollback(r: Dict[str, Any]) -> None:
    if not r.get("changed"):
        print(r.get("message", "No change."))
        return
    print(f"\n{'[DRY-RUN] ' if r.get('dry_run') else ''}Removing {len(r['removed'])} close(s):")
    for t in r["removed"]:
        print(f"  - {t['symbol']:<14} PnL={t['pnl_usdt']:+.4f}  {t['reason']}  {t['close_time']}")
    if r.get("after"):
        a = r["after"]
        print(f"  stats -> trades {a['total_trades']}  W {a['wins']}  L {a['losses']}  "
              f"realized {a['total_realized']}")


def _print_fixpnl(r: Dict[str, Any]) -> None:
    if not r.get("changed"):
        print(r.get("message", "No change."))
        return
    t = r["trade"]
    tag = "[DRY-RUN] " if r.get("dry_run") else ""
    if r["action"] == "delete":
        print(f"\n{tag}Delete trade: {t['symbol']} {t['close_time']} "
              f"(was PnL {t['pnl_usdt']:+.4f}, reason {t['reason']})")
    else:
        print(f"\n{tag}Correct trade: {t['symbol']} {t['close_time']} "
              f"PnL {t['pnl_usdt']:+.4f} -> {r['new_pnl']:+.4f}")
    if r.get("after"):
        a = r["after"]
        print(f"  stats -> trades {a['total_trades']}  W {a['wins']}  L {a['losses']}  "
              f"realized {a['total_realized']}")


def _print_inject(r: Dict[str, Any]) -> None:
    if not r.get("changed") and not r.get("updated"):
        print(r.get("message", "No change."))
        return
    tag = "[DRY-RUN] " if r.get("dry_run") else ""
    print(f"\n{tag}SL/TP for {len(r['updated'])} position(s):")
    for p in r["updated"]:
        print(f"  [{p['symbol']}] {p['action']}  entry={p['entry']}  cur={p['current']}  "
              f"ATR={p['atr_pct']:.2f}%  state={p['state']}")
        print(f"      SL={p['sl']}  TP={p['tp']}  trail={p['trail_pct']}%  "
              f"trailing={'on' if p['trailing_active'] else 'off'}")
    for e in r.get("errors", []):
        print(f"  ! {e['symbol']}: {e['error']}")


# ── Command handlers ────────────────────────────────────────────────────────

def _cmd_recount(args) -> int:
    r = maintenance.recount_stats(dry_run=True)  # preview first
    _print_recount(r)
    if args.dry_run or not r.get("changed"):
        return 0
    if not _confirm(args.yes, "Save recounted stats to pnl_store.json?"):
        print("Cancelled.")
        return 1
    r = maintenance.recount_stats(dry_run=False)
    print(f"\nSaved. Backup: {r.get('backup')}")
    return 0


def _cmd_rollback(args) -> int:
    r = maintenance.rollback_close(args.symbols, reason_prefix=args.reason_prefix, dry_run=True)
    _print_rollback(r)
    if args.dry_run or not r.get("changed"):
        return 0
    if not _confirm(args.yes, "Remove these closes and rebuild stats?"):
        print("Cancelled.")
        return 1
    r = maintenance.rollback_close(args.symbols, reason_prefix=args.reason_prefix, dry_run=False)
    print(f"\nSaved. Backup: {r.get('backup')}")
    return 0


def _cmd_fixpnl(args) -> int:
    try:
        r = maintenance.fix_pnl(
            symbol=args.symbol, real_pnl=args.real_pnl, close_time=args.close_time,
            reason_prefix=args.reason_prefix, delete=args.delete, dry_run=True,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return 2
    _print_fixpnl(r)
    if args.dry_run or not r.get("changed"):
        return 0
    if not _confirm(args.yes, "Apply this fix and rebuild stats?"):
        print("Cancelled.")
        return 1
    r = maintenance.fix_pnl(
        symbol=args.symbol, real_pnl=args.real_pnl, close_time=args.close_time,
        reason_prefix=args.reason_prefix, delete=args.delete, dry_run=False,
    )
    print(f"\nSaved. Backup: {r.get('backup')}")
    return 0


def _cmd_inject(args) -> int:
    r = maintenance.inject_sltp(dry_run=True)
    _print_inject(r)
    if args.dry_run or not (r.get("updated")):
        return 0
    if not _confirm(args.yes, "Write SL/TP to these positions?"):
        print("Cancelled.")
        return 1
    r = maintenance.inject_sltp(dry_run=False)
    print(f"\nSaved. Backup: {r.get('backup')}")
    return 0


def _cmd_storage(args) -> int:
    if args.storage_action == "migrate":
        summary = storage_migrate.migrate_json_to_sqlite()
        print("Migration summary:")
        for line in summary.get("migrated", []):
            print(f"  migrated: {line}")
        for line in summary.get("skipped", []):
            print(f"  skipped : {line}")
        print("\nRun `python admin.py storage verify` to confirm equivalence "
              "before setting STORAGE_BACKEND=sqlite.")
        return 0

    if args.storage_action == "verify":
        ok, report = storage_migrate.verify_equivalence()
        for doc, diffs in report.items():
            status = "OK" if not diffs else f"{len(diffs)} DIFFS"
            print(f"  {doc}: {status}")
            for d in diffs[:10]:
                print(f"    - {d}")
        print(f"\nEQUIVALENT: {ok}")
        return 0 if ok else 1

    if args.storage_action == "export":
        summary = storage_migrate.export_sqlite_to_json()
        print("Export summary:")
        for name in summary.get("exported", []):
            print(f"  written: archive/{name}")
        if summary.get("pnl_store"):
            ps = summary["pnl_store"]
            print(f"  pnl_store: {ps['closed_trades']} closed, {ps['open_positions']} open")
        if summary.get("trade_memory"):
            tm = summary["trade_memory"]
            print(f"  trade_memory: {tm['trades']} trades, {tm['symbols']} symbols")
        return 0

    print("Unknown storage action.")
    return 2


def _cmd_dashboard(args) -> int:
    return launchers.run_dashboard()


def _cmd_mcp(args) -> int:
    return launchers.run_mcp(transport=args.transport, host=args.host, port=args.port)


def _cmd_okx_web3(args) -> int:
    report = okx_web3.inspect_okx_web3(symbols=args.symbols or None, fetch=not args.no_fetch)
    print("OKX Web3 enrichment:")
    print(f"  enabled: {report['enabled']}")
    print(f"  auth: {report['auth']}")
    print(f"  base_url: {report['base_url']}")
    print(f"  default_chain: {report['default_chain']}")
    print(f"  max_symbols: {report['max_symbols']}")
    print(f"  auto_resolve: {report['auto_resolve']}")
    print(f"  resolve_min_liquidity_usd: {report['resolve_min_liquidity_usd']}")
    print(f"  min_interval_ms: {report['min_interval_ms']}")
    print(f"  max_429_retries: {report['max_429_retries']}")
    print(f"  base_429_delay_ms: {report['base_429_delay_ms']}")
    mapped = ", ".join(report["mapped_symbols"]) or "none"
    requested = ", ".join(report["requested_symbols"]) or "none"
    print(f"  mapped_symbols: {mapped}")
    print(f"  requested_symbols: {requested}")

    fetched = report.get("fetched")
    if fetched is None:
        print("  fetch: skipped (--no-fetch)")
        return 0

    print(f"  fetched: {fetched.get('available_count', 0)}/{fetched.get('requested_count', 0)}")
    auto_resolved = fetched.get("auto_resolved") or {}
    for symbol, meta in sorted(auto_resolved.items()):
        print(
            f"  auto-resolved {symbol}: {meta.get('symbol')} {meta.get('name')} "
            f"liq={meta.get('liquidity')} holders={meta.get('holders')}"
        )
    by_symbol = fetched.get("by_symbol", {})
    for symbol, item in sorted(by_symbol.items()):
        print(f"  {symbol}: {okx_web3.summarize_symbol(item)}")
    for symbol, errors in sorted((fetched.get("errors") or {}).items()):
        for error in errors[:3]:
            print(f"  ! {symbol}: {error}")
    return 0


# ── Parser ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="admin.py",
        description="Trading bot operator CLI (maintenance + launchers).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def _add_common(sp):
        sp.add_argument("--dry-run", action="store_true", help="preview only, never write")
        sp.add_argument("--yes", action="store_true", help="skip confirmation prompt")

    sp = sub.add_parser("recount-stats", help="rebuild stats from closed_trades")
    _add_common(sp)
    sp.set_defaults(func=_cmd_recount)

    sp = sub.add_parser("rollback-close", help="remove false closes and rebuild stats")
    sp.add_argument("symbols", nargs="+", help="symbols to roll back (e.g. HUSDT SIRENUSDT)")
    sp.add_argument("--reason-prefix", default="time_exit",
                    help='only remove closes whose reason starts with this (default "time_exit"; "" = any)')
    _add_common(sp)
    sp.set_defaults(func=_cmd_rollback)

    sp = sub.add_parser("fix-pnl", help="correct or delete a recorded trade, then rebuild stats")
    sp.add_argument("symbol", help="symbol of the trade to fix (e.g. DASHUSDT)")
    sp.add_argument("--real-pnl", type=float, default=None, help="overwrite pnl_usdt with this value")
    sp.add_argument("--delete", action="store_true", help="delete the matching trade entirely")
    sp.add_argument("--close-time", default=None, help='exact close_time match, "YYYY-MM-DD HH:MM:SS"')
    sp.add_argument("--reason-prefix", default=None, help="only match closes whose reason starts with this")
    _add_common(sp)
    sp.set_defaults(func=_cmd_fixpnl)

    sp = sub.add_parser("inject-sltp", help="attach ATR-based SL/TP to open positions missing them")
    _add_common(sp)
    sp.set_defaults(func=_cmd_inject)

    sp = sub.add_parser("storage", help="migrate/verify the SQLite storage backend")
    ssub = sp.add_subparsers(dest="storage_action", required=True)
    ssub.add_parser("migrate", help="copy pnl_store.json + trade_memory.json into SQLite")
    ssub.add_parser("verify", help="assert SQLite copy is identical to the JSON source")
    ssub.add_parser("export", help="export SQLite state to timestamped JSON in archive/")
    sp.set_defaults(func=_cmd_storage)

    sp = sub.add_parser("dashboard", help="launch the web dashboard (port 5050)")
    sp.set_defaults(func=_cmd_dashboard)

    sp = sub.add_parser("mcp", help="launch the MCP server")
    sp.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(func=_cmd_mcp)

    sp = sub.add_parser("okx-web3", help="inspect OKX Web3 enrichment config/results")
    sp.add_argument("symbols", nargs="*", help="optional symbols to fetch, e.g. WIFUSDT PEPEUSDT")
    sp.add_argument("--no-fetch", action="store_true", help="print config only; do not call OKX")
    sp.set_defaults(func=_cmd_okx_web3)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
