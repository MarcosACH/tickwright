<!--
One PR per change — no mixed-concern PRs. See CONTRIBUTING.md.
The PR merge is what closes the issue; never close issues by hand.
-->

## Summary

<!-- What changed and why. Keep it focused on the single logical change. -->

Closes #

## Type of change

- [ ] Feature slice (vertical: crosses feed → strategy → exchange → engine)
- [ ] Bug fix
- [ ] Docs / ADR / CONTEXT
- [ ] Chore / tooling / CI

## Checklist

- [ ] **Vertical slice** — this crosses every relevant layer in one PR (no horizontal layer in isolation).
- [ ] **TDD** — a failing test was written first, then made to pass (red → green → refactor).
- [ ] Tests exercise the **public interface**; mocks only at process boundaries (HTTP/WS, Kafka, clock, randomness).
- [ ] `uv run ruff format --check .` · `uv run ruff check .` · `uv run mypy .` all pass locally.
- [ ] Coverage stays **≥ 90% on the core**.
- [ ] Load-bearing decisions captured as an **ADR**; new/changed vocabulary reflected in **CONTEXT.md**.
- [ ] One logical change; commit messages follow the Conventional Commits convention.
