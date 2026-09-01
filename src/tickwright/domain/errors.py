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
    named ``VenueReadFailure`` the cycle retries — and, when the retrying stops
    helping, a per-cloid span that escalates to ``VenueReadUnresolvable``
    (ADR-0049 §4). That span exists to *find out* whether a condition is durable,
    by waiting. This one is durable at the first read and known to be: a stored
    venue row is immutable, so the read fails identically on every later pass.
    Answered as transient, one such fact would skip its order and re-read it for
    the whole span before faulting — and fault naming only the cloid, where this
    type names the fact, which is the one thing an operator's first question
    turns on. Waiting to learn what is already known is the opposite of the
    fail-fast ADR-0036 §4 promises.

    So it leaves the venue seam rather than being answered inside it, faulting
    the engine the way a broken boot precondition does
    (``VenueAccountModeUnsupported``): loud, non-zero, and awaiting an operator,
    which is the only thing that can resolve it. No money is ever misstated on
    either verdict — this one is about which failure an operator can see.
    """


class VenueReadUnresolvable(InvariantViolation):
    """One order's venue body has been unreadable for the whole of its budget,
    so the condition is durable and no further pass can resolve it (ADR-0049).

    The **sibling** of ``VenueFactUnsupported`` and split from it on what was
    known, not on what happened. That one is raised where a fact was *read* and
    understood and had nowhere to go, so the venue package can name the fact in
    the refusal; this one is raised where nothing could be read at all, and the
    only thing anyone knows about it is that it kept failing. Merging the two
    would put "we understood it" and "we never parsed it" under one name for a
    difference an operator's first question turns on.

    **Permanence is inferred, not observed**, which is the other half of the
    split. ADR-0048 §1 grants that a single unreadable body cannot be told from
    a truncated or transitional one — so a single one stays transient, and the
    escalation is what several consecutive ones across the cadence buy. Below
    the budget the order is skipped and the pass carries on; at it, the honest
    verdict is that waiting has been tried and did not work.

    Terminal-by-guess was the alternative and is worse: resolving the order
    ``FAILED`` or ``REJECTED`` would mint saga truth out of a body this process
    never read, against an order that may well be live and filling. Faulting
    misstates nothing — it stops, names the cloid, and leaves the venue's own
    strings in the ``EXCHANGE_REQUEST_FAILED`` reads behind it (each quoting the
    body it could not read) for the operator the condition belongs to.
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


class VenueLeverageMismatch(InvariantViolation):
    """The venue holds a position at a leverage or margin mode the configured
    ``LeverageSpec`` disagrees with (ADR-0044 §5).

    The one place config does **not** win at startup. The boot-time push aligns
    the venue to config for every symbol the account is flat in, because a flat
    symbol has no risk to re-margin — but a *held* position does, and writing to
    it would silently move real collateral: an operator who lowered a leverage in
    the venue UI to de-risk an open position would find the next boot putting it
    back. The engine cannot tell that apart from a config edit nobody applied, so
    it refuses rather than guessing which side is stale.

    Names every disagreeing symbol at once, and both pairs for each. The venue
    twin of ``StoreAccountMismatch``, for its reason: one-per-restart reporting
    makes a two-symbol drift take two reboots to discover, and the second reboot
    happens with the market moving. Both pairs because either side may be the
    wrong one — the remedy is sometimes config and sometimes the venue UI.

    Split from ``LeverageOutOfBounds`` because the two send an operator to
    different places on different evidence: that one is a configured value no
    instrument could ever carry, refusable without asking the venue anything,
    and it runs first for exactly that reason. This one needs the account read
    and is about a value the venue would happily accept.
    """


class VenueLeveragePushFailed(InvariantViolation):
    """The boot-time leverage push could not be completed against the venue
    (ADR-0044 §6): a call it needed kept failing until the startup budget ran
    out.

    Split from ``VenueLeverageMismatch`` on the same line the mode gate splits
    an unreadable mode from a refused one. That one is a *fact the venue told
    us* — a held position at a setting config disagrees with, which an operator
    can reconcile by looking at two printed pairs. This one is the absence of an
    answer: the account is left in neither the configured state nor a known one,
    and there is nothing to compare because the venue never said anything.
    Folding them would send an operator to the venue UI to fix a leverage that
    may well already be right.

    Retried before it is raised, because a boot-time blip is real and the same
    budget the startup barrier gets already covers it (ADR-0043 §6's precedent:
    extend the one boot window rather than mint a second timeout). Clearing
    startup with a venue this process failed to align is not an available
    outcome — that is the state the push exists to prevent.
    """


class LeverageOutOfBounds(InvariantViolation):
    """A configured ``LeverageSpec`` does not satisfy its instrument's bound
    (ADR-0044 §9): ``1 ≤ leverage ≤ spec.max_leverage``, or the symbol carries
    no ``InstrumentSpec`` to bound it at all.

    Raised from ``Exchange.start()`` on **both** paths, which is where the check
    has to live: ``max_leverage`` is on the spec, and the spec is the adapter's
    to author — from the meta endpoint on live, from ``PaperExchangeConfig`` on
    paper — so ``start()`` is the earliest moment both paths hold it. That is
    what splits it from ADR-0044 §3's dead-entry rejection, which *is* an
    ``AppConfig`` validator because both its inputs are config fields.

    Deliberately not a ``*Mismatch``. Its live-only sibling
    ``VenueLeverageMismatch`` reports config disagreeing with a value the venue
    *holds*; this one reports config disagreeing with the instrument's own
    published limit, which is true before any venue is asked and identically
    true on a paper run with no venue to ask. Every offending symbol is named
    with its bound at once, so an operator who over-levered two learns both on
    the first start.

    The two conditions are reported as **separate clauses** of that one message,
    because they send an operator to different places: an out-of-bounds leverage
    is theirs to lower, while a missing spec is a hole in the exchange's
    instrument universe that a leverage they may never have configured had
    nothing to do with.
    """
