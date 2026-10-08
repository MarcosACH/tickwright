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
  start while another process holds it. The lock must end when the process that holds it dies. A
  crash must never block the exit run, because that is when the operator needs it most. So on
  SQLite it is an OS lock on a separate file next to the database, such as `tickwright.db.lock`.
  The file existing means nothing. Only a held lock counts, so a crash leaves no stale lock. The
  lock is never on the database file itself. SQLite takes its own POSIX locks there, and POSIX
  drops all of a process's locks on a file when any handle to it closes.
  **(Extended in [#440](https://github.com/MarcosACH/tickwright/issues/440):** an exit run refuses
  a store with no account row, because no engine ever ran there. That is almost always the wrong
  directory. Boot creates the account row, so the run checks before it writes anything. The error
  prints the store's absolute path. The flag `--new-store` lets the run go anyway. It is for a lost
  host, where every store is new. The run then warns that it cannot see an engine on another store.
  A `--new-store` run leaves an account row behind. A later exit run on that store is not refused.
  Reconcile puts the venue's positions in the unattributed partition, and flatten closes those. A
  held lock is refused at once, with no wait and no flag to break it. On SQLite the engine writes
  its pid into the lock file, and the refusal prints the path and that pid. The pid is only for the
  message. The held lock still decides. On Postgres the engine's session sets short server-side
  keepalives: `tcp_keepalives_idle` 10 seconds, `tcp_keepalives_interval` 5 seconds,
  `tcp_keepalives_count` 3, and `tcp_user_timeout` 25000 milliseconds. The server then drops a dead
  session in about 25 seconds. It also drops a live engine that loses its network for that long.
  So the engine takes the lock on the same connection it writes through, and it never reconnects.
  A lost session then fails the engine's next write. The saga writes `PENDING` before it sends, so
  the engine stops before it can place another order. A separate lock connection would let the
  engine keep trading without its lock. The refusal reads the holder from `pg_stat_activity`. It prints its pid, client address, and start time. It tells
  the operator to retry in about 30 seconds if that engine is dead, or to run
  `SELECT pg_terminate_backend(<pid>)`. The Postgres DSN must be a direct connection. A pooler in
  transaction mode breaks a session advisory lock. In session mode, the keepalives reach only the
  pooler. Code does not detect a pooler, so `.env.example` states the rule.**)**

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
  engine. The lock catches this mistake when both runs open the same store. It cannot catch a run
  that opens a different store. See Consequences.
  **(Extended in [#440](https://github.com/MarcosACH/tickwright/issues/440):** for the gaps, we
  rejected these options. No override for a new store would leave a lost host with only the venue
  UI. A lease row with a heartbeat needs a new table and a clock to trust. Short keepalives need a
  few `SET` lines. An exit run that waits for the lock would hide a live engine for a minute. A
  flag that breaks the lock makes it too easy to end a live engine's session. Detecting a pooler
  has no reliable signal.**)**

## Consequences

- An exit starts a few seconds later. The operator stops the engine, and the run boots first.
- The lock also refuses two engines that share one store by accident. Before this, that case passed
  the account binding check (ADR-0038). Two engines on one account with separate stores stay
  undetectable.
- The lock does not catch an exit run that opens a different store. The default SQLite path
  `tickwright.db` is relative, and `.env` is read from the current directory. So a run started from
  another directory opens another store and takes another lock. It then runs while the engine still
  trades, and a strategy can reopen what flatten closed.
  **(Resolved in [#440](https://github.com/MarcosACH/tickwright/issues/440):** an exit run refuses
  a store with no account row, unless the operator passes `--new-store`. This catches the new store
  a wrong directory creates. It does not catch an old store left in that directory. On paper the
  gap does no harm, because a different store is a different paper account. See the block under
  the store lock above.**)**
- On Postgres, the advisory lock ends with the database session. If the engine host dies, the
  server can keep a half-open session, and its lock, for a while. An exit run can be refused during
  that time.
  **(Resolved in [#440](https://github.com/MarcosACH/tickwright/issues/440):** without keepalives,
  a Linux server notices a dead client after about 2 hours and 11 minutes. The engine's session now
  sets short server-side keepalives, so the wait drops to about 25 seconds. A refusal names the
  holder and how to end its session. A live engine cut off for 25 seconds also loses its lock. It
  then stops at its next write. These settings were not yet tested against a real server.
  The slice's Postgres test confirms them. See the block under the store lock above.**)**
- On paper, a market order fails when no price is cached. So a paper flatten cannot fill unless
  the feed runs. A replay feed restarts from the top of its file (ADR-0043 §5.1).
- Still open in #408:
  - How an exit run meets the kill switch.
  - Who owns a flatten order and its fills.
  - How far a flatten order may slip.
  - What the operator sees.
  - How to guard an exit run that opens a different store.
  - How long a dead Postgres session may hold the lock.
  - Whether an exit run starts the feed, and at what price a paper flatten fills.

**(Resolved by ADR-0053:** how an exit run meets the kill switch. Flatten trips the kill switch
first and its orders skip it. Cancel all leaves the kill switch alone. Decided in
[#411](https://github.com/MarcosACH/tickwright/issues/411).**)**

**(Resolved by ADR-0054:** who owns a flatten order and its fills. Flatten sends one reduce-only
order per symbol for the venue's position, owned by the reserved id `__operator__`. Its fills split
pro rata over every partition, so each strategy reads flat. Decided in
[#412](https://github.com/MarcosACH/tickwright/issues/412).**)**

**(Resolved by ADR-0059:** whether an exit run starts the feed, and at what price a paper flatten
fills. Flatten starts the feed, and live it also subscribes to every symbol the account holds.
Under replay, the run plays the whole file first, so a paper flatten fills at the last row's price.
Cancel all starts no feed. A market order's price is now also capped by the mark. Decided in
[#438](https://github.com/MarcosACH/tickwright/issues/438).**)**

**(Resolved in [#440](https://github.com/MarcosACH/tickwright/issues/440):** how to guard an exit
run that opens a different store, and how long a dead Postgres session may hold the lock. An exit
run refuses a store no engine has used, unless the operator passes `--new-store`. On Postgres a
dead session frees the lock in about 25 seconds. See the block under the store lock above.**)**
