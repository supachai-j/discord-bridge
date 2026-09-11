import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading

import discord

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


# All deployment-specific values come from the environment (see .env.example)
# rather than being hardcoded, so this file has nothing to sanitize before
# it's shared — every installer edits .env, never bridge.py.
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
DISCORD_CHUNK = 1900
EDIT_INTERVAL = 1.5
TIMEOUT_SECONDS = _env_int("BRIDGE_TIMEOUT_SECONDS", minimum=30, default=20 * 60)

# Subprocesses currently in flight, so a SIGTERM/SIGINT (systemctl stop /
# restart) can kill them instead of orphaning them — discord.py's client.run()
# only ever catches KeyboardInterrupt around the event loop, never forwards
# it to whatever `claude` subprocess happens to be running at the time.
_active_procs = set()


def _shutdown(signum, frame):
    for proc in list(_active_procs):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


def read_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def load_session_id():
    if os.path.exists(SESSION_FILE):
        sid = open(SESSION_FILE).read().strip()
        return sid or None
    return None


def save_session_id(sid):
    with open(SESSION_FILE, "w") as f:
        f.write(sid)


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
        # assignment happens and skip the kill entirely. Give it a moment.
        for _ in range(50):  # up to ~5s
            if self.proc is not None:
                break
            if self.done.wait(timeout=0.1):
                return
        if self.proc and self.proc.poll() is None:
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

    if message.content.strip().lower() in ("!reset", "!new"):
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        await message.reply("🔄 ล้าง session แล้ว ข้อความถัดไปจะเริ่มบทสนทนาใหม่", mention_author=False)
        return

    if busy_lock.locked():
        await message.reply("busy — คำสั่งก่อนหน้ายังไม่เสร็จ รอสักครู่นะครับ", mention_author=False)
        return

    async with busy_lock:
        thinking = await message.reply("⏳ กำลังเริ่มทำงาน...", mention_author=False)
        session_id = load_session_id()
        run = StreamRun(message.content, session_id)
        run.start()

        last_shown = None
        while not run.done.is_set():
            await asyncio.sleep(EDIT_INTERVAL)
            preview = run.text[-DISCORD_CHUNK:] if run.text else "(รอ output...)"
            body = f"⏳ กำลังทำงาน...\n{preview}"
            if body != last_shown:
                try:
                    await thinking.edit(content=body)
                    last_shown = body
                except discord.HTTPException:
                    pass

        if run.final and not run.final.get("is_error"):
            if run.final.get("session_id"):
                save_session_id(run.final["session_id"])
            text = (run.final.get("result") or run.text or "").strip() or "(no output)"
        else:
            err_text = run.error or (run.final or {}).get("result") or "เกิดข้อผิดพลาดไม่ทราบสาเหตุ"
            # session_id.txt can go stale (session deleted/corrupted) and then
            # every message fails the same way forever until someone deletes
            # it by hand. Self-heal: drop it so the next message starts fresh.
            if session_id and re.search(
                r"no conversation found|session.*not found|invalid session|no such session",
                err_text, re.I | re.S,  # re.S: claude's stderr can wrap the phrase across lines
            ):
                if os.path.exists(SESSION_FILE):
                    os.remove(SESSION_FILE)
                err_text += "\n\n(session id เดิมเสียหรือหาไม่เจอ — ล้างให้แล้ว ลองพิมพ์คำสั่งใหม่อีกครั้ง)"
            text = "⚠️ " + err_text

        chunks = [text[i:i + DISCORD_CHUNK] for i in range(0, len(text), DISCORD_CHUNK)] or ["(no output)"]
        try:
            await thinking.edit(content=chunks[0])
        except discord.HTTPException:
            await message.channel.send(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)


client.run(read_token())
