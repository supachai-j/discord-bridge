# 0004. Rewriting git history before making the repo public

Status: Accepted (one-time action, not a recurring policy)

## Context

The repository existed privately first, and its earliest commits had a real Discord channel id, a real allowed-user id, and a real Discord handle hardcoded in `bridge.py` (before ADR-0001's environment-driven config existed). Once configuration became environment-driven, the *current* file was clean — but `git log`/`git show` on the old commits would still expose those values the moment the repo went public, regardless of what the latest commit looked like.

## Decision

Before flipping the repository to public:

1. Verified the sanitized working tree contained no personal identifiers (`grep` across every tracked file).
2. Created an orphan branch from that clean working tree — a single new root commit with no parent history.
3. Replaced `main` with it and force-pushed, overwriting the old history on the (still-private, at that point) remote.
4. Only then changed the repository's visibility to public.
5. Verified after the fact by fetching the file directly from GitHub's API and re-scanning it, not just trusting the local state.

This was a one-time cleanup for a repo with a single maintainer and no other clones or forks in existence — safe to rewrite. It is not a general policy for this project going forward.

## Alternatives considered

- **`git filter-branch` / `git filter-repo` to scrub specific strings from history.** More surgical, but more error-prone for a small number of early commits — easy to miss a variant of a leaked value. Squashing to one clean commit is simpler to verify exhaustively (`grep` the one resulting tree, not N historical trees).
- **Leave history as-is, rely on the values being "just" a Discord channel/user id (not exploitable credentials).** Rejected: they're not secrets in the credential sense, but they directly link a real name/Discord handle to this deployment, which is exactly the kind of unnecessary personal-information exposure worth avoiding when it costs nothing to avoid.

## Consequences

- `git log` on `main` starts at a single squashed commit (`d20ecaf`) — this is expected and permanent; don't go looking for "missing" history before it.
- **This kind of rewrite is a one-time, pre-public action, not a repeatable practice.** Once a repo is public and has any external clones, stars, or forks, rewriting history to remove a leak is far more disruptive and generally the wrong move — rotate the leaked credential instead and let history show the leak happened (see `SECURITY.md`'s incident guidance and `docs/runbook.md`).
