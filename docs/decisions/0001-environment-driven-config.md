# 0001. Environment-driven configuration, nothing hardcoded

Status: Accepted

## Context

The first working version of this bot had its Discord channel id, allowed user ids, and Claude config dir hardcoded directly in `bridge.py`. That was fine for a single private deployment, but became a blocker the moment the repo needed to be shared or made public: the values would either have to be stripped out of every commit in the file's history, or the repo couldn't be published at all without leaking that deployment's Discord ids.

## Decision

All deployment-specific values (`DISCORD_CHANNEL_ID`, `DISCORD_ALLOWED_USER_IDS`, `CLAUDE_BIN`, `CLAUDE_CONFIG_DIR`, `BRIDGE_WORKDIR`, `BRIDGE_SESSION_FILE`, `BRIDGE_TIMEOUT_SECONDS`, `BRIDGE_LOG_LEVEL`) are read from the environment in `_load_config()`, with `.env` (or `instances/<name>.env` for multi-agent setups) as the actual source, never committed. `bridge.py` itself has nothing to sanitize before being shared.

Required variables fail fast with a specific error message (`_env_int`/`_env_int_set`) rather than falling back to an insecure or nonsensical default — a missing `DISCORD_ALLOWED_USER_IDS`, for instance, must never silently mean "allow everyone."

## Alternatives considered

- **A config file format (YAML/TOML) instead of env vars.** Rejected: env vars map directly onto how this actually gets deployed (systemd `EnvironmentFile=`), need no parsing dependency, and are the same shape whether you're running `bridge.py` directly or under a template unit.
- **Keep values hardcoded, gitignore a "local config" copy of `bridge.py`.** Rejected: fragile (easy to accidentally commit the real file instead of a template), and defeats the point of the file being the single source of truth for the logic.

## Consequences

- `bridge.py` is safe to open-source as-is; only `.env`/`instances/*.env` are gitignored and contain anything real.
- The module is import-safe (see ADR-0002's sibling concern, and `CLAUDE.md`) because config reads are deferred into `_load_config()`, called from `main()` — not evaluated at import time.
- Running a second agent is "write a new `.env`," not "fork the code."
