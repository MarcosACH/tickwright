# User strategies load by import path

Tickwright promises that a user brings config and a strategy, and the engine does the rest. Until
now that was not true. The engine could build only the two reference strategies named in
`StrategyConfig.kind`. A user with their own strategy had to edit `app/config.py` and
`app/build.py`, which means forking the engine. This ADR lets a strategy live in the user's own
code and be named in config. Decided in a `/grill-with-docs` session on 2026-10-21, while planning
the testnet soak. The soak was the first time someone brought a real strategy.

## Decision

- **Config names each strategy by import path.** The form is `module:Class`, for example
  `my_strats.momentum:Momentum`. The engine imports it at boot. The user installs Tickwright as a
  dependency and keeps the strategy in their own repo.
- **One loading path for every strategy.** The two reference strategies load the same way, for
  example `tickwright.strategies.single_shot:SingleShotMarketStrategy`. `StrategyConfig.kind` and the
  strategy `match` in `app/build.py` go away. So every test and verify recipe that runs a reference
  strategy also tests the user path.
- **The engine owns the run.** The user writes a strategy class and config, never a start script.
  The CLI keeps the signals, the exit codes, the store lock and the exit runs. `__main__.py` stays
  the only place that reads ambient config (issue #71).
- **One strategy entry has four fields.** They are `strategy` (the import path), `strategy_id`,
  `symbols` (a list) and `settings`. The engine already registers a set of symbols per strategy.
  Only the config limited it to one.
- **The class declares its settings.** A strategy class has a `Settings` attribute that names a
  Pydantic model. The engine checks the entry's `settings` against it at boot. A strategy with no
  settings declares an empty model.
- **Unknown settings fields are refused.** The engine refuses them even when the user's model
  allows extra fields. A typo must never be ignored without a word.
- **The constructor receives exactly three keyword arguments.** They are `emitter`, `portfolio` and
  `settings`, always all three. A strategy that reads no portfolio still accepts it.
- **A bad entry refuses at boot.** These all refuse before the store or the venue is touched:
  - a path that is not `module:Class`, or that does not import
  - a class with no `Settings`
  - a constructor that cannot take the three keyword arguments
  - a class that lacks a method of the `Strategy` Protocol
  - settings that do not validate

  A wrong constructor or a missing method would otherwise fail after the store exists. The CLI
  would exit 1, and a supervisor would restart it forever. Async methods are not checked, because
  a valid decorated method can look sync.
- **Two more refusals come after construction.** The store exists by then, but the engine has not
  run, so no lock is taken and nothing is written. Both still exit 2:
  - a constructor that raises
  - a `strategy_id` that is missing or differs from the entry's. The emitter and portfolio carry
    the entry's id, but the host keys on the strategy's. A mismatch would hide its fills from it,
    and a restart would resume its seq from the wrong high-water (#442).
- **The import path is trusted like code.** Anyone who can set the engine's environment can already
  run code as the engine. So naming a module in config adds no new attack surface.
- **Strategies only.** Feeds, exchanges, buses, stores and guards keep their `match` in
  `app/build.py` (ADR-0032).

## Considered options

- **The user writes the start script.** The user builds a node, adds strategies, and calls
  `run()`. Rejected because every user would have to rebuild the CLI's
  safety, and a user script could read ambient config by mistake.
- **Pass only the arguments the constructor names.** This would keep ADR-0041 §7 as written.
  Rejected because what a strategy receives would depend on its parameter names, which is hidden
  behavior.
- **One context object, such as `ctx.emitter`.** It would let us add a service later without
  changing any user's signature. Deferred. We are on `0.x`, so adding a keyword is allowed. Decide
  before `1.0.0`.
- **Config names the settings class too.** Each entry would hold a second import path. Rejected
  because a strategy and its settings class could then be mismatched.
- **Read the settings class from the `settings` type hint.** Rejected as hidden behavior, like the
  second option above.
- **Import paths for every seam.** Rejected for now. An exchange brings signing keys, account checks
  and recovery rules. Loading one safely is a different design problem.
- **Keep `kind` beside the import path.** Rejected because two loading paths cost more to read, and
  the new path would get few tests.

## Consequences

- The `TICKWRIGHT_STRATEGIES` format changes. On `0.x` that is a MINOR bump.
- ADR-0032 rejected importable config for wiring. That rejection now excludes strategies.
- ADR-0041 §7 said a strategy that reads no portfolio omits the argument. That no longer holds.
- `docs/extending.md` must stop saying a third feed, exchange, bus or store can live out of tree.
  Today it cannot.
- A strategy still cannot wake itself on a timer. That is a separate issue.
