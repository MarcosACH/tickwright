# Venue read outcomes: a failed read, and the refusal that is not one

_Recorded while delivering [#216](https://github.com/MarcosACH/tickwright/issues/216), which was filed as a bug — `fetch_order` raising on a malformed fill row and faulting the engine — and whose surgical fix would have cemented the very taxonomy that made a second, quieter bug permanent. **Resolves [ADR-0036](./0036-perp-fee-model.md) §4's deferred "fails closed, not fast"**, which named this issue as its owner. **Extends [ADR-0011](./0011-reconciliation-model.md) inv 1**: the freeze-on-a-failed-read contract is unchanged, and gains a stated boundary — what inv 1 does *not* cover. (The sentinel itself became a two-member `VenueReadFailure` in [ADR-0049](./0049-failed-read-blast-radius.md); every `None` written below is that value, and nothing about the three outcomes moved.) **Supports [ADR-0014](./0014-component-lifecycle-and-error-model.md)** (what a violated invariant does) and [ADR-0031](./0031-venue-extensibility-process-per-venue.md) (venue knowledge stays in the venue)._

An in-flight venue read can fail three ways, and only two of them are the same failure.

## 1. Decision

**Every in-flight venue read resolves to one of three outcomes, and the venue package owns the mapping once.**

| Outcome | Raised as | Verdict | Why |
| --- | --- | --- | --- |
| Transport failure | `OSError` (`TimeoutError` among it) | named, `None`, retried at the next deadline | No body arrived. The venue may be reachable next cycle. |
| Unreadable body | `UNREADABLE` — `ArithmeticError`, `KeyError`, `TypeError`, `ValueError` | named, `None`, retried at the next deadline | A body arrived and could not be read. A venue contract change is durable; a truncated or transitional response is not, and nothing here can tell them apart from one sample. |
| **Permanent refusal** | `VenueFactUnsupported` (an `InvariantViolation`) | named, **raised**, faults the engine | The venue reported a fact this engine cannot represent. It is already stored at the venue, so every later read returns it identically. |

`venues/hyperliquid/reading.py` holds all three: `read(request, query, send, normalize) -> T | None` for the two that answer `None`, `UNREADABLE` for the vocabulary they catch, and — by *not* being in that tuple — `VenueFactUnsupported` for the one that does not stop there.

**What decides coverage is whether something above the read retries it — not whether it runs at boot.** `None` is a freeze signal, and freezing only means anything to a caller that will ask again. `fetch_account_state` is a boot read *and* a `read` caller: it runs as a `StartupBarrier` step, and the barrier re-drives its whole sequence with capped backoff until the startup budget is spent, so `None` is precisely the "could not prove it, guessed nothing" its step contract is written around. `universe.py` runs at composition and the ADR-0046 account-mode gate runs in `start()` ahead of the barrier; neither has a retry above it, so each owns its own refusal — the mode gate builds the retry loop it needs and then raises, `universe.py` raises outright, and both fault the barrier rather than freezing it. Two policies, split on the retry.

**(The third outcome has a second member, since [#192](https://github.com/MarcosACH/tickwright/issues/192):** the row above reads "the venue reported a fact this engine cannot represent", which is one way a condition gets there and not the definition. What puts a condition in that row is **permanence** — §2's own closing line, "the distinction is permanence, not severity" — and a *delivery* can be permanently unreadable without any fact having been understood.

That is what a delivery off the `userFundings` socket is when it is not a batch of payments — at **either** depth the check reaches, the frame whose body is not a batch and the record inside a well-formed batch that is not a payment. One condition read twice, not two: an operator acts on both identically, and the second is only "inside" the first because a shape check has to stop somewhere. The table's second row assumes a body that "could not be read" may be truncated or transitional and that nothing can tell them apart from one sample, which is true of an HTTP response and **false of a websocket message**: fragments are the client's to reassemble (RFC 6455 §5.4) and a connection dying mid-message ends iteration instead of yielding half of one, so what reaches a parser is always exactly what the venue chose to send. An unreadable one is a contract change, known at the first read.

The rest of the row's reasoning then carries unchanged: there is no state a retry reaches where it succeeds, and the caller has no `None` to answer with anyway — a subscription's consumer is not a `read`, so freezing is not among its outcomes. It refuses, quoting the frame through `rendered`, and the runner's `TaskGroup` faults the run (ADR-0024). The alternative was to drop the socket and let the resubscribe re-deliver the snapshot, which heals a *transient* malformation and, against a durable one, spins on the backoff forever with the funding line frozen and only a log line per cycle — the silent economic failure ADR-0037's supervision exists to prevent, reached the long way round.

`VenueReadUnresolvable` is the neighbour this is **not**: that one infers permanence from a budget spent, because nothing about a single unreadable body established it. Here the transport establishes it outright, so there is no budget to spend.**)**

## 2. Why the third outcome exists: the transient verdict is a poison pill for a permanent condition

ADR-0036 §4 guards a fill fee settled in any token but USDC, because money in this engine is a bare `Decimal` with USDC left implicit (ADR-0029) and a foreign-token fee has nowhere to go. As shipped, that guard raised a `ValueError` — a member of `UNREADABLE` — so the fills read answered it exactly as it answers a missing field: a named `EXCHANGE_REQUEST_FAILED` and `None`, on which the reconcile cycle freezes.

For a body we could not parse, that is the right verdict. For this condition it compounds into a permanent, silent stall, and three facts have to line up before that is visible:

1. **The venue's stored fill row is immutable.** The read fails identically on every subsequent pass. There is no state the reconciler can reach where it succeeds.
2. **`_drive` returns `self._freeze()` *before* `handle` runs.** So `_resolve_inflight`'s `ConsecutiveMisses` budget never advances, and the escalation that would eventually resolve `FAILED` is unreachable.
3. **`_drive` returns on the first frozen read.** It iterates `self._cache.open_orders()`, so the affected order freezes **every order behind it in the iteration** too, on every cycle.

*Facts 2 and 3 are shape, not content: they hold for every member of `UNREADABLE`, not just the fee. This ADR removed fact 1 for the one condition it could name in advance and left the other two standing — which [#236](https://github.com/MarcosACH/tickwright/issues/236) then reproduced with an unmappable order status. [ADR-0049](./0049-failed-read-blast-radius.md) removes fact 3 at the root (a failed **send** stops the pass; an unreadable **body**, from a venue that is up and answering, skips only its own order) and answers fact 1 generally with a per-cloid budget, the "more than one sample" §1's row 2 says is missing. Fact 2 turns out to need no repair: an unreadable body is not an absent record, so advancing the miss count on one would be the very confusion inv 1 forbids.*

Net effect: one HYPE-settled fee freezes the whole reconciler indefinitely, emitting one `RECONCILE_FROZEN` per cycle and nothing else. No money is ever misstated — which is why the fee slice shipped it — but a permanent silent freeze is not the fail-fast ADR-0036 §4 promises. It is the worst available failure: the engine keeps running, keeps trading, and stops reconciling.

**The distinction is permanence, not severity.** A malformed row and a foreign-token fee are equally serious; they differ in whether waiting can resolve them. That is what makes them different outcomes rather than one outcome with two log levels.

## 3. Why a type and not a flag

`VenueFactUnsupported` is an `InvariantViolation` (ADR-0014), sibling to `VenueAccountModeUnsupported`, and being outside `UNREADABLE` is what makes the third outcome hold **by construction**.

Every guard that answers a transient failure catches `UNREADABLE`. A refusal that is not in that tuple passes through all of them without any of them naming it, deciding about it, or being written to know it exists. The alternative — a sentinel value, or a flag on the return — would put the burden on each call site to remember the distinction, which is exactly the per-site drift §4 exists to end. There are five such sites today and #191/#192/#193 add more.

The verdict on reaching the runner is the ordinary one: `except Exception` → `ComponentState.FAULTED`, `ENGINE_FAULTED` carrying the `repr`, exit 1. **No new named event.** The catalog is closed (ADR-0045), and a second name for a condition that already produces `ENGINE_FAULTED` with the message attached would be a name nothing acts on separately.

**The cost is stated plainly:** a foreign-token fee kills a running process holding positions. That is intended. The condition means the ledger cannot account for money that has already left the account, and the remedy is an operator's, not a retry's. Likelihood is low — perp fees are USDC-settled (ADR-0036 §4), and spot is out of scope (ADR-0030).

## 4. Why the venue owns the read and not just the vocabulary

Before this, "one venue read" was three collaborators: `_info` sent, a per-caller `try`/`except` decided what a transport failure meant, and a normalizer decided what an unreadable body meant. **Which layer owned which failure was chosen freshly at every read site** — so the five in-package sites disagreed:

- `account.py` returned `None` and named it;
- `_fetch_fills` returned `None` and named it, but let `OSError` escape to two callers that handled it differently — one swallowing it, one naming it under a *different* `request` label for the same venue query;
- `_decode_order_status` returned `None` **silently**, and so did the unmappable-status branch beside it — two freezes an operator could not attribute, neither of which had a test, because nothing forced one to exist;
- `_action_outcome` and `universe.py` raised.

The write path had the same hole with the opposite verdict: `status["resting"]["oid"]` and `int(status["filled"]["oid"])` sat inside `OSError`-only guards, so an unreadable field faulted the engine — and one `statuses` shape fell through every branch and reported *nothing at all*.

`read` survives the deletion test: delete it and three sites regrow a `try`/`except`, a namer, and the `None` contract, and are free to disagree about all three again. It is why normalizers here only *read* — they raise and no longer decide, so `normalize_account_state` returns a state rather than a state-or-`None`, and `_order_state` returns a state rather than a maybe.

### 4.1 The write verbs get the split, not just the vocabulary

`place` and `cancel` send a signed action rather than a query, so they cannot call `read` — but they must not decide the taxonomy again either, which is why `failed_send` and `unreadable_body` are the venue read module's public surface alongside it. That much is duplication removed. **The split is the load-bearing half.**

`read` guards a *pure* `normalize` and nothing else, and the write verbs now do the same: `_placement_adjudication` / `_cancel_adjudication` read the body, `_apply_placement` / `_apply_cancellation` carry the result out, and only the first pair is inside the guard. Collapsing the two — reading and publishing under one `except UNREADABLE`, which is how the write verbs were first written — is not a style question. `InMemoryBus.publish` dispatches subscribers inline and re-raises (ADR-0023), and a report published from inside the guard drains its **whole cascade** there: the saga, the checkpoint, the portfolio fold. Every member of `UNREADABLE` is a shape an engine bug takes too — `InvalidOperation` out of a `Decimal` comparison being the likeliest — so each was caught by the *venue adapter*, named `EXCHANGE_REQUEST_FAILED` against a response that had parsed cleanly, and the engine carried on. That is squarely against ADR-0024's containment rule and `runner.py`'s stated guarantee that a raw handler's exception reaches the `TaskGroup` and faults the engine.

The adjudication types are a union (`_ActionError | _Resting | _Rejected | _Filled`) rather than one record with optional fields, so a combination the venue cannot produce is one this adapter cannot represent; and each `match` ends in `assert_never`, so a fifth adjudication fails the type check instead of becoming the silent no-op §4 already catalogues once. `_CancelVerdict.ALREADY_GONE` exists for that same reason: the adapter deliberately emits nothing for a per-cancel error, and an intended silence and an accidental one look identical from outside unless one of them is named. Naming it is only half the guard, though — the verdict also has to mean what it says. `_cancel_adjudication` matches the venue's two documented shapes (`"success"`, and a per-cancel `{"error": …}`) and raises on anything else, rather than reading `ALREADY_GONE` as "not `success`": a catch-all would let a status the venue has never sent make a positive claim that the order is filled, cancelled, or never landed, and go out under the intended silence. That is the same fourth-adjudication hole the placement reader raises on, and both write verbs now refuse it the same way.

## 5. Why it lives in the venue package

Same reasoning as `figure` and `ingress.py` (ADR-0031): the load-bearing facts are Hyperliquid's. Which exceptions a body raises depends on how that venue encodes its figures; which conditions are *permanent* depends on what that venue stores immutably. A second venue may well differ on both. The universal half — that a figure must be a number — already lives in `domain.exact_figure`, and `VenueFactUnsupported` lives in `domain.errors` because faulting the engine is a domain verdict, not a venue one.

A second venue is the signal to promote `read` to a shared home. Not before.

## 6. Consequences

- **A permanent refusal now stops the engine.** Previously it stopped only the reconciler, quietly. Anyone reading `RECONCILE_FROZEN` in a dashboard should know it no longer covers this case — nor, since ADR-0049, the durably unreadable body, which now carries a `scope` field saying how much of the pass it froze.
- **Every failed read is named**, including the two that were silent, and each carries the bounded response body it could not read — key set first, since that is what identifies a contract change.
- **One read has one `request` label, however it failed.** The post-fill fills read used to name its transport failure `fills` and its parse failure `userFills`; both are `userFills` now, which is what the event catalog documents. ADR-0011's R004 distinction — a fills-read failure is not a *place* failure — is untouched. The label names the **read**, not the endpoint: the fills read is `userFills` under both of its queries, the windowed `userFillsByTime` included, because it is one read at one grain that varies only in how far back it looks. What must never vary again is the label moving with the *failure mode*, which is what made one read look like two.
- **An unreadable write body no longer faults the engine.** It is a failed read on the write path too: no report, and reconcile-by-cloid resolves the order (ADR-0008 rule 2). The engine keeps its backstop instead of dying next to it.
- **A failure downstream of a published venue fact still does.** The write guard covers a pure read of the body and stops there (§4.1), so an exception from a subscriber — saga, checkpoint, portfolio fold — reaches the `TaskGroup` as ADR-0024 requires, rather than being filed as a venue that answered badly.
- **The next in-flight read inherits all of it.** #191/#192/#193 pass a `normalize` and get the taxonomy, rather than re-deriving which layer owns what.
