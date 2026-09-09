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
    # A venue read that failed, healing nothing (ADR-0011 inv 1). ``scope`` says
    # how much it froze: ``cycle`` when the send died — the venue may be
    # unreachable, so nothing behind that order was read — or ``order`` when the
    # body arrived unreadable, where that order alone was skipped and the rest of
    # the pass reconciled normally (ADR-0049). Both carry the cloid, so the field
    # is the only thing telling an outage from a venue contract change.
    RECONCILE_FROZEN = "reconcile.frozen"

    # One completed account-grain cycle (``LedgerReconciliation``, ADR-0034).
    # Emitted on every pass, agreeing or not: the cycle is the *only* evidence
    # the ledger was cross-checked against the venue at all, and a record that
    # appeared solely on disagreement could not tell a healthy book from a
    # cadence that stopped running.
    #
    # Carries what the pass *found* — ``tier_1``/``tier_2`` counts, as its two
    # order-grain siblings carry their ``resolution`` — because the heal and the
    # alert that will act on the classification land in later slices, so until
    # then this record is the only thing reading it. ``unvalued`` is the third
    # outcome those counts would hide: a Tier-2 figure whose mark is absent is
    # dropped rather than reported (ADR-0041 §6), which is correct and would
    # otherwise make a pass that never looked identical to one that agreed.
    # ``suppressed`` is the fourth, and the other half of that pair: a figure
    # that *was* computed, cleared the band, and was withheld anyway because the
    # mark behind it was too old to band (ADR-0040 §6). The two range over
    # different sets — dropped before the band against withheld after it — which
    # is what lets an operator tell a book that was never valued from one whose
    # mark stream froze. Either count alone reads as the silence a healthy book
    # also produces.
    ACCOUNT_RECONCILED = "account.reconciled"
    # The same grain's failed read: the account anchor came back empty, so
    # nothing was inferred from it rather than reading an outage as a flat book
    # (ADR-0011 inv 1). Its own name beside the order grain's ``reconcile.frozen``
    # because the grain differs — this one froze a whole account cross-check, not
    # a cloid — and because the two never share a scope field.
    #
    # Covers **both** of the account read's callers, deliberately: the cadence,
    # where a freeze costs one pass, and the startup barrier's materialisation,
    # where it exhausts the startup budget and faults the process. One anchor
    # failing one way is one name — an operator telling an outage at boot from
    # an outage an hour in reads the ``scope`` field (``barrier``/``cadence``),
    # not a second vocabulary. That is the same call ``reconcile.frozen`` makes
    # one grain up: the catalog is closed and nothing routes on the difference,
    # but the cost differs by the whole run, so plenty reads it.
    ACCOUNT_RECONCILE_FROZEN = "account.reconcile_frozen"
    # One Tier-1 heal the cycle actually booked — the "why did it move" record
    # ADR-0034 requires of every correction, emitted once per heal rather than
    # once per pass because the pass's own record counts findings and a count
    # cannot answer what a moved ledger moved *between*.
    #
    # Carries both sides (``ledger``/``venue``) for ``Divergence``'s reason: a
    # delta alone tells a missed fill from a duplicated one in neither
    # direction. ``symbol`` is ``None`` on the cash correction, which is held at
    # the account grain (ADR-0041 §2), and ``field`` distinguishes the two.
    #
    # ``event_id`` is the key the synthetic was applied under, not a fresh id
    # for the record: it is what joins a healed partition in the store back to
    # the pass that booked it, and what identifies a redelivered heal as the one
    # that was deduped rather than a second correction.
    #
    # A finding the cycle could not heal emits nothing here — it stays a
    # Divergence on the pass's own count — because a record of a heal that never
    # landed is worse than the silence: it would close the audit question it
    # exists to answer with the wrong answer.
    ACCOUNT_HEALED = "account.healed"
    # The cash heal the cycle refused because the venue's account abstraction
    # mode could no longer be verified (ADR-0046 §4). The account grain's
    # cross-check has stopped; the position grain and the local ledger carry on.
    #
    # Deliberately **not** a ``*_DIVERGENCE``. Its two neighbours report a
    # number disagreeing with the venue; this one reports that the two numbers
    # can no longer be compared at all, which is a different thing to page on —
    # a divergence asks which side is wrong, this asks nobody anything until an
    # operator puts the account back.
    #
    # ``reason`` carries ``changed`` against ``unreadable``, the one distinction
    # the control flow does *not* make: both fail closed, because an unverified
    # mode is not evidence of an unchanged one. It exists for the operator, who
    # is sent to the venue UI by one and to the network by the other.
    #
    # The ``unreadable`` half arrives **beneath** an ``EXCHANGE_REQUEST_FAILED``
    # naming the ``userAbstraction`` read that failed, since a mode the adapter
    # could not read is a failed venue read like any other. Two records rather
    # than one because they answer different questions: that one says why the
    # venue could not be read and quotes the body, this one says what the engine
    # stopped doing about it. A ``changed`` mode has no such pair — the venue
    # answered, and the answer was a mode we refuse.
    ACCOUNT_MODE_UNVERIFIED = "account.mode_unverified"
    # A Tier-2 figure the cycle recomputed and the venue's own number disagreed
    # with by more than the band allows (ADR-0040 §6). **Alert-only, always**:
    # Tier-2 is recomputed from ``(position, mark)`` on every read and never
    # stored, so there is nothing here to heal — a stale mark is not a wrong
    # ledger, and a correction would be one applied to a number that will be
    # thrown away and derived again on the next read.
    #
    # Carries ``field``/``symbol``/``ledger``/``venue``, the shape
    # ``account.healed`` carries and for the same reason: a delta cannot tell a
    # mark the engine has not seen yet from a position it has mis-valued. It
    # deliberately does **not** carry the band it broke — the band is config an
    # operator already has, while the pair is the only thing the pass knows.
    VALUATION_DIVERGENCE = "valuation.divergence"
    # The venue's stored leverage or margin mode for a held symbol no longer
    # equal to the pair config asked for (ADR-0044 §10). Its neighbour above
    # reports a *computed* number drifting inside a tolerance; this reports a
    # discrete **operator setting**, so the comparison is an exact match and
    # there is no band — a pair has nothing to measure a tolerance in.
    #
    # **Alert-only, and specifically never a re-push.** An operator who lowers a
    # leverage in the venue UI to de-risk a live position is making a decision
    # about live risk with more context than a config file has; an engine that
    # re-imposed the boot-time value would undo that silently, and at the moment
    # it matters most. Config wins at startup, the venue wins in flight.
    #
    # Carries ``symbol`` plus **both** pairs, spelled out —
    # ``configured_mode``/``configured_leverage`` against
    # ``venue_mode``/``venue_leverage``. Configured rather than "ledger",
    # because the engine's side of this comparison is what an operator wrote and
    # not a figure the ledger accumulated, which is the one place this record's
    # vocabulary parts from its two neighbours'. Both sides ride it for the
    # reason they do: the operator who made the change is being told which of
    # the two settings the margin model keeps computing against.
    #
    # Plus one bit, ``declared``, which is the caveat on that word. The check
    # ranges over the venue's rows, so it reaches a symbol the resolved book
    # never named — a leftover position, or one opened by hand — where the
    # engine's side is the safe fallback rather than a choice. Still alerted,
    # because the fallback is what the margin model really computes that row
    # against once the Tier-1 heal books it in; but ``declared=false`` is
    # different advice, and an operator sent to reconcile a config that does not
    # mention the symbol is being sent to the wrong file.
    LEVERAGE_DIVERGENCE = "leverage.divergence"

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
    # The boot-time leverage push declining to write: the venue already holds a
    # position at the configured mode and leverage, so nothing was sent
    # (``preflight.push_leverage``, ADR-0044 §7). Carries ``symbol``/``mode``/
    # ``leverage`` — the grain an operator compares against config.
    #
    # Named from the **skip** branch and only from it, which is not where a
    # reader expects to find it. A no-op ``updateLeverage`` returns the identical
    # ``ok`` envelope a real change does (measured in #142), so the write path
    # cannot tell the two apart; the branch that declined to write is the one
    # place "already aligned" is knowable at all. There is deliberately no
    # counterpart naming a push that *did* land — the push runs once at boot and
    # never again, so the absence of this name over a symbol in the book is what
    # says the venue was moved.
    EXCHANGE_LEVERAGE_UNCHANGED = "exchange.leverage_unchanged"
