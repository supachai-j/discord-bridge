# Contributing

A guide to actually working on this codebase — day-to-day workflow, conventions, and the reasoning behind them. If you're an AI coding agent, read [CLAUDE.md](CLAUDE.md) first; it has constraints specific to agents editing this file. This document is the human-facing counterpart — the *how*, where `CLAUDE.md` is mostly the *don't-do-this-again*.

## Getting started

```bash
git clone https://github.com/supachai-j/discord-bridge.git
cd discord-bridge
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
```

That installs `discord.py` plus `pytest`, `ruff`, and `bandit`. You don't need a Discord bot token or a running `claude` to do most development — see [Verifying a change](#verifying-a-change) for when you do.

## Everyday workflow

```bash
ruff check .        # style + real bugs (pyflakes/pycodestyle/import order)
bandit -r bridge.py # security-focused scan, separate from ruff on purpose
pytest -q           # unit tests — pure logic only, see below
```

All three run in CI (`.github/workflows/ci.yml`) on every push and PR to `main`. Run them locally before pushing; CI catching it isn't a workflow, it's a backstop.

### What the tests do and don't cover

`tests/test_bridge.py` covers pure functions with no I/O or network dependency: config parsing (`_env_int`, `_env_int_set`), stale-session detection (`is_stale_session_error`), text chunking (`chunk_text`), and the session-file round trip. It does **not** exercise Discord or `claude` — there's no mock gateway connection or fake subprocess. A fully green `pytest` run tells you the logic you touched is correct in isolation; it does not tell you the bot still works.

### Verifying a change

If your change touches `StreamRun`, `on_message`, signal handling, or `_load_config()`, verify it against a real deployment before considering it done:

```bash
sudo systemctl restart discord-bridge@<name>.service
journalctl -u discord-bridge@<name>.service -f
# then send a real message in the configured Discord channel
```

Check specifically for: the bot logs in and shows "watching channel ..." at `INFO` level, the restart is fast (well under a second — `time sudo systemctl restart ...`) with no leftover `claude -p` process (`ps aux | grep bridge.py`), and `session_id.txt`'s content is unchanged if your change wasn't supposed to touch session handling. `docs/runbook.md` has the full command reference and a table of what specific symptoms mean.

## Code conventions

- **No hardcoded deployment values.** Channel ids, user ids, hostnames, tokens — none of it belongs in `bridge.py`, docs, or commit messages. See [ADR-0001](docs/decisions/0001-environment-driven-config.md). If you're pasting a real config to illustrate a bug, redact it.
- **Fail fast, not silently permissive.** `_env_int`/`_env_int_set` raise a clear `SystemExit` on a missing or malformed required variable rather than falling back to a default that could be wrong in a dangerous direction (e.g. an empty allowlist must never mean "allow everyone"). New config should follow the same pattern.
- **`bridge.py` must stay import-safe.** Nothing at module scope should read the environment, register a signal handler, or touch the network — that all belongs inside `_load_config()` / `main()`, called only from `if __name__ == "__main__":`. This is what lets `tests/` `import bridge` without a live `.env`.
- **Accepted security-scanner findings get a `# nosec` comment with a reason, not a config-level suppression.** If `bandit` flags something you've reviewed and accepted, put `# nosec BXXX` on the exact flagged line, with the *why* in a comment on the line(s) above it (see the three existing examples in `bridge.py`). Keep the `# nosec BXXX` token alone on its line — bandit tries to parse trailing words after it as more test ids and spams warnings otherwise.
- **Comments explain *why*, not *what*.** The codebase already does this consistently (e.g. the note above `_STALE_SESSION_RE` about why the match is bounded) — match that style rather than restating what the next line obviously does.

## Commit messages

Release notes are auto-generated from commit messages and PR titles (`.github/workflows/release.yml`, triggered by a tag) — there's no hand-maintained `CHANGELOG.md`. Write commit messages as if they'll be read on a release page, because they will be:

- First line: what changed, imperative mood ("Fix the thing", not "Fixed" or "Fixes")
- Body: why — what broke without it, what the alternative would have been, anything a future reader would otherwise have to reconstruct from the diff alone

## Architecture Decision Records

For a decision with real alternatives (not just "the obvious fix"), or a deliberate non-obvious constraint, write a short ADR under `docs/decisions/` — see [`docs/decisions/README.md`](docs/decisions/README.md) for the format and when it's warranted. Four exist already as examples of the intended length and tone.

## Security-sensitive changes

Read [`CLAUDE.md`'s "high-stakes" section](CLAUDE.md#security-sensitive-code-paths--treat-changes-here-as-high-stakes) before touching the allowlist/`busy_lock` ordering in `on_message`, `_STALE_SESSION_RE`, or the shutdown/watchdog logic — each has caused a real bug before. If you find an actual vulnerability (not a deployment misconfiguration), report it per [`SECURITY.md`](SECURITY.md) rather than opening a public PR that demonstrates it.

## Releasing

```bash
git tag v0.2.0
git push --tags
```

This triggers a GitHub Release with auto-generated notes — that's the entire release process. **There is no CD** (see [ADR-0003](docs/decisions/0003-no-cd-manual-deploy.md)); getting a release onto a running instance is always a manual `git pull && sudo systemctl restart ...`, deliberately.

Rough semver guidance for this project specifically: **major** = a required env var is renamed/removed, or a documented behavior changes incompatibly; **minor** = a new optional env var or feature, backward compatible; **patch** = a bug fix with no config or behavior surface change.

## CI/CD at a glance

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | push/PR to `main` | `ruff`, `bandit`, `pytest` |
| `release.yml` | tag `vX.Y.Z` | Creates a GitHub Release with auto-generated notes |
| Dependabot (`dependabot.yml`) | weekly | Opens PRs bumping pip and GitHub Actions dependencies |
| GitHub secret scanning + push protection | on every push | Enabled at the repo level (not a file in this tree) — blocks a push containing a recognizable secret pattern |

## Adding a feature that needs a new agent/instance

That's a deployment concern, not a code change — see [README.md#running-multiple-agents](README.md#running-multiple-agents). Don't add per-agent branching logic to `bridge.py` itself; the whole design is that one env file is one agent (see [ADR-0002](docs/decisions/0002-systemd-template-for-multi-agent.md)).
