"""Pure helpers for position-level analytics over closed-trade events.

The PnL store records every partial exit as a separate closed-trade event.  A
position must therefore be reconstructed before trade counts, win rates, or
drawdowns are calculated; otherwise a multi-leg exit is counted more than
once.  The helpers in this module do not perform I/O and never mutate their
inputs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timezone
from math import fsum, isfinite
from typing import Any


Trade = Mapping[str, Any]
AggregatedTrade = dict[str, Any]

_FULLY_CLOSED_REMAINDER = 0.001
_MONEY_DIGITS = 10


def _as_finite_float(value: Any) -> float:
    """Return a finite float, treating absent or malformed PnL as zero."""

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if isfinite(number) else 0.0


def _round_money(value: float) -> float:
    """Remove binary floating-point noise without imposing exchange precision."""

    rounded = round(value, _MONEY_DIGITS)
    return 0.0 if rounded == 0 else rounded


def _is_partial(trade: Trade) -> bool:
    """Interpret the common boolean representations used by JSON and SQLite."""

    value = trade.get("partial", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "partial"}
    return bool(value)


def _has_missing_open_time(trade: Trade) -> bool:
    """Return whether an event lacks a usable lifecycle identifier."""

    value = trade.get("open_time")
    return value is None or (isinstance(value, str) and not value.strip())


def _hashable_identity(value: Any) -> Any:
    """Make unusual identity values safe for use in a grouping key."""

    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _normalise_datetime(value: Any) -> datetime | None:
    """Parse supported close-time values into a comparable naive UTC datetime."""

    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _chronology_key(trade: Trade, input_index: int) -> tuple[Any, ...]:
    """Return a stable chronological key, falling back to source order."""

    parsed = _normalise_datetime(trade.get("close_time"))
    if parsed is None:
        return (1, input_index)
    return (0, parsed, input_index)


def _partial_event_completes_position(trade: Trade) -> bool:
    """Recognise a position fully consumed by its last partial-close event."""

    if trade.get("completed") is True:
        return True

    for field in ("remaining_pct_after", "remaining_pct"):
        if field not in trade or trade.get(field) is None:
            continue
        try:
            remainder = float(trade[field])
        except (TypeError, ValueError, OverflowError):
            continue
        if isfinite(remainder) and remainder <= _FULLY_CLOSED_REMAINDER:
            return True
    return False


def aggregate_position_trades(
    closed: Iterable[Trade] | None,
    include_incomplete: bool = False,
) -> list[AggregatedTrade]:
    """Collapse closed-trade exit events into complete position lifecycles.

    New events are grouped by stable ``position_id``; legacy events fall back
    to ``symbol``, ``action``, and ``open_time``.  Every
    event with a missing/blank ``open_time`` receives a private, per-event
    fallback key so unrelated legacy trades can never be merged accidentally.

    The returned record is a shallow copy of the terminal (non-partial) trade,
    preserving fields such as ``pnl_usdt`` and ``reason``.  Four position-level
    fields are added:

    * ``position_pnl_usdt``: sum of PnL across every exit leg;
    * ``partial_pnl_usdt``: sum of PnL from partial legs only;
    * ``exit_legs``: number of closed-trade events in the lifecycle; and
    * ``completed``: whether the lifecycle has a terminal close.

    A lifecycle is complete when it contains a non-partial event, or when a
    partial event reports a remaining fraction at or below 0.001.  Incomplete
    partial-only lifecycles are omitted unless ``include_incomplete`` is true.
    Results are ordered by terminal close time (source order is the fallback).
    The input iterable and its trade mappings are never modified.
    """

    groups: dict[tuple[Any, Any, Any], list[tuple[int, Trade]]] = {}

    for input_index, trade in enumerate(closed or ()):
        if not isinstance(trade, Mapping):
            continue

        position_id = trade.get("position_id")
        if position_id:
            open_identity = ("position_id", _hashable_identity(position_id))
        elif _has_missing_open_time(trade):
            # Each object is identity-unique and cannot collide with any real
            # timestamp, deliberately preventing legacy rows from merging.
            open_identity: Any = object()
        else:
            open_identity = ("open_time", _hashable_identity(trade.get("open_time")))

        key = (
            _hashable_identity(trade.get("symbol")),
            _hashable_identity(trade.get("action")),
            open_identity,
        )
        groups.setdefault(key, []).append((input_index, trade))

    aggregated: list[tuple[tuple[Any, ...], AggregatedTrade]] = []

    for legs in groups.values():
        full_legs = [(index, trade) for index, trade in legs if not _is_partial(trade)]
        completed = bool(full_legs) or any(
            _partial_event_completes_position(trade) for _, trade in legs
        )
        if not completed and not include_incomplete:
            continue

        terminal_candidates = full_legs or legs
        terminal_index, terminal_trade = max(
            terminal_candidates,
            key=lambda item: _chronology_key(item[1], item[0]),
        )

        partial_pnl = fsum(
            _as_finite_float(trade.get("pnl_usdt"))
            for _, trade in legs
            if _is_partial(trade)
        )
        position_pnl = fsum(
            _as_finite_float(trade.get("pnl_usdt")) for _, trade in legs
        )
        position_margin = fsum(
            _as_finite_float(trade.get("notional"))
            / max(_as_finite_float(trade.get("leverage")), 1.0)
            for _, trade in legs
        )

        position = dict(terminal_trade)
        position.update(
            {
                "position_pnl_usdt": _round_money(position_pnl),
                "partial_pnl_usdt": _round_money(partial_pnl),
                "position_margin_usdt": _round_money(position_margin),
                "position_pnl_pct": (
                    round(position_pnl / position_margin * 100.0, 4)
                    if position_margin > 0 else 0.0
                ),
                "exit_legs": len(legs),
                "completed": completed,
            }
        )
        aggregated.append(
            (_chronology_key(terminal_trade, terminal_index), position)
        )

    aggregated.sort(key=lambda item: item[0])
    return [position for _, position in aggregated]


def calculate_position_metrics(closed: Iterable[Trade] | None) -> dict[str, Any]:
    """Calculate position-level performance metrics from closed-trade events.

    Only completed lifecycles are measured.  Wins, losses, and flats use total
    ``position_pnl_usdt`` rather than the final exit leg.  Drawdown is the
    largest peak-to-trough decline of cumulative position PnL, with positions
    ordered by terminal ``close_time``.  ``profit_factor`` is positive infinity
    when there is profit but no loss, and zero when there is neither.

    ``best_lifecycle`` and ``worst_lifecycle`` contain the full corresponding
    aggregate records (or ``None`` when there are no completed positions).
    Monetary values are denominated in USDT.
    """

    records = list(closed or ())
    # Accept output from aggregate_position_trades as well as raw exit events.
    # The explicit marker prevents confusing new raw terminal rows—which may
    # also carry position_pnl_usdt—with already aggregated lifecycles.
    if records and all(
        isinstance(record, Mapping)
        and "exit_legs" in record
        and "position_pnl_usdt" in record
        for record in records
    ):
        positions = [dict(record) for record in records if record.get("completed", True)]
    else:
        positions = aggregate_position_trades(records, include_incomplete=False)
    pnl_values = [
        _as_finite_float(position.get("position_pnl_usdt"))
        for position in positions
    ]

    winning_values = [pnl for pnl in pnl_values if pnl > 0]
    losing_values = [pnl for pnl in pnl_values if pnl < 0]
    flats = len(pnl_values) - len(winning_values) - len(losing_values)

    gross_profit = fsum(winning_values)
    gross_loss = fsum(losing_values)
    gross_pnl = fsum(pnl_values)

    if gross_loss < 0:
        profit_factor = gross_profit / abs(gross_loss)
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnl_values:
        equity = fsum((equity, pnl))
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    best_lifecycle = (
        max(positions, key=lambda trade: _as_finite_float(trade["position_pnl_usdt"]))
        if positions
        else None
    )
    worst_lifecycle = (
        min(positions, key=lambda trade: _as_finite_float(trade["position_pnl_usdt"]))
        if positions
        else None
    )

    completed_trades = len(positions)
    return {
        "completed_trades": completed_trades,
        "wins": len(winning_values),
        "losses": len(losing_values),
        "flats": flats,
        "gross_pnl_usdt": _round_money(gross_pnl),
        "gross_profit_usdt": _round_money(gross_profit),
        "gross_loss_usdt": _round_money(gross_loss),
        "avg_win_usdt": (
            _round_money(gross_profit / len(winning_values))
            if winning_values
            else 0.0
        ),
        "avg_loss_usdt": (
            _round_money(gross_loss / len(losing_values))
            if losing_values
            else 0.0
        ),
        "profit_factor": (
            _round_money(profit_factor) if isfinite(profit_factor) else profit_factor
        ),
        "expectancy_usdt": (
            _round_money(gross_pnl / completed_trades) if completed_trades else 0.0
        ),
        "max_drawdown_usdt": _round_money(max_drawdown),
        "best_lifecycle": best_lifecycle,
        "worst_lifecycle": worst_lifecycle,
    }
