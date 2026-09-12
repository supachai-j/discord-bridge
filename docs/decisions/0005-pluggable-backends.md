# 0005. Pluggable backends: agentic CLIs and OpenAI-compatible chat APIs

Status: Accepted

## Context

`bridge.py` hardcoded exactly one backend: `claude -p --resume`, with a parser tuned to Claude Code's specific stream-json event shape. A request came in to support other models — naming codex, Gemini, grok, and GLM, plus local models. Those aren't one kind of thing: codex and Gemini CLI ship their own first-party agentic CLI (file edits, shell commands, the same shape as `claude`), while grok, GLM, and most local-model setups are only reachable as a plain chat-completions API with no tool-use of their own.

## Decision

A `Backend` is anything producing a `BackendRun` object with a live `.text` (for the streaming preview), `.done` (a `threading.Event`), and `.error`/`.state` set once done — the same shape `on_message`'s polling loop already used for `StreamRun`, generalized. `.state` is opaque to `on_message`: a resumable session id for an agent CLI, a serialized message-history list for a stateless chat API, or `None` if the backend has no persistence.

Three backends, selected per instance via `BACKEND` (`claude` default / `cli` / `openai_compatible` — see ADR-0002 for why per-instance, not per-message):

- **`ClaudeRun`** — today's logic, moved without changing behavior (verified against the live production instance after the move: identical argv, identical stream-json parsing, identical error precedence).
- **`CLIRun`** — a generic adapter for another agentic CLI, fully configured via env vars (`CLI_BIN`, `CLI_ARGS_NEW`, `CLI_ARGS_RESUME`). Deliberately **plaintext-only** — no attempt to parse a CLI-specific structured event format. Concretely researched codex's (`codex exec --json`, JSONL `item.completed`/`turn.completed` events, `codex exec resume --last`) and Gemini CLI's (`gemini -p --output-format json`, no documented resume in headless mode) actual current interfaces before writing this — but chose not to hardcode parsers for either, because neither CLI is installed on this host to test against, and shipping an unverified structured parser as if it were confirmed would be worse than an honest, universally-correct plaintext fallback. Resuming a specific CLI (like codex's `resume --last`) is supported through the argv template; a CLI with no resume capability at all (Gemini CLI's headless mode today) just runs stateless, which is that CLI's actual behavior, not a limitation of this adapter.
- **`OpenAICompatibleRun`** — one implementation, talking to the standard OpenAI streaming chat-completions wire format, covers grok (`api.x.ai/v1`), GLM, and any local model server (Ollama/vLLM/llama.cpp all expose this same shape) with no per-vendor code. Uses `aiohttp`, already a transitive dependency of `discord.py` — zero new packages. No file/shell tool access; this is documented prominently so nobody expects it to edit files the way `ClaudeRun`/`CLIRun` do.

## Alternatives considered

- **A fully generic, env-configured JSON-path parser for `CLIRun`** (so a deployer could point it at any structured CLI output by declaring field paths). Rejected as over-engineering for a v1 with no confirmed second CLI to validate it against — plaintext-only is simpler, always correct, and easy to extend later with a concrete `CLI_OUTPUT_MODE` once a specific CLI's shape is actually verified live.
- **A new HTTP client dependency (`httpx`/`requests`) for the chat backend.** Rejected — `aiohttp` is already installed as part of `discord.py`'s own dependency chain, and using it avoids adding anything to `requirements.txt` that wasn't already there.
- **Hardcoding grok/GLM as named backends instead of one generic `openai_compatible`.** Rejected — both expose an OpenAI-compatible endpoint, so a per-vendor backend would just be the same code with a different `base_url` default, adding maintenance surface for no behavioral difference.

## Consequences

- Adding a new agent with a different backend is an `instances/<name>.env` with a different `BACKEND` and its own vars — no code change, matching the deployment story ADR-0002 already established.
- The existing production Claude deployment required zero config changes — omitting `BACKEND` keeps exactly today's behavior.
- `CLIRun`'s codex/Gemini example configs in `.env.example` are explicitly **not** claimed as verified end-to-end; confirm against your installed CLI's `--help` before deploying (see `docs/runbook.md`).
- `on_message`, `busy_lock`, the edit-throttle loop, `!reset`, and the stale-state self-heal check are now genuinely backend-agnostic — none of them reference Claude specifically any more.
