from __future__ import annotations

import asyncio

import pytest

from watchtower.config import LlmConfig
from watchtower.llm import build_llm
from watchtower.llm.claude_cli import ClaudeCliBackend
from watchtower.llm.claude_cli import build_argv as claude_argv
from watchtower.llm.codex_cli import CodexCliBackend
from watchtower.llm.codex_cli import build_argv as codex_argv


# --- pure argv construction (verified flags) --------------------------------
def test_claude_argv_verified_flags():
    argv = claude_argv("claude", "haiku")
    assert argv[:6] == ["claude", "-p", "--model", "haiku", "--output-format", "text"]


def test_claude_argv_prompt_injection_restrictions():
    # C1: the headless agent must be locked down for untrusted piped input.
    argv = claude_argv("claude", "haiku")
    assert "--disallowedTools" in argv
    i = argv.index("--disallowedTools")
    assert argv[i + 1] == "*"
    assert "--max-turns" in argv
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_claude_argv_custom_binary_and_model():
    argv = claude_argv("/opt/claude", "sonnet")
    assert argv[0] == "/opt/claude"
    assert "sonnet" in argv


def test_codex_argv_verified_flags():
    argv = codex_argv("codex", "gpt-5-codex")
    assert argv[:2] == ["codex", "exec"]
    assert "--sandbox" in argv and "read-only" in argv
    assert "--skip-git-repo-check" in argv
    assert argv[-1] == "-"  # prompt read from stdin
    assert "--model" in argv and "gpt-5-codex" in argv


def test_codex_argv_no_model_omits_flag():
    argv = codex_argv("codex", "")
    assert "--model" not in argv
    assert argv[-1] == "-"


def test_codex_argv_cd_confines_to_workdir():
    # H1: Codex must be confined to the dedicated empty LLM dir.
    argv = codex_argv("codex", "gpt-5", "/var/lib/watchtower/llm")
    assert "--cd" in argv
    assert argv[argv.index("--cd") + 1] == "/var/lib/watchtower/llm"
    argv_no = codex_argv("codex", "gpt-5")
    assert "--cd" not in argv_no


def test_build_llm_dispatch():
    assert build_llm(LlmConfig(backend="claude_cli")).name == "claude_cli"
    assert build_llm(LlmConfig(backend="codex_cli")).name == "codex_cli"
    assert build_llm(LlmConfig(backend="none")).name == "none"


# --- subprocess mocking: assert correct argv + stdin, handle failures --------
class FakeProc:
    def __init__(self, out=b"the summary", err=b"", rc=0, hang=False):
        self._out, self._err, self.returncode, self._hang = out, err, rc, hang
        self.killed = False

    async def communicate(self, input=None):
        FakeProc.last_input = input
        if self._hang:
            await asyncio.sleep(3600)
        return self._out, self._err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def make_spy(**proc_kwargs):
    calls = {}

    async def fake_exec(*argv, stdin=None, stdout=None, stderr=None, cwd=None, env=None):
        calls["argv"] = list(argv)
        calls["cwd"] = cwd
        return FakeProc(**proc_kwargs)

    return fake_exec, calls


@pytest.mark.asyncio
async def test_claude_summarize_success(monkeypatch):
    fake_exec, calls = make_spy(out=b"  bullet summary  ")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    be = ClaudeCliBackend(LlmConfig(backend="claude_cli", model="haiku", workdir="/tmp/llm"))
    res = await be.summarize("PROMPT TEXT")
    assert res.ok is True
    assert res.text == "bullet summary"
    # Restricted argv is asserted in the pure-argv tests above; here confirm the
    # backend passes it through plus the isolated cwd.
    assert calls["argv"][:6] == ["claude", "-p", "--model", "haiku", "--output-format", "text"]
    assert "--disallowedTools" in calls["argv"]
    assert calls["cwd"] == "/tmp/llm"
    assert FakeProc.last_input == b"PROMPT TEXT"


@pytest.mark.asyncio
async def test_codex_summarize_success(monkeypatch):
    fake_exec, calls = make_spy(out=b"digest")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    be = CodexCliBackend(LlmConfig(backend="codex_cli", model="gpt-5", workdir="/tmp/llm"))
    res = await be.summarize("P")
    assert res.ok is True and res.text == "digest"
    assert calls["argv"][:2] == ["codex", "exec"]
    assert calls["argv"][-1] == "-"
    assert "--cd" in calls["argv"]
    assert calls["cwd"] == "/tmp/llm"


@pytest.mark.asyncio
async def test_llm_nonzero_exit_returns_not_ok(monkeypatch):
    fake_exec, _ = make_spy(out=b"", err=b"boom", rc=1)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    be = ClaudeCliBackend(LlmConfig(backend="claude_cli"))
    res = await be.summarize("P")
    assert res.ok is False
    assert "boom" in res.error


@pytest.mark.asyncio
async def test_llm_timeout_returns_not_ok(monkeypatch):
    fake_exec, _ = make_spy(hang=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    be = ClaudeCliBackend(LlmConfig(backend="claude_cli", timeout_seconds=0))
    res = await be.summarize("P")
    assert res.ok is False
    assert "timeout" in res.error


@pytest.mark.asyncio
async def test_llm_missing_binary_returns_not_ok(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("no claude")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    be = ClaudeCliBackend(LlmConfig(backend="claude_cli"))
    res = await be.summarize("P")
    assert res.ok is False and "not found" in res.error


@pytest.mark.asyncio
async def test_none_backend_signals_fallback():
    from watchtower.llm.none import NoneBackend

    res = await NoneBackend(LlmConfig(backend="none")).summarize("P")
    assert res.ok is False


def test_claude_argv_effort_flag():
    argv = claude_argv("claude", "sonnet", "high")
    assert argv[argv.index("--effort") + 1] == "high"


def test_claude_argv_no_effort_by_default():
    assert "--effort" not in claude_argv("claude", "haiku")
