"""Domain error taxonomy.

An ``InvariantViolation`` is the fail-fast class (ADR-0014): an illegal saga
transition or a broken engine assumption. It is never a handler error — it
pierces containment and faults the engine rather than being logged-and-skipped.
"""


class InvariantViolation(Exception):
    """A load-bearing engine invariant was broken (fail-fast, ADR-0014)."""


class StartupReconciliationTimeout(InvariantViolation):
    """The startup barrier could not reconcile against the venue within its
    bounded window (ADR-0024): the engine must go ``FAULTED`` and exit non-zero
    rather than trade on unverified state — freeze, don't guess (ADR-0011)."""


class StoreAccountMismatch(InvariantViolation):
    """The durable ledger and the venue's ``AccountSpec`` disagree about the
    account this engine is trading (ADR-0042 §3, ADR-0043 §10).

    Raised from the **first** step of the startup sequence, before the order
    cache is rebuilt, so a store that must not be traded at all is refused
    without paying the mass read behind it.

    **One type, two disjoint conditions** — they cannot both fire, since a
    missing account row leaves no fields to compare:

    - recorded fields disagree with what the adapter declares, and
    - a paper store carries order history with no ledger behind it (ADR-0043
      §8), which cannot be backfilled: the per-fill fee only exists on the event
      as of ADR-0036 and funding did not exist at all, so a reconstruction would
      write zeroes for money that was actually charged.

    A second class was rejected because the operator's remedy is the same
    sentence either way — point the run at a fresh store, or restore the
    declared values — so a distinct name would buy precision nobody acts on.
    Every disagreeing field is named at once, so an operator who changed two
    learns both on the first restart rather than one per restart.
    """


class VenueFactUnsupported(InvariantViolation):
    """The venue reported a fact this engine has no way to represent, and will
    report it identically forever (ADR-0048): a fill fee settled in a token
    other than USDC, against a ledger whose money is a bare ``Decimal`` with
    USDC left implicit (ADR-0029).

    The **permanence** is the whole reason this is a type and not a member of
    the venue's ``UNREADABLE`` vocabulary. Its neighbours there describe a body
    we could not parse, where a re-read may well succeed, so their verdict is a
    named ``None`` the cycle retries. A stored venue row is immutable: the read
    fails identically on every later pass, the reconciler's ``_drive`` returns
    its freeze *before* any retry budget advances, and the first affected order
    freezes every order behind it in the iteration too. Answered as transient,
    one such fact would stall the whole reconciler indefinitely on a
    ``RECONCILE_FROZEN`` a cycle and nothing else — a permanent silent freeze
    where ADR-0036 §4 promises a fail-fast.

    So it leaves the venue seam rather than being answered inside it, faulting
    the engine the way a broken boot precondition does
    (``VenueAccountModeUnsupported``): loud, non-zero, and awaiting an operator,
    which is the only thing that can resolve it. No money is ever misstated on
    either verdict — this one is about which failure an operator can see.
    """


class VenueAccountModeUnsupported(InvariantViolation):
    """The venue account is not in a mode whose numbers this engine can read
    (ADR-0046 §3): its abstraction mode is outside Manual/Standard, or could not
    be read at all within the startup budget.

    Deliberately **not** a ``*Mismatch``. Its siblings — ``StoreAccountMismatch``
    (ADR-0042) and ``VenueLeverageMismatch`` (ADR-0044 §5) — report a recorded
    value disagreeing with an observed one, and either one can be reasoned about
    by comparing the two. This one reports that the observed values do not
    *mean* what the engine reads them to mean: under a pooled mode the perps
    clearinghouse is a sub-ledger, so equity and free margin come back an order
    of magnitude low with nothing in the response indicating it. Nothing
    downstream can be compared, so nothing downstream may run.
    """
