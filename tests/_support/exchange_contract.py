"""The gate over how every venue ends an order (#632).

#629 stalled the soak. Hyperliquid filled an IOC order only in part, and the
saga never ended. Paper already ended that case, and every test of each
adapter was green. Nothing checked that both venues end an order the same way.

``OrderEnd`` names each way a venue can end an order. ``seam_answers`` owes
one answer per member from every exchange. So a new member goes red on every
suite that does not drive it yet.

**Shared assertion, per-adapter driving**, as in ``feed_contract.py``. Each
suite drives its own adapter to the case, then hands the saga here. The
expected end lives here, not in the suites, so the venues cannot drift apart.

Explicit assertion messages throughout: this module is not a test module, so
pytest does not rewrite its asserts and a bare comparison would fail blind.
"""

from decimal import Decimal
from enum import Enum

from tickwright.domain import Order, OrderState


class OrderEnd(Enum):
    """Each way a venue ends an order, and the saga state it must end in."""

    # The venue drops the part it could not fill. The fill stands (ADR-0007).
    IOC_PARTIAL_FILL = OrderState.CANCELLED


def assert_order_ends(order: Order | None, *, case: OrderEnd, filled: Decimal, name: str) -> None:
    """Assert the saga ``name`` drove through ``case`` ended as every venue ends it.

    ``filled`` is what the venue filled. A saga that ends with fewer fills
    than that dropped a fill the account holds.
    """
    assert order is not None, f"{name} drove {case.name} and the engine holds no saga for it"
    assert order.state is case.value, (
        f"{name} ended {case.name} as {order.state.name}. Every venue ends it as "
        f"{case.value.name}, or the saga can stall or disagree across venues (#629)"
    )
    assert order.cum_qty == filled, (
        f"{name} ended {case.name} with {order.cum_qty} filled, but the venue filled {filled}"
    )
