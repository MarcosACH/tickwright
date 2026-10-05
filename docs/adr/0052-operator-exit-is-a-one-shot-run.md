# Operator exit: a one-shot run with the engine stopped

The engine can enter a position safely, but the operator has no safe way out. The kill switch
stops new orders. It leaves resting orders and positions alone (ADR-0026). This ADR decides how the
operator asks for cancel all and flatten, and what each one touches. The terms are in
`CONTEXT.md`. Decided in [#410](https://github.com/MarcosACH/tickwright/issues/410), part of
[#408](https://github.com/MarcosACH/tickwright/issues/408).

## Decision

- **Two commands.** `tickwright cancel-all` cancels every resting order in the account and exits.
  `tickwright flatten` runs cancel all first, then closes every position, then exits. Flatten can
  never skip cancel all, because the command runs it itself.
- **The engine is stopped first.** Each command is a one-shot run. It boots, recovers, and
  reconciles like a normal run. It starts no strategy. Then it does its one job and exits.
- **Account scope.** Operator cancel all also cancels orders the engine did not place, such as
  orders placed by hand in the venue UI. Flatten also closes positions in the unattributed
  partition (ADR-0038). This is an exception to ADR-0011. See its correction block.
- **A store lock.** A running engine holds an exclusive lock on its store. For SQLite this is a
  file lock. For Postgres it is an advisory lock. A one-shot run takes the same lock. It refuses to
  start while another process holds it.

## Considered options

- **More signals.** `SIGHUP` for cancel all and `SIGQUIT` for flatten. Signals carry no data, and
  both already mean something else by convention. A stray signal could close every position.
- **A local admin socket.** The running engine would listen on a Unix socket. This is a new live
  control surface. A strategy keeps running during the exit and can reopen a position.
- **An operator exit for one strategy.** For example `tickwright flatten --strategy X`. A strategy
  can already exit itself, with a reduce-only order and cancel all on its own orders. An operator
  flatten for one strategy would see only that strategy's share of the position. ADR-0026 also
  defers a per-strategy kill switch. So both commands always cover every strategy in the account.
- **One `exit` command for both.** An operator sometimes wants to stop quoting but keep a position.
  A market maker before a news event is one example. One command would force a close.
- **Engine orders only.** This keeps ADR-0011 whole. But a hand-placed order that still rests after
  flatten can fill and reopen exposure. Cancel first exists to stop exactly that case.
- **A documented rule instead of a lock.** Nothing would catch an operator who forgets to stop the
  engine. Both runs read the same config, so they always point at the same store. The lock catches
  exactly this mistake.

## Consequences

- An exit starts a few seconds later. The operator stops the engine, and the run boots first.
- The lock also refuses two engines that share one store by accident. Before this, that case passed
  the account binding check (ADR-0038). Two engines on one account with separate stores stay
  undetectable.
- Still open in #408: how an exit run meets the kill switch, who owns a flatten order and its
  fills, how far a flatten order may slip, and what the operator sees.
