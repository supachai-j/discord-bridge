import pytest

import bridge

# ---------------------------------------------------------------------------
# _env_int / _env_int_set — the config-parsing helpers that turn a bad .env
# into a clear startup message instead of a raw traceback or, worse, a
# silently wrong default.
# ---------------------------------------------------------------------------

def test_env_int_missing_required(monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    with pytest.raises(SystemExit, match="missing required env var FOO"):
        bridge._env_int("FOO")


def test_env_int_missing_uses_default(monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    assert bridge._env_int("FOO", default=42) == 42


def test_env_int_malformed_raises_clean_systemexit(monkeypatch):
    monkeypatch.setenv("FOO", "12,34")
    with pytest.raises(SystemExit, match="expected an integer"):
        bridge._env_int("FOO")


def test_env_int_below_minimum_rejected(monkeypatch):
    monkeypatch.setenv("FOO", "5")
    with pytest.raises(SystemExit, match="must be at least 30"):
        bridge._env_int("FOO", minimum=30)


def test_env_int_at_minimum_is_ok(monkeypatch):
    monkeypatch.setenv("FOO", "30")
    assert bridge._env_int("FOO", minimum=30) == 30


def test_env_int_set_parses_csv_with_stray_whitespace(monkeypatch):
    monkeypatch.setenv("FOO", "1, 2,3")
    assert bridge._env_int_set("FOO") == {1, 2, 3}


def test_env_int_set_tolerates_stray_commas(monkeypatch):
    # A trailing/doubled comma from a copy-paste shouldn't be fatal — only a
    # genuinely non-numeric token should be.
    monkeypatch.setenv("FOO", "1,,2,")
    assert bridge._env_int_set("FOO") == {1, 2}


def test_env_int_set_missing_required_raises(monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    with pytest.raises(SystemExit, match="missing required env var FOO"):
        bridge._env_int_set("FOO")


def test_env_int_set_missing_not_required_returns_empty(monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    assert bridge._env_int_set("FOO", required=False) == set()


def test_env_int_set_malformed_raises_clean_systemexit(monkeypatch):
    monkeypatch.setenv("FOO", "1,abc")
    with pytest.raises(SystemExit, match="expected comma-separated integers"):
        bridge._env_int_set("FOO")


# ---------------------------------------------------------------------------
# is_stale_session_error — the auto-heal detector. Bounded so it can't
# false-positive by matching "session" and "not found" across a large
# unrelated stretch of stderr (a real review finding on the earlier,
# unbounded `.*` version of this pattern).
# ---------------------------------------------------------------------------

def test_stale_session_detects_no_conversation_found():
    assert bridge.is_stale_session_error("Error: no conversation found for id abc123")


def test_stale_session_detects_session_not_found_nearby():
    assert bridge.is_stale_session_error("session abc123 not found")


def test_stale_session_detects_across_a_short_newline_wrap():
    assert bridge.is_stale_session_error("session id abc123\nwas not found in store")


def test_stale_session_ignores_unrelated_far_apart_text():
    noise = "x" * 500
    err_text = f"session started fine {noise} some other file was not found"
    assert not bridge.is_stale_session_error(err_text)


def test_stale_session_ignores_generic_errors():
    assert not bridge.is_stale_session_error("rate limited, please retry later")


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------

def test_chunk_text_single_chunk_when_short():
    assert bridge.chunk_text("hello", size=100) == ["hello"]


def test_chunk_text_splits_on_boundary():
    assert bridge.chunk_text("abcdef", size=2) == ["ab", "cd", "ef"]


def test_chunk_text_empty_returns_empty_list():
    # Documents the contract callers rely on: every call site guarantees
    # non-empty text (see the comment above chunk_text's one caller in
    # on_message), so this never actually happens in production — but the
    # function itself stays honest about it rather than faking a chunk.
    assert bridge.chunk_text("") == []


def test_chunk_text_default_size_matches_discord_limit():
    text = "a" * (bridge.DISCORD_CHUNK + 1)
    chunks = bridge.chunk_text(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == bridge.DISCORD_CHUNK


# ---------------------------------------------------------------------------
# load_session_id / save_session_id round trip
# ---------------------------------------------------------------------------

def test_session_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "SESSION_FILE", str(tmp_path / "session_id.txt"), raising=False)
    assert bridge.load_session_id() is None
    bridge.save_session_id("abc-123")
    assert bridge.load_session_id() == "abc-123"


def test_load_session_id_strips_whitespace(tmp_path, monkeypatch):
    session_file = tmp_path / "session_id.txt"
    session_file.write_text("  abc-123  \n")
    monkeypatch.setattr(bridge, "SESSION_FILE", str(session_file), raising=False)
    assert bridge.load_session_id() == "abc-123"


def test_load_session_id_empty_file_is_none(tmp_path, monkeypatch):
    session_file = tmp_path / "session_id.txt"
    session_file.write_text("")
    monkeypatch.setattr(bridge, "SESSION_FILE", str(session_file), raising=False)
    assert bridge.load_session_id() is None
