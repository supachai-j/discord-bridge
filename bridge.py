import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading

import discord

__version__ = "0.1.0"


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


# Constants with no per-deployment meaning — unlike everything _load_config()
# reads, these are always the same regardless of environment.
DISCORD_CHUNK = 1900
EDIT_INTERVAL = 1.5

# Subprocesses currently in flight, so a SIGTERM/SIGINT (systemctl stop /
# restart) can kill them instead of orphaning them — discord.py's client.run()
# only ever catches KeyboardInterrupt around the event loop, never forwards
# it to whatever `claude` subprocess happens to be running at the time.
_active_procs = set()


def _load_config():
    # Deferred into a function — called from main(), not at import time — so
    # this module can be imported (e.g. by tests) without a live .env in
    # place. See .env.example for what each of these does; nothing
    # deployment-specific is hardcoded, so this file has nothing to sanitize
    # before it's shared — every installer edits .env, never bridge.py.
    global TOKEN_FILE, CHANNEL_ID, ALLOWED_USER_IDS, CLAUDE_BIN
    global CLAUDE_CONFIG_DIR, WORKDIR, SESSION_FILE, TIMEOUT_SECONDS
    TOKEN_FILE = os.path.expanduser(os.environ.get("DISCORD_BOT_TOKEN_FILE", "~/.discord_bot_token"))
    CHANNEL_ID = _env_int("DISCORD_CHANNEL_ID")
    ALLOWED_USER_IDS = _env_int_set("DISCORD_ALLOWED_USER_IDS")
    CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
    CLAUDE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR")  # optional — unset means claude's own default
    # Recommended: point this somewhere other than $HOME. It's defense-in-depth
    # on top of whatever permissions.deny rules you put in claude's settings.json
    # (those block Read() on secrets regardless of cwd) — keeping the
    # subprocess's cwd off $HOME just means relative-path tool calls don't land
    # in it either.
    WORKDIR = os.path.expanduser(os.environ.get("BRIDGE_WORKDIR", "~"))
    SESSION_FILE = os.path.expanduser(os.environ.get("BRIDGE_SESSION_FILE", "~/discord-bridge/session_id.txt"))
    TIMEOUT_SECONDS = _env_int("BRIDGE_TIMEOUT_SECONDS", minimum=30, default=20 * 60)


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
    except Exception:
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
# somewhere much later in the buffer. re.S: claude's stderr can wrap the
# phrase across lines.
_STALE_SESSION_RE = re.compile(
    r"no conversation found|session.{0,100}?not found|invalid session|no such session",
    re.I | re.S,
)


def is_stale_session_error(err_text):
    """True if err_text looks like claude rejected --resume because the
    session id no longer exists, rather than some other kind of failure."""
    return bool(_STALE_SESSION_RE.search(err_text))


def chunk_text(text, size=DISCORD_CHUNK):
    """Split text into size-character pieces for Discord's message-length
    limit. Every call site here guarantees text is non-empty, so the normal
    contract is "at least one chunk" — chunk_text("") is the one exception,
    returning [] rather than pretending a fake chunk exists."""
    return [text[i:i + size] for i in range(0, len(text), size)]


class StreamRun:
    """Runs `claude -p --output-format stream-json` in a background thread,
    accumulating text deltas so the asyncio side can poll .text for a live preview."""

    def __init__(self, prompt, session_id):
        self.prompt = prompt
        self.session_id = session_id
        self.text = ""
        self.final = None
        self.error = None
        self.done = threading.Event()
        self.proc = None
        self.stderr_tail = ""
        self.timed_out = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self):
        # TIMEOUT_SECONDS used to be passed to proc.wait() *after* the stdout
        # loop already returned, so it only ever bounded the final reap, not
        # a hung/slow claude process. This actually kills it.
        if self.done.wait(timeout=TIMEOUT_SECONDS):
            return
        self.timed_out = True
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
        except Exception:
            pass

    def _run(self):
        env = os.environ.copy()
        if CLAUDE_CONFIG_DIR:
            env["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR
        cmd = [
            CLAUDE_BIN, "-p", self.prompt,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=WORKDIR, env=env, bufsize=1,
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
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "stream_event":
                    ev = obj.get("event", {})
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta":
                            self.text += delta.get("text", "")
                elif obj.get("type") == "result":
                    self.final = obj
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            stderr_thread.join(timeout=5)
            if self.final is None and not self.error:
                if self.timed_out:
                    self.error = f"หมดเวลา ({TIMEOUT_SECONDS}s) — สั่ง kill process แล้ว"
                else:
                    self.error = self.stderr_tail or f"(exit {self.proc.returncode}, no result line)"
        except Exception as e:
            self.error = str(e)
        finally:
            _active_procs.discard(self.proc)
            self.done.set()


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

busy_lock = asyncio.Lock()


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id}) — watching channel {CHANNEL_ID}")


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
        # Also gates !reset: if it ran while a StreamRun was still in flight,
        # that run's eventual save_session_id() would silently recreate
        # SESSION_FILE with the old id, undoing the reset the user just asked
        # for. Treat !reset like any other message w.r.t. the lock.
        await message.reply("busy — คำสั่งก่อนหน้ายังไม่เสร็จ รอสักครู่นะครับ", mention_author=False)
        return

    if is_reset:
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        await message.reply("🔄 ล้าง session แล้ว ข้อความถัดไปจะเริ่มบทสนทนาใหม่", mention_author=False)
        return

    async with busy_lock:
        thinking = await message.reply("⏳ กำลังเริ่มทำงาน...", mention_author=False)
        session_id = load_session_id()
        run = StreamRun(message.content, session_id)
        run.start()

        last_shown = None
        edit_failures = 0
        while not run.done.is_set():
            # Back off the edit cadence on repeated failures (e.g. Discord's
            # per-message edit rate limit under a long, fast-streaming run)
            # instead of retrying at a fixed 1.5s regardless — and log it,
            # instead of the previous silent `except: pass`.
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
                    print(f"[discord-bridge] preview edit failed, backing off: {e}", file=sys.stderr)

        if run.final and not run.final.get("is_error"):
            if run.final.get("session_id"):
                save_session_id(run.final["session_id"])
            text = (run.final.get("result") or run.text or "").strip() or "(no output)"
        else:
            err_text = run.error or (run.final or {}).get("result") or "เกิดข้อผิดพลาดไม่ทราบสาเหตุ"
            # session_id.txt can go stale (session deleted/corrupted) and then
            # every message fails the same way forever until someone deletes
            # it by hand. Self-heal: drop it so the next message starts fresh.
            if session_id and is_stale_session_error(err_text):
                if os.path.exists(SESSION_FILE):
                    os.remove(SESSION_FILE)
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
    client.run(read_token())


if __name__ == "__main__":
    main()
