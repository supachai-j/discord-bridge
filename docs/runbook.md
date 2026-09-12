# Operations runbook

Day-to-day commands and "X is happening, now what" for a running `discord-bridge` deployment. For setup, see [README.md](../README.md); for the code-level rationale behind specific fixes, see the fix ledger at the bottom.

## Cheat sheet

```bash
# Status / logs
systemctl status discord-bridge@<name>.service
journalctl -u discord-bridge@<name>.service -f

# Deploy a change (no CD — this is always a deliberate, manual step)
cd discord-bridge && git pull
sudo systemctl restart discord-bridge@<name>.service

# Reset a conversation without SSH
#   type !reset or !new in the Discord channel
# ...or by hand:
rm discord-bridge/instances/<name>.session_id.txt   # path per instances/<name>.env

# Confirm nothing was orphaned by a restart
ps aux | grep bridge.py
```

Single-instance (flat `discord-bridge.service` instead of the `@` template) deployments: drop `@<name>` from the unit name and use `session_id.txt` / `.env` at the repo root instead of `instances/`.

## Failure modes

| Symptom | Cause | What happens now |
|---|---|---|
| Stuck at "⏳ กำลังทำงาน..." indefinitely | `claude` subprocess hung | Watchdog kills it after `BRIDGE_TIMEOUT_SECONDS` (default 1200s) and the channel gets a timeout error reply. If it's still stuck past that, the watchdog itself is broken — check `journalctl` for a Python traceback. |
| No reply, no error, nothing in the channel | A chatty `claude` subprocess filled the stderr pipe buffer and deadlocked against the stdout-reading loop | Shouldn't happen — stderr is drained on its own thread concurrently. If it does, that's a regression; bisect against the fix ledger below. |
| Same error on every message, forever | Stale `session_id` — the session `claude` thinks it's resuming no longer exists | Auto-heals: an error matching "session not found"-shaped text clears the session file automatically. If it's still looping, the error text doesn't match the pattern — check it against `is_stale_session_error` in `bridge.py` and widen the regex if needed (keep the `{0,100}?` bound, see fix ledger). |
| `!reset` said it cleared the session, but the bot still remembers the old conversation | A message was still in flight when `!reset` ran, and its `save_session_id()` call landed after the reset | Fixed — `!reset` now respects `busy_lock` like any other message, so it can't race an in-flight run. If you see this on a version built before that fix, update. |
| `busy — คำสั่งก่อนหน้ายังไม่เสร็จ` | Working as intended — messages aren't queued | Wait for the current run to finish, then resend. |
| Bot goes offline right after `systemctl restart`, back online a few seconds later | Normal — SIGTERM triggers a graceful `client.close()` before the new process starts | If it takes noticeably longer than a couple of seconds, or the old process is still running (`ps aux | grep bridge.py`), something's wrong with shutdown — see the `_shutdown` fix in the ledger. |
| Preview message stops updating mid-run but the final reply still arrives | Discord edit rate limit — backs off and logs it (`journalctl`) instead of retrying at a fixed cadence | Not an error; the final result still lands as a fresh message if the edit ultimately fails. |
| Bot won't start; log says `missing required env var ...` | `.env` (or `instances/<name>.env`) is missing a required variable, or it's malformed | The message names the variable — check it against `.env.example`. This fails fast on purpose rather than falling back to an insecure or nonsensical default. |

## Suspected security incident

- **Bot token possibly leaked** (committed to a fork, pasted somewhere, etc.): regenerate it in the Discord Developer Portal, update the token file, `systemctl restart`. Old sessions the leaked token could have opened are invalidated the moment you regenerate.
- **Unexpected command output appearing in the channel**: check `DISCORD_ALLOWED_USER_IDS` first — confirm it's still exactly who you expect. Then check `journalctl` for what `claude` was actually asked to run; the prompt is the literal Discord message content.
- **Suspect a permission you didn't intend to grant was used**: check `<CLAUDE_CONFIG_DIR>/settings.json`'s `permissions.allow`/`.deny` — remember an unset `CLAUDE_CONFIG_DIR` means the bot shares whatever permissions your interactive `claude` sessions have (see [README's security model](../README.md#security-model)).
- Found an actual vulnerability in `bridge.py` itself (not a deployment misconfiguration)? See [SECURITY.md](../SECURITY.md) — please don't file it as a public issue.

## Fix ledger

Kept short and factual — this is a log of what changed and why, not a marketing changelog. Full detail in each commit message.

| Commit | What | Why |
|---|---|---|
| `57fcd4d` | Watchdog actually kills hung `claude`; stderr drained concurrently; `!reset`/self-heal for stale sessions; `requirements.txt` pinned | Timeout was previously applied *after* the read loop already returned, so it bounded nothing; undrained stderr could deadlock the whole run |
| `3e5760c` | `!reset` now respects `busy_lock`; stale-session regex bounded (`{0,100}?` instead of unbounded `.*`); watchdog's proc-not-yet-set race removed; edit-rate backoff + logging; graceful `client.close()` on shutdown | `!reset` could previously race an in-flight run and get silently undone; the unbounded regex could false-positive across unrelated stderr |
| `110337b` | `bridge.py` refactored to be import-safe (`main()` behind `if __name__`); `is_stale_session_error`/`chunk_text` extracted; test suite + CI added | Needed so the module can be unit tested without a live `.env` or registering signal handlers as a side effect of import |
| `2a59b33` | Documentation site added (`docs/`) | — |
| `d20ecaf` | Initial public history (squashed) | Earlier commits had real Discord channel/user ids baked in; history was rewritten before making the repo public — see `CLAUDE.md` |
