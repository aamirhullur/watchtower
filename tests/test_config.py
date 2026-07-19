from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from watchtower.config import ConfigError, check_secrets, load_config


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_example_config_loads():
    cfg = load_config(Path(__file__).parents[1] / "config" / "config.example.yaml")
    assert cfg.stt.backend == "whispercpp"
    assert cfg.llm.backend == "claude_cli"
    assert cfg.llm.model == "haiku"
    platforms = {t.platform for t in cfg.watch}
    assert platforms == {"youtube", "twitch"}
    # per-target override resolution
    target = next(t for t in cfg.watch if t.name == "Some Channel")
    assert cfg.poll_interval_for(target) == 5
    assert cfg.update_interval_for(target) == cfg.update_interval_minutes


def test_defaults_when_minimal(tmp_path):
    cfg = load_config(write(tmp_path, "watch: []\n"))
    assert cfg.poll_interval_minutes == 10
    assert cfg.update_interval_minutes == 15
    assert cfg.watch == []


def test_unknown_top_level_key_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "bogus_key: 1\nwatch: []\n"))


def test_unknown_nested_key_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "stt:\n  nope: 1\nwatch: []\n"))


def test_bad_llm_backend_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "llm:\n  backend: gpt5\nwatch: []\n"))


def test_watch_requires_platform_and_handle(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "watch:\n  - handle: x\n"))


def test_watch_bad_platform_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "watch:\n  - platform: tiktok\n    handle: x\n"))


def test_check_secrets_reports_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    cfg = load_config(
        write(
            tmp_path,
            """
            llm:
              backend: none
            watch:
              - platform: twitch
                handle: someone
            """,
        )
    )
    warnings = check_secrets(cfg)
    joined = "\n".join(warnings)
    assert "DISCORD_WEBHOOK_URL" in joined
    assert "TWITCH_CLIENT_ID" in joined


def test_check_secrets_clean_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    cfg = load_config(
        write(
            tmp_path,
            """
            llm:
              backend: none
            watch:
              - platform: youtube
                handle: "@x"
            """,
        )
    )
    assert check_secrets(cfg) == []
