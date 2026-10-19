"""
risk — deterministic risk-management stage.

Pure, testable functions that sit between the AI signal and the executor.
No network calls, no exchange side effects. Every decision is auditable via
`write_decision_audit` (appends a JSON line to logs/decision_audit.jsonl).
"""

from risk.decision_engine import (
    DecisionResult,
    apply_expectancy_filter_and_sizing,
    apply_max_loss_cap,
    write_decision_audit,
)

__all__ = [
    "DecisionResult",
    "apply_expectancy_filter_and_sizing",
    "apply_max_loss_cap",
    "write_decision_audit",
]
