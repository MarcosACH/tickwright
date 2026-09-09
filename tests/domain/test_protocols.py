"""The seam Protocols' *shape* — what a member's placement obliges of callers.

Not a behavioural suite: what each member does is claimed adapter by adapter
(``tests/_support/seam_claims.py``), and what each caller does with it is
asserted where that caller is exercised. What is asserted here is the one thing
neither of those can see — that the ``Exchange`` seam stays **partitioned**, so
a member added to it lands on the anchor its callers actually read rather than
widening what every caller must know.
"""

from typing import get_protocol_members

from tickwright.domain import AccountAnchor, Exchange, OrderAnchor


def test_the_exchange_seam_partitions_into_its_two_anchors_and_the_venue_s_own() -> None:
    """Every ``Exchange`` member belongs to exactly one of three groups.

    The two anchors are ADR-0034's two grains made into seams: the **cloid**
    anchor the ``ExecutionManager`` and the ``Reconciler`` drive, and the
    **account** anchor ``LedgerReconciliation`` drives. What is left is the
    venue's own — the lifecycle the runner sequences (ADR-0024) and the two
    static declarations it reads at composition — and it is left on ``Exchange``
    rather than made a third anchor because no caller narrows to it: the runner
    holds the composite either way, since it is what it hands the three
    components below it.

    The disjointness is the half that would rot silently. A member on both
    anchors would oblige a caller to know a grain it never reads, which is the
    condition the split exists to end; a member on neither would be the drift
    back to one seam, arriving one method at a time.
    """
    order = get_protocol_members(OrderAnchor)
    account = get_protocol_members(AccountAnchor)

    assert not (order & account), f"a member on both anchors: {sorted(order & account)}"
    assert get_protocol_members(Exchange) - order - account == {
        "start",
        "run",
        "stop",
        "account_spec",
        "instrument_specs",
    }
