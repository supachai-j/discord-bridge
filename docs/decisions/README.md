# Architecture Decision Records

Short records of decisions that would otherwise only live in a chat transcript or someone's memory. Not every change needs one — write one when a decision had real alternatives, and the reasoning for picking one would matter to someone reading the code later (including future-you).

## When to write one

- Choosing between two workable architectures (e.g. ADR-0002)
- A deliberate non-obvious constraint (e.g. ADR-0003: no CD, on purpose)
- A one-time, hard-to-reverse operational action worth explaining (e.g. ADR-0004: rewriting public git history)

Bug fixes, refactors, and additive features generally don't need one — the commit message and, for anything security-relevant, `docs/runbook.md`'s fix ledger, are enough.

## Format

Each ADR is `NNNN-short-title.md`, numbered sequentially, never renumbered or deleted (mark superseded ones as such rather than removing them):

```markdown
# NNNN. Title

Status: Accepted | Superseded by NNNN | Rejected

## Context
What problem or question forced a decision?

## Decision
What was chosen.

## Alternatives considered
What else was on the table, and why it lost.

## Consequences
What this makes easier, harder, or simply true from now on.
```

## Index

| ADR | Title |
|---|---|
| [0001](0001-environment-driven-config.md) | Environment-driven configuration, nothing hardcoded |
| [0002](0002-systemd-template-for-multi-agent.md) | systemd instantiated unit for running multiple agents |
| [0003](0003-no-cd-manual-deploy.md) | No CD — deployment stays a manual, deliberate step |
| [0004](0004-git-history-rewrite-before-public.md) | Rewriting git history before making the repo public |
