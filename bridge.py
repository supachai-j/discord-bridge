import asyncio
import json
import logging
import os
import re
import shlex
import signal

# subprocess: the whole point of this file is to run an agent CLI.
import subprocess  # nosec B404
import sys
import threading

import aiohttp  # already a transitive dependency of discord.py — no new package
import discord

__version__ = "0.2.0"

# discord.py already configures logging (that's where the "discord.gateway:
# connected" lines in the systemd journal come from) — use the same module
# so our own messages share its timestamp/level formatting instead of a
# separate, inconsistent print() stream.
logger = logging.getLogger("bridge")


def _env_int_set(name, required=True):
    raw = os.environ.get(name, "").strip()
    if not raw:
        if required:
            raise SystemExit(f"missing required env var {name} (see .env.example)")
        return set()
    try:
        return {int(x) for x in raw.split(",") if x.strip()}
    except ValueError:
        raise SystemExit(f"{name}={raw!r} — expected comma-separated integers (see .env.example)")


def _env_int(name, minimum=None, default=None):
    raw = os.environ.get(name, "").strip()
    if not raw:
        if default is not None:
            return default
        raise SystemExit(f"missing required env var {name} (see .env.example)")
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} — expected an integer (see .env.example)")
    if minimum is not None and value < minimum:
        raise SystemExit(f"{name}={value} — must be at least {minimum} (see .env.example)")
    return value


def _env_str(name, required=True, default=None):
    raw = os.environ.get(name, "").strip()
    if not raw:
        if required:
            raise SystemExit(f"missing required env var {name} for BACKEND={BACKEND!r} (see .env.example)")
        return default
    return raw


# Constants with no per-deployment meaning — unlike everything _load_config()
# reads, these are always the same regardless of environment.
DISCORD_CHUNK = 1900
EDIT_INTERVAL = 1.5

# Subprocesses currently in flight, so a SIGTERM/SIGINT (systemctl stop /
# restart) can kill them instead of orphaning them — discord.py's client.run()
# only ever catches KeyboardInterrupt around the event loop, never forwards
# it to whatever agent subprocess happens to be running at the time.
_active_procs = set()


def _load_config():
    # Deferred into a function — called from main(), not at import time — so
    # this module can be imported (e.g. by tests) without a live .env in
    # place. See .env.example for what each of these does; nothing
    # deployment-specific is hardcoded, so this file has nothing to sanitize
    # before it's shared — every installer edits .env, never bridge.py.
    global TOKEN_FILE, CHANNEL_ID, ALLOWED_USER_IDS, CLAUDE_BIN, CLAUDE_MODEL, CLAUDE_FALLBACK_MODEL
    global CLAUDE_CONFIG_DIR, WORKDIR, SESSION_FILE, TIMEOUT_SECONDS, LOG_LEVEL
    global BACKEND, CLI_BIN, CLI_ARGS_NEW, CLI_ARGS_RESUME
    global OPENAI_COMPATIBLE_BASE_URL, OPENAI_COMPATIBLE_API_KEY_FILE, OPENAI_COMPATIBLE_MODEL
    global OPENAI_COMPATIBLE_SYSTEM_PROMPT, OPENAI_COMPATIBLE_MAX_HISTORY

    LOG_LEVEL = getattr(logging, os.environ.get("BRIDGE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    TOKEN_FILE = os.path.expanduser(os.environ.get("DISCORD_BOT_TOKEN_FILE", "~/.discord_bot_token"))
    CHANNEL_ID = _env_int("DISCORD_CHANNEL_ID")
    ALLOWED_USER_IDS = _env_int_set("DISCORD_ALLOWED_USER_IDS")
    WORKDIR = os.path.expanduser(os.environ.get("BRIDGE_WORKDIR", "~"))
    SESSION_FILE = os.path.expanduser(os.environ.get("BRIDGE_SESSION_FILE", "~/discord-bridge/session_id.txt"))
    TIMEOUT_SECONDS = _env_int("BRIDGE_TIMEOUT_SECONDS", minimum=30, default=20 * 60)

    # Which backend answers messages — see docs/decisions/0005-pluggable-backends.md.
    # "claude" is the only one with a real production deployment behind it;
    # the others are documented as needing verification against your actual
    # installed CLI / endpoint before trusting them.
    BACKEND = os.environ.get("BACKEND", "claude").strip().lower()
    if BACKEND not in ("claude", "cli", "openai_compatible"):
        raise SystemExit(f"BACKEND={BACKEND!r} — must be claude, cli, or openai_compatible")

    CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
    CLAUDE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR")  # optional — unset means claude's own default
    # Optional — unset means whatever claude's own default model is for this
    # CLAUDE_CONFIG_DIR/account. Accepts an alias ("opus", "sonnet", "fable")
    # or a full model name, same as `claude --model`.
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL")
    CLAUDE_FALLBACK_MODEL = os.environ.get("CLAUDE_FALLBACK_MODEL")
    # Recommended: point WORKDIR somewhere other than $HOME. It's defense-in-
    # depth on top of whatever permissions.deny rules you put in claude's
    # settings.json (those block Read() on secrets regardless of cwd) —
    # keeping the subprocess's cwd off $HOME just means relative-path tool
    # calls don't land in it either.

    CLI_BIN = CLI_ARGS_NEW = CLI_ARGS_RESUME = None
    if BACKEND == "cli":
        CLI_BIN = _env_str("CLI_BIN")
        CLI_ARGS_NEW = _env_str("CLI_ARGS_NEW")  # e.g. "exec {prompt}" for codex, "-p {prompt}" for gemini
        # Optional: a template for continuing a prior turn, e.g.
        # "exec resume --last {prompt}" for codex. Leave unset for a CLI with
        # no such capability (e.g. gemini's headless mode as of this writing)
        # — every message then starts a fresh, stateless conversation, which
        # is an accurate reflection of that CLI's real capability, not a
        # limitation of this adapter.
        CLI_ARGS_RESUME = _env_str("CLI_ARGS_RESUME", required=False)

    OPENAI_COMPATIBLE_BASE_URL = OPENAI_COMPATIBLE_API_KEY_FILE = OPENAI_COMPATIBLE_MODEL = None
    OPENAI_COMPATIBLE_SYSTEM_PROMPT = OPENAI_COMPATIBLE_MAX_HISTORY = None
    if BACKEND == "openai_compatible":
        OPENAI_COMPATIBLE_BASE_URL = _env_str("OPENAI_COMPATIBLE_BASE_URL").rstrip("/")
        OPENAI_COMPATIBLE_API_KEY_FILE = os.path.expanduser(_env_str("OPENAI_COMPATIBLE_API_KEY_FILE"))
        OPENAI_COMPATIBLE_MODEL = _env_str("OPENAI_COMPATIBLE_MODEL")
        OPENAI_COMPATIBLE_SYSTEM_PROMPT = _env_str("OPENAI_COMPATIBLE_SYSTEM_PROMPT", required=False)
        OPENAI_COMPATIBLE_MAX_HISTORY = _env_int("OPENAI_COMPATIBLE_MAX_HISTORY", minimum=2, default=40)


def _shutdown(signum, frame):
    for proc in list(_active_procs):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    # Prefer letting the gateway connection close gracefully: schedule
    # client.close() on the running loop so client.run()'s own `async with
    # self:` block unwinds normally and asyncio.run() returns on its own.
    # Fall back to a hard exit if the loop isn't reachable for some reason
    # (e.g. a signal arriving before the loop has started).
    try:
        loop = client.loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.create_task, client.close())
            return
    # Best-effort graceful path in a signal handler; sys.exit(0) right below
    # is the real fallback either way.
    except Exception:  # nosec B110
        pass
    sys.exit(0)


def read_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def load_session_id():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            sid = f.read().strip()
        return sid or None
    return None


def save_session_id(sid):
    with open(SESSION_FILE, "w") as f:
        f.write(sid)


# {0,100}? keeps this from matching across large unrelated stretches of
# stderr just because "session" appears early and "not found" appears
# somewhere much later in the buffer. re.S: an agent's stderr can wrap the
# phrase across lines.
_STALE_SESSION_RE = re.compile(
    r"no conversation found|session.{0,100}?not found|invalid session|no such session",
    re.I | re.S,
)


def is_stale_session_error(err_text):
    """True if err_text looks like the backend rejected a resume because the
    session/state it was given no longer exists, rather than some other kind
    of failure."""
    return bool(_STALE_SESSION_RE.search(err_text))


def chunk_text(text, size=DISCORD_CHUNK):
    """Split text into size-character pieces for Discord's message-length
    limit. Every call site here guarantees text is non-empty, so the normal
    contract is "at least one chunk" — chunk_text("") is the one exception,
    returning [] rather than pretending a fake chunk exists."""
    return [text[i:i + size] for i in range(0, len(text), size)]


class BackendRun:
    """Common shape every backend exposes to on_message, regardless of
    whether it's actually a subprocess or an HTTP call underneath: a live
    .text attribute to poll for the streaming preview, a .done Event, and
    .error / .state set once .done fires. .state is opaque to on_message —
    a resumable session id string for an agent CLI, a serialized transcript
    for a stateless chat API, or None if the backend has no persistence."""

    def __init__(self, prompt, state):
        self.prompt = prompt
        self.text = ""
        self.error = None
        # Starts as the *input* state (what to resume from) and is
        # overwritten with the *new* state by _finalize()/_run_async() on
        # success. On error it's left as the input value, which is harmless
        # since on_message only persists it when .error is None.
        self.state = state
        self.done = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        raise NotImplementedError


class _WatchedSubprocessRun(BackendRun):
    """Shared machinery for any backend that talks to another CLI via
    subprocess: a concurrent stderr-drain thread (avoids a pipe-buffer
    deadlock if the child writes a lot to stderr while nothing reads it —
    see docs/runbook.md's fix ledger), and a watchdog that kills a hung
    process after TIMEOUT_SECONDS. Subclasses implement _build_argv(),
    _handle_line(), and _finalize()."""

    def __init__(self, prompt, state):
        super().__init__(prompt, state)
        self.proc = None
        self.stderr_tail = ""
        self.timed_out = False

    def start(self):
        super().start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self):
        # TIMEOUT_SECONDS used to be passed to proc.wait() *after* the stdout
        # loop already returned, so it only ever bounded the final reap, not
        # a hung/slow subprocess. This actually kills it.
        if self.done.wait(timeout=TIMEOUT_SECONDS):
            return
        self.timed_out = True
        logger.warning("%s subprocess exceeded BRIDGE_TIMEOUT_SECONDS=%s, killing it",
                        type(self).__name__, TIMEOUT_SECONDS)
        # self.proc is set by _run() right after Popen(); on a very short
        # TIMEOUT_SECONDS the watchdog can otherwise wake up before that
        # assignment happens. Wait for either — not capped, since one of the
        # two is always guaranteed to happen: _run() either reaches Popen()
        # or hits an exception first and sets `done` itself.
        while self.proc is None:
            if self.done.wait(timeout=0.1):
                return
        if self.proc.poll() is None:
            self.proc.kill()

    def _drain_stderr(self, pipe):
        # Must run concurrently with the stdout loop below: if nothing reads
        # stderr while it fills its OS pipe buffer (~64KB), a chatty child
        # blocks on write() and the whole run deadlocks.
        try:
            for line in pipe:
                self.stderr_tail = (self.stderr_tail + line)[-1500:]
        # Background drain thread must never crash the run over a logging
        # concern.
        except Exception:  # nosec B110
            pass

    def _build_argv(self):
        raise NotImplementedError

    def _env_overrides(self):
        return {}

    def _handle_line(self, line):
        """Called once per raw stdout line (already whitespace-stripped, and
        never empty). Mutate self.text/self.error/self.state as needed."""
        raise NotImplementedError

    def _finalize(self, returncode):
        """Called once after the process exits and stdout is exhausted, to
        settle self.text/self.error/self.state into their final values."""
        raise NotImplementedError

    def _run(self):
        env = os.environ.copy()
        env.update(self._env_overrides())
        try:
            # argv is a list and shell=False (default), so self.prompt (the
            # Discord message) can't inject shell metacharacters here.
            self.proc = subprocess.Popen(  # nosec B603
                self._build_argv(), cwd=WORKDIR, env=env, bufsize=1,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            _active_procs.add(self.proc)
            stderr_thread = threading.Thread(
                target=self._drain_stderr, args=(self.proc.stderr,), daemon=True
            )
            stderr_thread.start()
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                self._handle_line(line)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            stderr_thread.join(timeout=5)
            self._finalize(self.proc.returncode)
        except Exception as e:
            self.error = str(e)
        finally:
            _active_procs.discard(self.proc)
            self.done.set()


class ClaudeRun(_WatchedSubprocessRun):
    """`claude -p --output-format stream-json`. This is the one backend with
    a real production deployment behind it — see CLAUDE.md before changing
    _handle_line/_finalize; their behavior must stay identical to what
    shipped as StreamRun before this file supported multiple backends."""

    def __init__(self, prompt, state):
        super().__init__(prompt, state)
        self._final = None

    def _build_argv(self):
        argv = [
            CLAUDE_BIN, "-p", self.prompt,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if self.state:
            argv += ["--resume", self.state]
        if CLAUDE_MODEL:
            argv += ["--model", CLAUDE_MODEL]
        if CLAUDE_FALLBACK_MODEL:
            argv += ["--fallback-model", CLAUDE_FALLBACK_MODEL]
        return argv

    def _env_overrides(self):
        return {"CLAUDE_CONFIG_DIR": CLAUDE_CONFIG_DIR} if CLAUDE_CONFIG_DIR else {}

    def _handle_line(self, line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if obj.get("type") == "stream_event":
            ev = obj.get("event", {})
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    self.text += delta.get("text", "")
        elif obj.get("type") == "result":
            self._final = obj

    def _finalize(self, returncode):
        final = self._final
        if final is not None and not final.get("is_error"):
            if final.get("session_id"):
                self.state = final["session_id"]
            self.text = (final.get("result") or self.text or "").strip() or "(no output)"
        elif final is not None:
            self.error = final.get("result") or "เกิดข้อผิดพลาดไม่ทราบสาเหตุ"
        elif self.timed_out:
            self.error = f"หมดเวลา ({TIMEOUT_SECONDS}s) — สั่ง kill process แล้ว"
        else:
            self.error = self.stderr_tail or f"(exit {returncode}, no result line)"


class CLIRun(_WatchedSubprocessRun):
    """Generic adapter for another agentic CLI (codex, Gemini CLI, or
    similar), configured entirely through CLI_BIN/CLI_ARGS_NEW/
    CLI_ARGS_RESUME — see .env.example. Deliberately plaintext-only: every
    stdout line is a text chunk, with no attempt to parse a CLI-specific
    structured event format, because that format (a) differs per tool and
    (b) isn't something this project can verify without the CLI installed
    and tested live. See docs/decisions/0005-pluggable-backends.md."""

    def _substitute(self, template):
        return [self.prompt if tok == "{prompt}" else tok for tok in shlex.split(template)]

    def _build_argv(self):
        template = CLI_ARGS_RESUME if (self.state and CLI_ARGS_RESUME) else CLI_ARGS_NEW
        return [CLI_BIN] + self._substitute(template)

    def _handle_line(self, line):
        self.text += line + "\n"

    def _finalize(self, returncode):
        self.text = self.text.strip()
        if not self.text:
            self.error = self.stderr_tail or f"(exit {returncode}, no output)"
            return
        if self.timed_out:
            self.error = f"หมดเวลา ({TIMEOUT_SECONDS}s) — สั่ง kill process แล้ว"
            return
        # There's no session id to parse out of plain text, so "state" here
        # just means "a previous turn happened" — enough to pick
        # CLI_ARGS_RESUME next time for a CLI whose resume flag (e.g. codex's
        # `resume --last`) doesn't need an explicit id.
        if CLI_ARGS_RESUME:
            self.state = "1"


def openai_build_messages(system_prompt, history, prompt):
    """Assemble the `messages` array for an OpenAI-compatible chat request.
    Pure and independently testable — no network involved."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    return messages


def openai_parse_sse_line(line):
    """Parse one raw SSE line from an OpenAI-compatible streaming response.
    Returns a text delta (possibly ""), None if the line carries no delta
    (a non-`data:` line, or a chunk with no content field), or the sentinel
    string "[DONE]" when the stream signals completion."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if data == "[DONE]":
        return "[DONE]"
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = obj.get("choices") or [{}]
    return (choices[0].get("delta") or {}).get("content")


def openai_trim_history(history, max_messages):
    """Keep only the most recent max_messages entries — a stateless chat
    API's request (and cost) would otherwise grow without bound over a long
    conversation."""
    return history[-max_messages:] if max_messages else history


class OpenAICompatibleRun(BackendRun):
    """Talks to any POST {base_url}/chat/completions endpoint using the
    OpenAI streaming chat-completions wire format (SSE `data: {...}` lines,
    a `[DONE]` sentinel) — this covers grok (api.x.ai/v1), GLM, and local
    models via Ollama/vLLM/llama.cpp, which all expose this same shape.

    No file/shell tool access — this is a plain chat backend, not an
    agentic one. `state` is a JSON-encoded message list, not a server-side
    session id, since stateless chat APIs have no resume concept of their
    own; it's truncated to OPENAI_COMPATIBLE_MAX_HISTORY messages so a long
    conversation doesn't grow the request (and the token bill) forever."""

    def _run(self):
        try:
            asyncio.run(self._run_async())
        except Exception as e:
            self.error = str(e)
        finally:
            self.done.set()

    def _read_api_key(self):
        with open(OPENAI_COMPATIBLE_API_KEY_FILE) as f:
            return f.read().strip()

    async def _run_async(self):
        history = []
        if self.state:
            try:
                history = json.loads(self.state)
            except (json.JSONDecodeError, TypeError):
                history = []

        messages = openai_build_messages(OPENAI_COMPATIBLE_SYSTEM_PROMPT, history, self.prompt)
        payload = {"model": OPENAI_COMPATIBLE_MODEL, "messages": messages, "stream": True}
        headers = {
            "Authorization": f"Bearer {self._read_api_key()}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{OPENAI_COMPATIBLE_BASE_URL}/chat/completions", json=payload, headers=headers,
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:500]
                    self.error = f"HTTP {resp.status}: {body}"
                    return
                async for raw_line in resp.content:
                    delta = openai_parse_sse_line(raw_line.decode("utf-8", "replace"))
                    if delta == "[DONE]":
                        break
                    if delta:
                        self.text += delta

        if not self.text:
            self.error = "(no output)"
            return
        new_history = history + [
            {"role": "user", "content": self.prompt},
            {"role": "assistant", "content": self.text},
        ]
        self.state = json.dumps(openai_trim_history(new_history, OPENAI_COMPATIBLE_MAX_HISTORY))


_BACKEND_RUN_CLASSES = {
    "claude": ClaudeRun,
    "cli": CLIRun,
    "openai_compatible": OpenAICompatibleRun,
}


def make_run(prompt, state):
    return _BACKEND_RUN_CLASSES[BACKEND](prompt, state)


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

busy_lock = asyncio.Lock()


@client.event
async def on_ready():
    logger.info("logged in as %s (id=%s) — watching channel %s [backend=%s]",
                client.user, client.user.id, CHANNEL_ID, BACKEND)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return

    if message.author.id not in ALLOWED_USER_IDS:
        await message.add_reaction("🚫")
        return

    is_reset = message.content.strip().lower() in ("!reset", "!new")

    if busy_lock.locked():
        # Also gates !reset: if it ran while a run was still in flight, that
        # run's eventual save_session_id() would silently recreate
        # SESSION_FILE with the old state, undoing the reset the user just
        # asked for. Treat !reset like any other message w.r.t. the lock.
        await message.reply("busy — คำสั่งก่อนหน้ายังไม่เสร็จ รอสักครู่นะครับ", mention_author=False)
        return

    if is_reset:
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        logger.info("session reset requested by user %s", message.author.id)
        await message.reply("🔄 ล้าง session แล้ว ข้อความถัดไปจะเริ่มบทสนทนาใหม่", mention_author=False)
        return

    async with busy_lock:
        thinking = await message.reply("⏳ กำลังเริ่มทำงาน...", mention_author=False)
        state_in = load_session_id()
        run = make_run(message.content, state_in)
        run.start()

        last_shown = None
        edit_failures = 0
        while not run.done.is_set():
            # Back off the edit cadence on repeated failures (e.g. Discord's
            # per-message edit rate limit under a long, fast-streaming run)
            # instead of retrying at a fixed 1.5s regardless — and log it,
            # instead of a silent `except: pass`.
            await asyncio.sleep(EDIT_INTERVAL * min(2 ** edit_failures, 8))
            preview = run.text[-DISCORD_CHUNK:] if run.text else "(รอ output...)"
            body = f"⏳ กำลังทำงาน...\n{preview}"
            if body != last_shown:
                try:
                    await thinking.edit(content=body)
                    last_shown = body
                    edit_failures = 0
                except discord.HTTPException as e:
                    edit_failures = min(edit_failures + 1, 3)
                    logger.warning("preview edit failed, backing off: %s", e)

        if run.error is None:
            if run.state:
                save_session_id(run.state)
            text = run.text
        else:
            err_text = run.error
            # Session/state can go stale (deleted upstream, corrupted, or an
            # id the backend no longer recognizes) and then every message
            # fails the same way forever until someone deletes it by hand.
            # Self-heal: drop it so the next message starts fresh.
            if state_in and is_stale_session_error(err_text):
                if os.path.exists(SESSION_FILE):
                    os.remove(SESSION_FILE)
                logger.warning("stale session/state detected and cleared: %s", err_text)
                err_text += "\n\n(session id เดิมเสียหรือหาไม่เจอ — ล้างให้แล้ว ลองพิมพ์คำสั่งใหม่อีกครั้ง)"
            text = "⚠️ " + err_text

        # text is never empty here: both branches above end in an
        # `... or "some fallback string"` chain, so this always yields at
        # least one chunk.
        chunks = chunk_text(text)
        try:
            await thinking.edit(content=chunks[0])
        except discord.HTTPException:
            await message.channel.send(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)


def main():
    _load_config()
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    # root_logger=True: discord.py's default only attaches its handler to
    # the "discord.*" logger namespace, so our own "bridge" logger would
    # otherwise have no handler and silently fall back to Python's WARNING-
    # only lastResort handler — INFO logs would vanish, not just print
    # differently.
    logging.getLogger("bridge").setLevel(LOG_LEVEL)
    client.run(read_token(), log_level=LOG_LEVEL, root_logger=True)


if __name__ == "__main__":
    main()
