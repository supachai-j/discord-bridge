# discord-bridge

[![CI](https://github.com/supachai-j/discord-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/supachai-j/discord-bridge/actions/workflows/ci.yml)

A small Discord bot that pipes one channel into a resident AI coding agent — [Claude Code](https://claude.com/claude-code) by default, or another agentic CLI (Codex, Gemini CLI) or a plain chat API (grok, GLM, a local model) — see [Backends](#backends). Every message from an allowed user is sent to the backend, streams the reply back into Discord by editing a placeholder message, and persists whatever state that backend needs to continue the conversation next time.

📖 Full docs site: **[supachai-j.github.io/discord-bridge](https://supachai-j.github.io/discord-bridge/)** · day-to-day operations: [docs/runbook.md](docs/runbook.md) · reporting a vulnerability: [SECURITY.md](SECURITY.md) · working on this codebase: [CONTRIBUTING.md](CONTRIBUTING.md)

```
Discord channel --message--> bridge.py --spawn/call a backend--> claude / another CLI / a chat API
      ^                          |  (author must be in the allowlist)              |
      '--------- edit reply, every 1.5s while streaming --------------------------'
```

## Features

- **Pluggable backends** — Claude Code by default; another agentic CLI (Codex, Gemini CLI) or any OpenAI-compatible chat API (grok, GLM, local models) via one env var. See [Backends](#backends).
- **Streaming replies** — the placeholder message is edited live as output arrives, not dumped all at once at the end.
- **Persistent conversation** — messages in the channel are turns in one ongoing conversation, not one-shot prompts (backend-dependent — see [Backends](#backends) for what "persistent" means for a stateless chat API).
- **Hard allowlist** — wrong channel or wrong Discord user id, and the message never reaches the backend; everyone else just gets a 🚫 reaction.
- **`!reset` / `!new`** — clear the conversation and start over, from Discord, without SSH.
- **Self-healing** — a stale/invalid session is detected from the error text and cleared automatically instead of wedging every future message.
- **Watchdog timeout** — a hung backend subprocess is killed after `BRIDGE_TIMEOUT_SECONDS` instead of hanging forever (the HTTP-based backend gets the same bound natively, via its own request timeout).
- **Clean shutdown** — `SIGTERM`/`SIGINT` (what `systemctl stop`/`restart` send) kill any in-flight backend subprocess instead of orphaning it; discord.py's own `client.run()` doesn't do this on its own.
- **No permission bypass** (Claude/CLI backends) — never runs with `--dangerously-skip-permissions` or a loosened `--permission-mode`; anything outside your `permissions.allow` fails closed, because a non-interactive run has no prompt surface for anyone to answer.

## Requirements

- Python 3.10+
- A Discord application + bot token with the **Message Content** privileged intent enabled, invited to your server with permission to view/send/manage messages in one channel
- Whatever the chosen [backend](#backends) needs: `claude` installed and authenticated (default backend), another agentic CLI, or an API key for a chat endpoint

## Backends

One instance uses exactly one backend, chosen with `BACKEND` (see [Configuration](#configuration) and [`.env.example`](.env.example) for the full per-backend variable list). Running more than one model at once is running more than one instance — see [Running multiple agents](#running-multiple-agents); there's no per-message model switching.

| `BACKEND` | What it runs | Tool access (files/shell) | Persistence |
|---|---|---|---|
| `claude` (default) | [`claude`](https://claude.com/claude-code) `-p --resume` | Yes, via `permissions.allow` | Server-side resumable session |
| `cli` | Another agentic CLI you configure (Codex, Gemini CLI, ...) | Yes, whatever that CLI grants | Whatever that CLI's own resume flag supports — some (Gemini CLI's headless mode, currently) have none |
| `openai_compatible` | Any `POST {base_url}/chat/completions` endpoint — grok, GLM, a local model via Ollama/vLLM/llama.cpp, etc. | **No** — chat only | A local message-history transcript this bridge maintains, truncated to `OPENAI_COMPATIBLE_MAX_HISTORY` |

`claude` requires being authenticated once, interactively, under whichever `CLAUDE_CONFIG_DIR` you point this at first (a non-interactive `-p` run has nowhere to show a login prompt). `cli` needs the chosen CLI installed and its actual current flags confirmed — the codex/Gemini CLI examples in `.env.example` are researched but **not verified end-to-end** here; check `--help` on your own install before trusting them (see [ADR-0005](docs/decisions/0005-pluggable-backends.md)). `openai_compatible` needs only an API key file and a base URL — no CLI at all, which makes it the quickest way to try a local model.

## Setup

```bash
git clone https://github.com/supachai-j/discord-bridge.git
cd discord-bridge

python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
$EDITOR .env                 # fill in DISCORD_CHANNEL_ID, DISCORD_ALLOWED_USER_IDS, etc.

install -m 600 /dev/stdin ~/.discord_bot_token <<< "<your bot token>"

venv/bin/python bridge.py    # run it directly to test before wiring up systemd
```

### Run as a service

```ini
# /etc/systemd/system/discord-bridge.service
[Unit]
Description=Discord bridge for Claude Code
After=network-online.target
Wants=network-online.target
# Give up after 5 restarts in 5 minutes instead of hammering Discord's
# login endpoint forever on something that won't self-resolve, like a
# revoked token.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/discord-bridge
EnvironmentFile=/home/youruser/discord-bridge/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/youruser/discord-bridge/venv/bin/python /home/youruser/discord-bridge/bridge.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-bridge.service
journalctl -u discord-bridge.service -f
```

### Running multiple agents

One `bridge.py` process serves one channel with one session (see [Known limitations](#known-limitations)). To run several agents side by side — different personas, different channels, even different Discord bot applications — use a systemd *instantiated* unit instead of the single `discord-bridge.service` above: one template, one process per instance, full isolation (a hung or crashed agent can't take another down), no per-agent copy of `bridge.py` or its `venv`.

```ini
# /etc/systemd/system/discord-bridge@.service
[Unit]
Description=Discord bridge instance "%i"
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/discord-bridge
EnvironmentFile=/home/youruser/discord-bridge/instances/%i.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/youruser/discord-bridge/venv/bin/python /home/youruser/discord-bridge/bridge.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
mkdir -p instances
cp instances/instances.example.env instances/myagent.env
$EDITOR instances/myagent.env        # give it its own channel, allowlist,
                                      # BRIDGE_SESSION_FILE and (usually)
                                      # CLAUDE_CONFIG_DIR

sudo systemctl daemon-reload
sudo systemctl enable --now discord-bridge@myagent.service
journalctl -u discord-bridge@myagent.service -f
```

Add another agent by repeating the last three lines with a new `instances/<name>.env` — the template and the code are shared, nothing else to duplicate. `%i` is the part after the `@`, so `discord-bridge@myagent.service` reads `instances/myagent.env`.

Two instances can even share one Discord bot token (each just opens its own gateway session, the same way shards do) if you want two channels on one bot identity — just make sure `BRIDGE_SESSION_FILE` differs between them so their conversations don't overwrite each other.

## Configuration

Everything is read from the environment — see [`.env.example`](.env.example) for the full list with comments. Nothing deployment-specific is hardcoded in `bridge.py`, so the file itself never needs editing per-install.

| Variable | Required | Default | What it controls |
|---|---|---|---|
| `DISCORD_BOT_TOKEN_FILE` | no | `~/.discord_bot_token` | path to the bot token (chmod 600 it) |
| `DISCORD_CHANNEL_ID` | **yes** | — | the one channel the bot listens in |
| `DISCORD_ALLOWED_USER_IDS` | **yes** | — | comma-separated Discord user ids allowed to command it |
| `BACKEND` | no | `claude` | `claude` / `cli` / `openai_compatible` — see [Backends](#backends) |
| `CLAUDE_BIN` | no | `claude` (from `PATH`) | path to the `claude` CLI — **use a full path under systemd**, its `PATH` usually excludes `~/.local/bin` (`BACKEND=claude`) |
| `CLAUDE_MODEL`, `CLAUDE_FALLBACK_MODEL` | no | claude's own default | which model answers, and an automatic fallback if it's overloaded — same values as `claude --model` (`BACKEND=claude`) |
| `CLAUDE_CONFIG_DIR` | no | claude's own default (`~/.claude`) | run under a dedicated identity/history/`permissions.deny` — strongly recommended if you also use `claude` interactively, see [Security model](#security-model) (`BACKEND=claude`) |
| `BRIDGE_WORKDIR` | no | `~` if unset — `.env.example` ships `~/workspace` | cwd for a subprocess-based backend — keep this off `$HOME` |
| `BRIDGE_SESSION_FILE` | no | `~/discord-bridge/session_id.txt` | where the backend's opaque state is persisted |
| `BRIDGE_TIMEOUT_SECONDS` | no | `1200` | kill a hung backend after this many seconds |
| `CLI_BIN`, `CLI_ARGS_NEW`, `CLI_ARGS_RESUME` | required for `cli` | — | the other agentic CLI's binary and argv templates — see `.env.example` |
| `OPENAI_COMPATIBLE_BASE_URL`, `_API_KEY_FILE`, `_MODEL` | required for `openai_compatible` | — | endpoint, key file, and model name — see `.env.example` |
| `OPENAI_COMPATIBLE_SYSTEM_PROMPT`, `_MAX_HISTORY` | no | unset, `40` | optional system prompt; how many prior messages to keep (`openai_compatible`) |

## Security model

Three gates, outside-in:

1. **Channel** — messages outside `DISCORD_CHANNEL_ID` are ignored.
2. **User** — the author must be in `DISCORD_ALLOWED_USER_IDS`, or the message never reaches Claude at all (just a 🚫 reaction).
3. **Tool permissions** — whatever `permissions.allow` / `permissions.deny` you've set in the target `CLAUDE_CONFIG_DIR`'s `settings.json` applies as normal. Since the bridge never passes `--dangerously-skip-permissions` or a permissive `--permission-mode`, anything not explicitly allowed simply fails — there's no prompt surface in Discord for anyone to click "yes" on.

If you're exposing this to anyone other than yourself, or the allowed user's machine isn't fully trusted, add explicit `Read()` deny rules for `~/.ssh`, your bot token file, and any credentials files, regardless of `BRIDGE_WORKDIR` — `Read()` rules match by path, not by cwd.

Leaving `CLAUDE_CONFIG_DIR` unset means the bot runs under the *same* `permissions.allow`/`.deny` as your own interactive `claude` sessions on that machine — typically much broader than a bot needs. Set it to a dedicated config dir so a Discord message can't exercise permissions you only meant to grant yourself at a terminal.

Gate 3 is specific to `claude`/`cli` backends. `openai_compatible` has no file/shell tool access at all — gates 1 and 2 (channel + allowlist) are the whole story for it, and the API key file is the only credential worth protecting.

## Known limitations

- One conversation for the whole channel — `BRIDGE_SESSION_FILE` holds a single session id, not one per Discord user.
- One channel per bot process — see [Running multiple agents](#running-multiple-agents) for running several side by side.
- Messages are not queued — a second message while one is still running gets a "busy" reply, not a wait-in-line.
- Tests (`tests/`) cover the pure logic — config parsing, stale-session detection, chunking — not the Discord/`claude` integration itself; that's still verified by restarting the service and sending a real message.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

CI (`.github/workflows/ci.yml`) runs both on every push and pull request to `main`.

## Releasing

Tag a commit with semver and push the tag:

```bash
git tag v0.2.0
git push --tags
```

`.github/workflows/release.yml` turns that into a GitHub Release with auto-generated notes from the commits since the last tag. That's the entire release process — **there is no CD**. Getting a release onto a running instance is a manual, deliberate step:

```bash
git pull
sudo systemctl restart discord-bridge@<name>.service   # or discord-bridge.service
```

This is intentional for a bot with real permissions to a live `claude` session: a bad deploy should require a human to have typed `git pull`, not land automatically because CI turned green.

## License

MIT — see [LICENSE](LICENSE).
