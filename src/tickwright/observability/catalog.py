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

    # Position changes (``PortfolioProjection``, ADR-0045 §2). A position change
    # is an *output* derived from a fill already on the bus, so it is
    # deliberately not a bus event — which makes this catalog the only place it
    # is observable from outside. A flip through zero emits ``closed`` then
    # ``opened``: the residual opens a fresh average-cost record.
    POSITION_OPENED = "position.opened"
    POSITION_CHANGED = "position.changed"
    POSITION_CLOSED = "position.closed"

    # The live account row created at the startup barrier from the venue's own
    # account read (``PortfolioProjection``, ADR-0042 §6/ADR-0043 §6). Named
    # because the derivation happens once and is never checked again: live
    # genesis is provenance only, so this record is the only account of where a
    # ledger's opening balance came from. Paper's genesis seed emits nothing —
    # its number is in the operator's own config, not read from anywhere.
    ACCOUNT_MATERIALISED = "account.materialised"

    # Live-feed ingress: a conflation drop under backpressure, and a
    # malformed-frame drop that is skipped instead of faulting the engine
    # (``HyperliquidFeed``, ADR-0023).
    FEED_LAGGED = "feed.lagged"
    FEED_FRAME_DROPPED = "feed.frame_dropped"

    # Engine lifecycle (``Engine`` runner, ADR-0024).
    ENGINE_BARRIER_CLEARED = "engine.barrier_cleared"
    ENGINE_FEED_STARTED = "engine.feed_started"
    ENGINE_FAULTED = "engine.faulted"
    ENGINE_STOP_HOOK_FAILED = "engine.stop_hook_failed"

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

    # A live-exchange request that yielded no usable answer, on either path and
    # for either reason (``HyperliquidExchange``): a send or read that failed in
    # **transport** — outcome unknown, no report emitted — or a 200-OK body the
    # adapter cannot **parse**, which is a failed read and never venue truth
    # (ADR-0011 inv 1). ``request`` names the venue request (``place``,
    # ``cancel``, ``userFills``, ``clearinghouseState``, …); ``cloid`` rides
    # along only where the request has one, so the account-grain read carries
    # none. Nothing is reported either way — reconcile-by-cloid resolves an
    # in-flight order (ADR-0008 rule 2) and the account-grain cycle freezes.
    EXCHANGE_REQUEST_FAILED = "exchange.request_failed"
    # Live-exchange write path: the venue refused the whole action envelope (bad
    # nonce/signature, an action rate-limit) at HTTP 200 — distinct from a failed
    # transport and from a per-order REJECTED. No report; reconcile-by-cloid owns
    # the in-flight order (``HyperliquidExchange``, ADR-0008 rule 2).
    EXCHANGE_ACTION_REJECTED = "exchange.action_rejected"
