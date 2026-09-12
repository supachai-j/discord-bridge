# Security Policy

## Supported versions

This project doesn't maintain parallel release branches — only the latest tag on `main` gets fixes. If you're running an older release, update to the latest before reporting; the fix may already be there.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Use GitHub's private reporting instead:

**[Report a vulnerability](https://github.com/supachai-j/discord-bridge/security/advisories/new)** (Security tab → "Report a vulnerability")

Include what you found, how to reproduce it, and what you think the impact is. You should get a response within a few days.

## Threat model

This project's job is to let a Discord message trigger a `claude` subprocess with real tool access on the host it runs on. That's a deliberately powerful capability, so the things worth reporting are different from a typical web app's:

- **Anything that lets a message from outside `DISCORD_ALLOWED_USER_IDS` reach `claude`.** This is the primary control (see [Security model](README.md#security-model)) — a bypass here is the highest-severity class of bug in this repo.
- **Anything that lets an allowed user's message read or exfiltrate files it shouldn't**, beyond whatever `permissions.allow`/`.deny` the operator configured — e.g. a path-traversal-shaped bug in `bridge.py` itself, not "the operator forgot to set `CLAUDE_CONFIG_DIR`" (that's a deployment mistake, covered in the docs, not a vulnerability in the code).
- **Anything that lets the bot's own process be crashed, wedged, or made to leak the bot token / session id** to the Discord channel or elsewhere.
- Prompt-injection-shaped concerns (a message crafted to make `claude` misbehave) are **out of scope for this repo** in the sense that they're `claude`'s own alignment/safety surface, not `bridge.py`'s — but if you find a way to use one to bypass the *allowlist itself* (not just to make Claude say something), that's very much in scope.

Not a vulnerability: an allowed user (the one person `DISCORD_ALLOWED_USER_IDS` is set to) being able to do a lot of damage on purpose. That's the deployment's owner, by design — see [Known limitations](README.md#known-limitations).
