# issue-900: leverage bound is checked on both exchange paths

base: main | worktree: -
seams under test: `Exchange.start()` on both adapters, and `check_leverage` in `domain`

| # | done | behavior | files | sha |
| - | ---- | -------- | ----- | --- |
| 1 | [x] | refuses a leverage below 1 | src/domain/leverage.py, tests/domain/test_leverage.py | a1b2c3d |
| 2 | [ ] | refuses a leverage above the instrument cap, naming every offending symbol | src/domain/leverage.py, tests/domain/test_leverage.py | |
| 3 | [ ] | paper `start()` validates and never writes | src/adapters/paper/exchange.py | |
| 4 | [ ] | hyperliquid `start()` runs the same check behind the account-mode gate | src/venues/hyperliquid/exchange.py | |
