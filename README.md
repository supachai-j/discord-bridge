# discord-bridge

A small Discord bot that pipes one channel into a resident [Claude Code](https://claude.com/claude-code) session. Every message from an allowed user spawns `claude -p --resume <session-id>`, streams the reply back into Discord by editing a placeholder message, and exits — no long-running Claude process, just a session id persisted between messages.

```
Discord channel --message--> bridge.py --spawn `claude -p --resume`--> claude subprocess
      ^                          |  (author must be in the allowlist)         |
      '--------- edit reply, every 1.5s while streaming ----------------------'
```

## Features

- **Streaming replies** — the placeholder message is edited live as Claude's output arrives, not dumped all at once at the end.
- **Persistent conversation** — messages in the channel are turns in one ongoing `claude` session (`--resume`), not one-shot prompts.
- **Hard allowlist** — wrong channel or wrong Discord user id, and the message never reaches Claude; everyone else just gets a 🚫 reaction.
- **`!reset` / `!new`** — clear the session and start over, from Discord, without SSH.
- **Self-healing** — a stale/deleted session id is detected from the error text and cleared automatically instead of wedging every future message.
- **Watchdog timeout** — a hung `claude` subprocess is killed after `BRIDGE_TIMEOUT_SECONDS` instead of hanging forever.
- **Clean shutdown** — `SIGTERM`/`SIGINT` (what `systemctl stop`/`restart` send) kill any in-flight `claude` subprocess instead of orphaning it; discord.py's own `client.run()` doesn't do this on its own.
- **No permission bypass** — never runs with `--dangerously-skip-permissions` or a loosened `--permission-mode`; anything outside your `permissions.allow` fails closed, because a non-interactive `-p` run has no prompt surface for anyone to answer.

## Requirements

- Python 3.10+
- A Discord application + bot token with the **Message Content** privileged intent enabled, invited to your server with permission to view/send/manage messages in one channel
- [`claude`](https://claude.com/claude-code) installed and already authenticated once, interactively, under whichever `CLAUDE_CONFIG_DIR` you point this at (a non-interactive `-p` run has nowhere to show a login prompt)

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
| `CLAUDE_BIN` | no | `claude` (from `PATH`) | path to the `claude` CLI |
| `CLAUDE_CONFIG_DIR` | no | claude's own default (`~/.claude`) | run under a dedicated identity/history/`permissions.deny` — strongly recommended if you also use `claude` interactively, see [Security model](#security-model) |
| `BRIDGE_WORKDIR` | no | `~` if unset — `.env.example` ships `~/workspace` | cwd for the `claude` subprocess — keep this off `$HOME` |
| `BRIDGE_SESSION_FILE` | no | `~/discord-bridge/session_id.txt` | where the resumable session id is persisted |
| `BRIDGE_TIMEOUT_SECONDS` | no | `1200` | kill a hung `claude` subprocess after this many seconds |

## Security model

Three gates, outside-in:

1. **Channel** — messages outside `DISCORD_CHANNEL_ID` are ignored.
2. **User** — the author must be in `DISCORD_ALLOWED_USER_IDS`, or the message never reaches Claude at all (just a 🚫 reaction).
3. **Tool permissions** — whatever `permissions.allow` / `permissions.deny` you've set in the target `CLAUDE_CONFIG_DIR`'s `settings.json` applies as normal. Since the bridge never passes `--dangerously-skip-permissions` or a permissive `--permission-mode`, anything not explicitly allowed simply fails — there's no prompt surface in Discord for anyone to click "yes" on.

If you're exposing this to anyone other than yourself, or the allowed user's machine isn't fully trusted, add explicit `Read()` deny rules for `~/.ssh`, your bot token file, and any credentials files, regardless of `BRIDGE_WORKDIR` — `Read()` rules match by path, not by cwd.

Leaving `CLAUDE_CONFIG_DIR` unset means the bot runs under the *same* `permissions.allow`/`.deny` as your own interactive `claude` sessions on that machine — typically much broader than a bot needs. Set it to a dedicated config dir so a Discord message can't exercise permissions you only meant to grant yourself at a terminal.

## Known limitations

- One conversation for the whole channel — `BRIDGE_SESSION_FILE` holds a single session id, not one per Discord user.
- One channel per bot process — see [Running multiple agents](#running-multiple-agents) for running several side by side.
- Messages are not queued — a second message while one is still running gets a "busy" reply, not a wait-in-line.
- No test suite yet; changes are verified by restarting the service and sending a real message.

## License

MIT — see [LICENSE](LICENSE).
