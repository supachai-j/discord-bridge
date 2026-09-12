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


# ---------------------------------------------------------------------------
# ClaudeRun._build_argv — model/fallback-model selection and --resume.
# ---------------------------------------------------------------------------

def _patch_claude_defaults(monkeypatch):
    monkeypatch.setattr(bridge, "CLAUDE_BIN", "claude", raising=False)
    monkeypatch.setattr(bridge, "CLAUDE_MODEL", None, raising=False)
    monkeypatch.setattr(bridge, "CLAUDE_FALLBACK_MODEL", None, raising=False)
    monkeypatch.setattr(bridge, "CLAUDE_EFFORT", None, raising=False)


def test_claude_run_build_argv_base(monkeypatch):
    _patch_claude_defaults(monkeypatch)
    run = bridge.ClaudeRun("hi", None)
    assert run._build_argv() == [
        "claude", "-p", "hi",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]


def test_claude_run_build_argv_includes_resume_when_state_present(monkeypatch):
    _patch_claude_defaults(monkeypatch)
    run = bridge.ClaudeRun("continue", "abc-123")
    argv = run._build_argv()
    assert argv[-2:] == ["--resume", "abc-123"]


def test_claude_run_build_argv_no_resume_flag_without_state(monkeypatch):
    _patch_claude_defaults(monkeypatch)
    run = bridge.ClaudeRun("hi", None)
    assert "--resume" not in run._build_argv()


def test_claude_run_build_argv_includes_model_when_set(monkeypatch):
    _patch_claude_defaults(monkeypatch)
    monkeypatch.setattr(bridge, "CLAUDE_MODEL", "opus", raising=False)
    argv = bridge.ClaudeRun("hi", None)._build_argv()
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "opus"


def test_claude_run_build_argv_includes_fallback_model_when_set(monkeypatch):
    _patch_claude_defaults(monkeypatch)
    monkeypatch.setattr(bridge, "CLAUDE_FALLBACK_MODEL", "sonnet", raising=False)
    argv = bridge.ClaudeRun("hi", None)._build_argv()
    assert "--fallback-model" in argv
    assert argv[argv.index("--fallback-model") + 1] == "sonnet"


def test_claude_run_build_argv_includes_effort_when_set(monkeypatch):
    _patch_claude_defaults(monkeypatch)
    monkeypatch.setattr(bridge, "CLAUDE_EFFORT", "xhigh", raising=False)
    argv = bridge.ClaudeRun("hi", None)._build_argv()
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_load_config_rejects_bad_claude_effort(monkeypatch):
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "1")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("CLAUDE_EFFORT", "extreme")
    with pytest.raises(SystemExit, match="must be low, medium, high, xhigh, or max"):
        bridge._load_config()


def test_claude_run_build_argv_omits_model_flags_when_unset(monkeypatch):
    _patch_claude_defaults(monkeypatch)
    argv = bridge.ClaudeRun("hi", None)._build_argv()
    assert "--model" not in argv
    assert "--fallback-model" not in argv
    assert "--effort" not in argv


# ---------------------------------------------------------------------------
# Backend selection (make_run) — BACKEND is normally set by _load_config(),
# but tests set the module attribute directly to isolate the factory logic
# from environment parsing.
# ---------------------------------------------------------------------------

def test_make_run_selects_claude_backend(monkeypatch):
    monkeypatch.setattr(bridge, "BACKEND", "claude", raising=False)
    assert isinstance(bridge.make_run("hi", None), bridge.ClaudeRun)


def test_make_run_selects_cli_backend(monkeypatch):
    monkeypatch.setattr(bridge, "BACKEND", "cli", raising=False)
    assert isinstance(bridge.make_run("hi", None), bridge.CLIRun)


def test_make_run_selects_openai_compatible_backend(monkeypatch):
    monkeypatch.setattr(bridge, "BACKEND", "openai_compatible", raising=False)
    assert isinstance(bridge.make_run("hi", None), bridge.OpenAICompatibleRun)


def test_load_config_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("BACKEND", "not-a-real-backend")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "1")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "1")
    with pytest.raises(SystemExit, match="must be claude, cli, or openai_compatible"):
        bridge._load_config()


# ---------------------------------------------------------------------------
# CLIRun — argv templating and finalize logic for the generic agentic-CLI
# adapter (codex, Gemini CLI, or similar). Plaintext-only by design; see the
# class docstring and docs/decisions/0005-pluggable-backends.md.
# ---------------------------------------------------------------------------

def test_cli_run_build_argv_new(monkeypatch):
    monkeypatch.setattr(bridge, "CLI_BIN", "codex", raising=False)
    monkeypatch.setattr(bridge, "CLI_ARGS_NEW", "exec {prompt}", raising=False)
    monkeypatch.setattr(bridge, "CLI_ARGS_RESUME", None, raising=False)
    run = bridge.CLIRun("fix the bug", None)
    assert run._build_argv() == ["codex", "exec", "fix the bug"]


def test_cli_run_build_argv_resume_used_when_state_present(monkeypatch):
    monkeypatch.setattr(bridge, "CLI_BIN", "codex", raising=False)
    monkeypatch.setattr(bridge, "CLI_ARGS_NEW", "exec {prompt}", raising=False)
    monkeypatch.setattr(bridge, "CLI_ARGS_RESUME", "exec resume --last {prompt}", raising=False)
    run = bridge.CLIRun("continue", "1")
    assert run._build_argv() == ["codex", "exec", "resume", "--last", "continue"]


def test_cli_run_build_argv_falls_back_to_new_without_prior_state(monkeypatch):
    monkeypatch.setattr(bridge, "CLI_BIN", "codex", raising=False)
    monkeypatch.setattr(bridge, "CLI_ARGS_NEW", "exec {prompt}", raising=False)
    monkeypatch.setattr(bridge, "CLI_ARGS_RESUME", "exec resume --last {prompt}", raising=False)
    run = bridge.CLIRun("first message", None)
    assert run._build_argv() == ["codex", "exec", "first message"]


def test_cli_run_finalize_sets_truthy_state_when_resume_configured(monkeypatch):
    monkeypatch.setattr(bridge, "CLI_ARGS_RESUME", "exec resume --last {prompt}", raising=False)
    run = bridge.CLIRun("hi", None)
    run.text = "some output\n"
    run._finalize(0)
    assert run.error is None
    assert run.state == "1"


def test_cli_run_finalize_no_state_when_resume_not_configured(monkeypatch):
    monkeypatch.setattr(bridge, "CLI_ARGS_RESUME", None, raising=False)
    run = bridge.CLIRun("hi", None)
    run.text = "some output\n"
    run._finalize(0)
    assert run.error is None
    assert run.state is None


def test_cli_run_finalize_errors_on_empty_output():
    run = bridge.CLIRun("hi", None)
    run.text = "   \n"
    run._finalize(1)
    assert run.error is not None
    assert run.state is None


# ---------------------------------------------------------------------------
# OpenAICompatibleRun's pure helpers — request building and SSE parsing,
# testable without any real network call.
# ---------------------------------------------------------------------------

def test_openai_build_messages_without_system_prompt():
    messages = bridge.openai_build_messages(None, [], "hello")
    assert messages == [{"role": "user", "content": "hello"}]


def test_openai_build_messages_with_system_prompt_and_history():
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first reply"},
    ]
    messages = bridge.openai_build_messages("be terse", history, "second")
    assert messages == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "second"},
    ]


def test_openai_parse_sse_line_extracts_delta():
    line = 'data: {"choices":[{"delta":{"content":"Hel"}}]}'
    assert bridge.openai_parse_sse_line(line) == "Hel"


def test_openai_parse_sse_line_done_sentinel():
    assert bridge.openai_parse_sse_line("data: [DONE]") == "[DONE]"


def test_openai_parse_sse_line_ignores_non_data_lines():
    assert bridge.openai_parse_sse_line("") is None
    assert bridge.openai_parse_sse_line(": keep-alive") is None


def test_openai_parse_sse_line_handles_empty_delta_chunk():
    # A role-only chunk (the first one in a stream) has no "content" key.
    line = 'data: {"choices":[{"delta":{"role":"assistant"}}]}'
    assert bridge.openai_parse_sse_line(line) is None


def test_openai_parse_sse_line_malformed_json_is_ignored():
    assert bridge.openai_parse_sse_line("data: {not json") is None


def test_openai_trim_history_keeps_most_recent():
    history = [{"role": "user", "content": str(i)} for i in range(10)]
    trimmed = bridge.openai_trim_history(history, 4)
    assert trimmed == history[-4:]


def test_openai_trim_history_noop_under_limit():
    history = [{"role": "user", "content": "one"}]
    assert bridge.openai_trim_history(history, 40) == history
