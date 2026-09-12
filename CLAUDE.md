# CLAUDE.md

Guidance for any Claude Code session (or other coding agent) working in this repo. This is a single-file Discord bot — read `bridge.py` in full before changing it; it's short enough that partial context is more dangerous than useful here.

## What this is

`bridge.py` forwards messages from one Discord channel to `claude -p --resume <session-id>`, streams the reply back by editing a placeholder message, and exits. One process = one channel = one ongoing `claude` conversation. Full design in `README.md`; day-to-day failure modes and how to respond in `docs/runbook.md`.

## Before touching `bridge.py`

- **Run the tests and lint first, and again before you're done**: `pip install -r requirements-dev.txt && ruff check . && pytest -q`. The test suite only covers pure logic (`_env_int`, `_env_int_set`, `is_stale_session_error`, `chunk_text`, session file I/O) — nothing that touches Discord or spawns `claude`, so a green test run does **not** mean the bot still works end to end.
- **The module must stay import-safe.** Env parsing, signal registration, and `client.run()` live in `main()` behind `if __name__ == "__main__":` specifically so `import bridge` (what the tests do) doesn't require a live `.env` or try to connect to Discord. Don't move config reads or `signal.signal()` calls back to module scope.
- **Nothing deployment-specific is hardcoded.** No channel ids, user ids, hostnames, or tokens belong in `bridge.py`, `.env.example`, `README.md`, `docs/`, or commit messages — this repo is public. Real values live only in `.env` / `instances/*.env`, both gitignored. If you're pasting in a real deployment's config to explain something, redact it first.
- **This has genuinely deployed history to it.** `git log` on `main` starts at a single squashed commit (`d20ecaf`) — history before that was rewritten because early commits had real Discord ids baked in. Don't assume `git blame`/`git log` predates that commit for anything.

## Verifying a change against the real bot

Unit tests don't exercise Discord or `claude` at all. If a change touches `StreamRun`, `on_message`, signal handling, or anything in `_load_config()`, the only real verification is: deploy it, restart the service, and confirm behavior against the live gateway connection and (ideally) a real Discord message. `docs/runbook.md` has the exact commands and what to look for in the logs. Do **not** claim a fix works based on `pytest` passing alone if it touches one of those paths.

## Deployment model — no CD, on purpose

There is no pipeline that pushes code onto the machine actually running the bot. Deploying is `git pull && sudo systemctl restart discord-bridge@<name>.service` (or `discord-bridge.service` for a single-instance setup), done by a person, on purpose — see `README.md#releasing`. Don't add auto-deploy-on-push without the maintainer explicitly asking for it; a bad deploy landing automatically on something with real shell/tool access is a materially worse failure mode than for a typical web app.

## `CLAUDE_CONFIG_DIR` and multi-identity deployments

`bridge.py` spawns `claude` with `CLAUDE_CONFIG_DIR` set (if configured) so the bot's identity, conversation history, and `permissions.allow`/`.deny` are separate from whoever's running `claude` interactively on the same machine. If you're debugging a specific deployment and it "can't run a command" or "`/permissions` doesn't exist," that's almost always this — the bot runs `claude -p` non-interactively, so a prompt outside `permissions.allow` just fails silently rather than asking anyone. See `README.md#security-model`.

Running more than one agent on one host means more than one `instances/<name>.env`, each with its own `CLAUDE_CONFIG_DIR`, `BRIDGE_SESSION_FILE`, and usually its own bot token — see `README.md#running-multiple-agents`. Don't collapse them into one process; the isolation is the point (see `docs/decisions/` if present, or ask before doing this).

## Security-sensitive code paths — treat changes here as high-stakes

- The three gates in `on_message` (channel match → allowlist → `busy_lock`) — reordering these has caused a real bug before (`!reset` bypassing the lock; fixed, see `docs/runbook.md`'s fix ledger). Think through the interaction with an in-flight `StreamRun` before changing the order or adding a new early-return.
- `_STALE_SESSION_RE` — keep the bounded `{0,100}?` gap. An earlier unbounded `.*` version could false-match across unrelated stderr and delete a perfectly good session id.
- Anything in `_shutdown` / the watchdog — these exist specifically to avoid orphaning a `claude` subprocess or leaving a stale gateway session open. Verify with a real `systemctl restart` and `ps aux | grep bridge.py`, not just by reading the diff.

## Release process

`git tag vX.Y.Z && git push --tags` triggers `.github/workflows/release.yml`, which creates a GitHub Release with auto-generated notes. Don't hand-write a CHANGELOG entry for this — the auto-generated notes from commit messages are the changelog. Write commit messages accordingly (what changed and why, not just "fix bug").
