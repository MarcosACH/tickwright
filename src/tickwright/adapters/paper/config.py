"""``PaperExchange`` config — each adapter's config lives in its package
(ADR-0032); only the composition root reads them all."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from tickwright.domain import InstrumentSpec

from .fill_model import StochasticParams

DEFAULT_ACCOUNT_LABEL = "default"
"""The label a paper account opens under when the operator names none.

Declared once and read by both ``PaperExchangeConfig.account_label`` and
``PaperExchange``'s own constructor default, because the two must not drift: a
label decides the ``paper-<label>`` account id, so two spellings of "the
default" would point a directly-constructed exchange and a ``build_engine``-
constructed one at different ledgers (ADR-0042 §5)."""


class PaperExchangeConfig(BaseModel):
    """Config-sourced venue metadata (ADR-0031): the paper venue has no meta
    endpoint, so its per-symbol ``InstrumentSpec``s come from here. The
    composition root also wires them into the venue-agnostic guard.

    The ``fill_model`` discriminant is the one nondeterminism knob (ADR-0012):
    ``immediate`` (the reproducible default) needs neither ``seed`` nor the
    ``stochastic`` knobs; ``stochastic`` reads the ``seed`` (to build the RNG)
    plus the ``StochasticParams`` bundle (nested under ``stochastic``), whose
    defaults are inert so an unseeded stochastic model still behaves
    optimistically.
    """

    instrument_specs: dict[str, InstrumentSpec] = Field(default_factory=dict)
    """Paper's instrument universe — what the meta endpoint is on live (ADR-0031).

    Empty by field default and yet **required in practice**: ``start()`` refuses
    a boot in which a strategy-traded symbol has no spec here (ADR-0044 §9). The
    demand cannot move up to a field constraint or an ``AppConfig`` validator —
    which symbols are traded is known at config load, but ``max_leverage`` lives
    on the spec the *adapter* authors, so the earliest point both paths hold it
    is ``start()`` itself. Empty stays legal because a run with no strategies is.
    """

    fill_model: Literal["immediate", "stochastic"] = "immediate"
    seed: int = 0
    stochastic: StochasticParams = StochasticParams()

    genesis_collateral: Decimal | None = Field(default=None, gt=0)
    """The paper account's opening cash — the one number the whole Tier-1 ledger
    is measured against (ADR-0042 §1).

    Nullable **here** and demanded by ``AppConfig`` when the paper venue is
    selected, which is not a softening: a required field would fire during this
    block's own validation, before any validator can see the ``exchange``
    discriminant, and a partial paper block is the ordinary case — so it would
    force a paper number onto a live run. The ``None`` is the unset marker that
    check reads, never an operator-selectable state.

    ``gt=0``, because an account cannot be *created* owing money. That is input
    validation, not margin enforcement: derived free margin still goes negative
    freely and without consequence at runtime (ADR-0040 §7)."""

    account_label: str = Field(default=DEFAULT_ACCOUNT_LABEL, pattern=r"^[a-z0-9_]{1,32}$")
    """The label half of the ``paper-<label>`` account id (ADR-0042 §5).

    May default where the collateral may not: a wrong label cannot make a number
    wrong, only point at the wrong ledger, which the store's identity check
    catches loudly. **No hyphen**, so a paper id stays unambiguously two segments
    against a live id's three, and never derived from anything ambient — two runs
    of one config must produce the same account identity."""
