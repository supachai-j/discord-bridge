# CLAUDE.md

Guidance for any Claude Code session (or other coding agent) working in this repo. This is a single-file Discord bot — read `bridge.py` in full before changing it; it's short enough that partial context is more dangerous than useful here.

## What this is

`bridge.py` forwards messages from one Discord channel to a pluggable backend (`claude -p --resume` by default; see `README.md#backends` and `docs/decisions/0005-pluggable-backends.md`), streams the reply back by editing a placeholder message, and persists whatever opaque state that backend needs to continue next time. One process = one channel = one backend = one ongoing conversation. Full design in `README.md`; day-to-day failure modes and how to respond in `docs/runbook.md`.

## Before touching `bridge.py`

- **Run the tests and lint first, and again before you're done**: `pip install -r requirements-dev.txt && ruff check . && bandit -r bridge.py && pytest -q`. The test suite only covers pure logic (`_env_int`, `_env_int_set`, `is_stale_session_error`, `chunk_text`, session file I/O, the backend factory, `CLIRun`'s argv templating, `OpenAICompatibleRun`'s message/SSE helpers) — nothing that actually spawns a subprocess or makes a network call, so a green test run does **not** mean the bot still works end to end.
- **The module must stay import-safe.** Env parsing, signal registration, and `client.run()` live in `main()` behind `if __name__ == "__main__":` specifically so `import bridge` (what the tests do) doesn't require a live `.env` or try to connect to Discord. Don't move config reads or `signal.signal()` calls back to module scope.
- **Nothing deployment-specific is hardcoded.** No channel ids, user ids, hostnames, or tokens belong in `bridge.py`, `.env.example`, `README.md`, `docs/`, or commit messages — this repo is public. Real values live only in `.env` / `instances/*.env`, both gitignored. If you're pasting in a real deployment's config to explain something, redact it first.
- **This has genuinely deployed history to it.** `git log` on `main` starts at a single squashed commit (`d20ecaf`) — history before that was rewritten because early commits had real Discord ids baked in. Don't assume `git blame`/`git log` predates that commit for anything.

## Verifying a change against the real bot

Unit tests don't exercise Discord, a subprocess, or an HTTP call at all. If a change touches any `BackendRun` subclass (`ClaudeRun`, `CLIRun`, `OpenAICompatibleRun`), `on_message`, signal handling, or anything in `_load_config()`, the only real verification is: deploy it, restart the service, and confirm behavior against the live gateway connection and (ideally) a real Discord message. For a `ClaudeRun`/subprocess change specifically, a quick way to exercise the full pipeline without going through Discord at all: instantiate the class directly (`bridge.ClaudeRun(prompt, state)`, `.start()`, `.done.wait()`) with the real `CLAUDE_CONFIG_DIR`/`WORKDIR` set — this is how a real bug in the backend refactor (state being silently discarded, breaking `--resume` while every test still passed) was actually caught. `docs/runbook.md` has the exact commands and what to look for in the logs. Do **not** claim a fix works based on `pytest` passing alone if it touches one of those paths.

## Deployment model — no CD, on purpose

There is no pipeline that pushes code onto the machine actually running the bot. Deploying is `git pull && sudo systemctl restart discord-bridge@<name>.service` (or `discord-bridge.service` for a single-instance setup), done by a person, on purpose — see `README.md#releasing`. Don't add auto-deploy-on-push without the maintainer explicitly asking for it; a bad deploy landing automatically on something with real shell/tool access is a materially worse failure mode than for a typical web app.

## `CLAUDE_CONFIG_DIR` and multi-identity deployments

`bridge.py` spawns `claude` with `CLAUDE_CONFIG_DIR` set (if configured) so the bot's identity, conversation history, and `permissions.allow`/`.deny` are separate from whoever's running `claude` interactively on the same machine. If you're debugging a specific deployment and it "can't run a command" or "`/permissions` doesn't exist," that's almost always this — the bot runs `claude -p` non-interactively, so a prompt outside `permissions.allow` just fails silently rather than asking anyone. See `README.md#security-model`.

Running more than one agent on one host means more than one `instances/<name>.env`, each with its own `CLAUDE_CONFIG_DIR`, `BRIDGE_SESSION_FILE`, and usually its own bot token — see `README.md#running-multiple-agents`. Don't collapse them into one process; the isolation is the point (see `docs/decisions/` if present, or ask before doing this).

## Backends — `ClaudeRun` carries a stricter bar than the others

`ClaudeRun` is the one backend with a real production deployment behind it. Its `_build_argv`/`_handle_line`/`_finalize` must stay behaviorally identical to what shipped as `StreamRun` before this file supported multiple backends — same argv, same stream-json parsing, same error-precedence order (final-with-is_error beats timeout beats stderr-tail). `CLIRun` and `OpenAICompatibleRun` don't carry that history yet; still verify them for real (see above), but a behavior change there isn't a regression against anything already running.

All three share `BackendRun.__init__` for the `state` handshake: it must start as the *input* state (what to resume from) and only get overwritten with the *new* state on success. Resetting it to `None` unconditionally in `__init__` is exactly the bug that shipped once already — it silently broke `--resume` for every backend while every unit test still passed, because nothing in the test suite exercises a real two-turn conversation. If you touch `BackendRun`, `_WatchedSubprocessRun`, or any subclass's `__init__`, re-run the two-call manual check described above before considering it done.

## Security-sensitive code paths — treat changes here as high-stakes

- The three gates in `on_message` (channel match → allowlist → `busy_lock`) — reordering these has caused a real bug before (`!reset` bypassing the lock; fixed, see `docs/runbook.md`'s fix ledger). Think through the interaction with an in-flight run before changing the order or adding a new early-return.
- `_STALE_SESSION_RE` — keep the bounded `{0,100}?` gap. An earlier unbounded `.*` version could false-match across unrelated stderr and delete a perfectly good session/state.
- Anything in `_shutdown` / the watchdog in `_WatchedSubprocessRun` — these exist specifically to avoid orphaning a backend subprocess or leaving a stale gateway session open. Verify with a real `systemctl restart` and `ps aux | grep bridge.py`, not just by reading the diff.

## Release process

`git tag vX.Y.Z && git push --tags` triggers `.github/workflows/release.yml`, which creates a GitHub Release with auto-generated notes. Don't hand-write a CHANGELOG entry for this — the auto-generated notes from commit messages are the changelog. Write commit messages accordingly (what changed and why, not just "fix bug").
