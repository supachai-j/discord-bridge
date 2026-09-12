# 0002. systemd instantiated unit for running multiple agents

Status: Accepted

## Context

One `bridge.py` process serves exactly one Discord channel with one `claude` session. Running a second agent (different persona, different channel, sometimes a different Discord application entirely) needed a way to run several independent copies of the same code without hand-maintaining N nearly-identical systemd unit files, and without merging them into one process.

## Decision

Use a systemd *instantiated* unit, `discord-bridge@.service`, where `%i` (the part after `@`) names an env file under `instances/%i.env`. One template unit, shared code and venv, one OS process per instance — `systemctl enable --now discord-bridge@myagent.service` reads `instances/myagent.env` automatically. Adding an agent is one new env file plus one `systemctl enable --now`; the template and `bridge.py` never change.

Verified concretely, not just in theory: an existing live deployment was migrated onto the template with zero session loss (`session_id.txt` byte-identical before/after), and a throwaway second instance was run concurrently on the *same* Discord bot token (Discord allows multiple gateway sessions per token, the same mechanism sharding uses) to confirm two instances genuinely don't collide — separate PIDs, separate gateway sessions, separate logs.

## Alternatives considered

- **Separate hand-written unit file per agent.** Works, but every unit is a copy-paste of the same boilerplate (`StartLimitIntervalSec`, `PYTHONUNBUFFERED`, `Restart=on-failure`, ...) that has to be kept in sync by hand across N files. A hardening fix (e.g. the graceful-shutdown work, ADR-adjacent but not its own ADR) would need to be copied into every unit instead of edited once.
- **Single process, route by channel id.** Rejected as higher-risk than either of the above: an unhandled exception in one agent's message handler would be running in the same event loop as every other agent, so one agent's bug can take all of them down together — the opposite of the isolation multiple agents are usually wanted for in the first place. It also can't represent two distinct Discord bot *identities* (avatar/name) without running multiple `discord.Client` instances in one process anyway, which reintroduces most of systemd's job by hand.

## Consequences

- A crashed or hung agent can't take another one down — they're separate OS processes.
- `journalctl -u discord-bridge@<name>` gives per-agent logs for free; no manual log prefixing.
- Restarting one agent to pick up a code change restarts only that agent's Discord connection, not everyone's.
- Slightly more baseline memory than a single-process design (~30MB per instance, measured) — acceptable for the isolation gained; would need revisiting only at a scale (dozens of agents on one small host) this project isn't at.
