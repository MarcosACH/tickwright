# v1 is frictionless: no fees, no margin, no PnL in the engine

The paper exchange's fill model emits **price + quantity only**. Fees, commission, margin, and
PnL accounting are **out of v1 scope**. A fill is a fill; what it does to an account balance is
a separate concern.

This follows the established separation — the matching engine does not apply
commissions/fees; an accounting layer does — and our own deferral of the portfolio/accounting
surface (an open architecture-surface question, leaning deferred). Modelling fees/margin without
that accounting layer would smear money math through the fill path, which is exactly the
coupling the reference implementation should avoid.

A fee/commission model and a margin/accounting layer are **additive seams** introduced only if
and when we add the portfolio surface. Keeping fills pure keeps the MVP honest about what it is:
an order-lifecycle engine, not a P&L simulator.
