# 0003. No CD — deployment stays a manual, deliberate step

Status: Accepted

## Context

CI (lint + tests + a security scan) runs automatically on every push. The natural next question is whether merging to `main`, or cutting a tagged release, should also automatically land on the machine actually running the bot.

## Decision

It doesn't, on purpose. `.github/workflows/release.yml` only creates a GitHub Release when a tag is pushed — it has no access to, and makes no attempt to reach, any running instance. Getting a release onto a live deployment is always `git pull && sudo systemctl restart discord-bridge@<name>.service` (or the flat `discord-bridge.service`), typed by a person.

## Alternatives considered

- **Auto-deploy on push to `main`.** Rejected outright — this would mean an untested or half-finished commit takes effect on a bot with live `claude` tool access the moment it's pushed, with no human checkpoint at all.
- **Auto-deploy on tagged release, via a self-hosted GitHub Actions runner on the host.** Considered more seriously — CI could genuinely trigger a real restart. Rejected for now because the actual deployment target is a home-lab VM behind NAT; a self-hosted runner would need outbound-only connectivity (fine) but also means a GitHub-Actions-controlled process has standing access to the box that runs a bot with real tool permissions — an extra credential/trust boundary that isn't worth it while there's exactly one maintainer who can just SSH in.
- **Pull-based auto-deploy (a systemd timer on the host that periodically checks for and applies new tags).** Same rejection as above, minus the runner-credential concern, but still: a bad tag pushed by mistake would auto-apply within one timer interval with no human in the loop. Left as a documented possibility, not implemented — see `README.md#releasing`.

## Consequences

- A bad release requires a bad `git pull` *and* a person to have typed it — not a mis-click or a bot config error.
- Cutting a release and having it actually run somewhere are two separate, independently-timed actions. This is a feature for a project with real host access, not friction to remove later.
- If a second maintainer or a fleet of instances ever appears, this decision should be revisited explicitly (write a new ADR marking this one superseded) rather than quietly working around it with an ad hoc script.
