"""The named-event catalog — the one importable, walkable list of every named
lifecycle event the engine may emit (ADR-0020).

Membership is the contract. ``named_event`` accepts only a ``NamedEvent`` (or a
string equal to one), so a typo or an undocumented name raises at the call site
rather than shipping a silent, un-asserted log record. The catalog is
deliberately *closed*: a later slice that adds a state-affecting path (the live
feed's ``feed.lagged``, say) adds its name here as part of its own acceptance —
the catalog-walk test then forces a path and a test to exist for it.

Only names whose emitting path already ships live here; the ADR-0020 vocabulary
is the roadmap, this enum is what is wired today.
"""

from enum import StrEnum


class NamedEvent(StrEnum):
    """Every named lifecycle event with a shipping emit path (ADR-0020)."""

    # Signal handling → saga transitions (``ExecutionManager``).
    ORDER_PLACED = "order.placed"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_LIVE = "order.live"
    ORDER_PARTIALLY_FILLED = "order.partially_filled"
    ORDER_FILLED = "order.filled"
    ORDER_DENIED = "order.denied"
    ORDER_REJECTED = "order.rejected"
    ORDER_FAILED = "order.failed"
    ORDER_CANCELLED = "order.cancelled"

    # Pre-trade kill switch (``PreTradeGuard``).
    GUARD_KILL_SWITCH_TRIPPED = "guard.kill_switch_tripped"
    GUARD_KILL_SWITCH_RESET = "guard.kill_switch_reset"

    # Strategy containment (``StrategyHost``).
    STRATEGY_ERROR = "strategy.error"
    STRATEGY_SNAPSHOT_INCOMPATIBLE = "strategy.snapshot_incompatible"

    # Reconciliation verdicts (``Reconciler``).
    INFLIGHT_RECONCILED = "inflight.reconciled"
    RECONCILE_RECENCY_SKIPPED = "reconcile.recency_skipped"
    GHOST_RECONCILED = "ghost.reconciled"
    RECONCILE_FROZEN = "reconcile.frozen"
