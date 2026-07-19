"""Configuration model + loading.

Config comes from a YAML file (``--config``). Secrets are *never* read from YAML;
they come from the process environment (systemd ``EnvironmentFile`` pattern).
Each env-backed secret is referenced by name here and resolved at use-time so a
``check-config`` run can report exactly which secrets are missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------- #
# Env-backed secret names (documented; values live only in the environment).
# --------------------------------------------------------------------------- #
ENV_DISCORD_WEBHOOK = "DISCORD_WEBHOOK_URL"
ENV_TWITCH_CLIENT_ID = "TWITCH_CLIENT_ID"
ENV_TWITCH_CLIENT_SECRET = "TWITCH_CLIENT_SECRET"
ENV_TWITCH_BOT_TOKEN = "TWITCH_BOT_TOKEN"  # user access token (websocket EventSub needs a user token)
ENV_TWITCH_BOT_REFRESH = "TWITCH_BOT_REFRESH"  # matching refresh token
ENV_GROQ_API_KEY = "GROQ_API_KEY"
ENV_NTFY_TOKEN = "NTFY_TOKEN"
ENV_CLAUDE_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"


class ConfigError(ValueError):
    """Raised when the config file is structurally invalid."""


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class DiscordConfig:
    # Per-purpose webhook overrides. Empty => fall back to env DISCORD_WEBHOOK_URL.
    default_webhook_env: str = ENV_DISCORD_WEBHOOK
    announce_webhook: str = ""
    update_webhook: str = ""
    digest_webhook: str = ""
    username: str = "watchtower"
    # Discord embed description hard cap is 4096; we stay well under it.
    max_description_chars: int = 3800


@dataclass
class SttConfig:
    backend: str = "whispercpp"  # whispercpp | groq
    # whispercpp
    whisper_cli: str = "whisper-cli"  # path to whisper.cpp binary
    whisper_model: str = "/opt/whisper/models/ggml-base.en.bin"
    whisper_threads: int = 2
    # groq
    groq_model: str = "whisper-large-v3-turbo"
    groq_api_key_env: str = ENV_GROQ_API_KEY
    # If transcription falls this many seconds behind capture, warn & maybe skip.
    lag_warn_seconds: int = 180
    skip_when_lagging: bool = False
    # Hard timeout for a single transcription subprocess. 0 => derived at load as
    # max(120, 4 * capture.segment_seconds) so a wedged whisper-cli can't hang the
    # transcribe loop forever.
    chunk_timeout_seconds: int = 0


@dataclass
class LlmConfig:
    backend: str = "claude_cli"  # claude_cli | codex_cli | none
    model: str = "haiku"  # claude alias (haiku/sonnet/opus) or codex model id
    # Reasoning effort passed to the claude CLI (--effort). Only some models
    # accept it (sonnet/opus tiers); leave empty for haiku.
    effort: str = ""
    # Optional stronger model for final/refined digests only (empty = use `model`).
    # Rolling updates are frequent and cheap; digests happen once or twice per
    # stream and benefit from a better writer (validated: haiku suffices live,
    # sonnet/opus digests are noticeably richer).
    digest_model: str = ""
    # Effort for the digest model (empty = CLI default).
    digest_effort: str = ""
    binary: str = ""  # default resolved per-backend (claude / codex on PATH)
    timeout_seconds: int = 120
    # Dedicated empty working directory used as the cwd for the LLM CLI subprocess.
    # Empty => resolved to <state_dir>/llm at load time. Keeping the CLI's cwd an
    # isolated empty dir (plus --disallowedTools "*") contains prompt-injection: a
    # malicious transcript/chat line can't steer the agent into reading repo files.
    workdir: str = ""
    # extra system-prompt style guidance appended to every summarize prompt
    style: str = "Be concise and factual. Prefer bullet points. Never invent details."


@dataclass
class CaptureConfig:
    workdir: str = "/var/lib/watchtower/work"
    segment_seconds: int = 60
    sample_rate: int = 16000
    keep_chunks: bool = False  # keep WAV chunks after transcription (debug)
    yt_dlp: str = "yt-dlp"
    streamlink: str = "streamlink"
    ffmpeg: str = "ffmpeg"
    # Proxy for yt-dlp YouTube operations (live capture, VOD caption/audio fetch).
    # YouTube bot-checks media requests from datacenter IPs; routing them through
    # a residential egress on the tailnet (e.g. socks5://<mac-tailnet-ip>:1080)
    # avoids that. Empty = direct. Twitch/streamlink is unaffected (not blocked).
    proxy: str = ""
    # Netscape cookies.txt for YouTube (throwaway Google account). On datacenter
    # IPs YouTube's bot-wall requires a logged-in session even with PO tokens.
    # File should be readable only by the service user. Empty = no cookies.
    cookies_file: str = ""
    # Restart backoff for the capture subprocess while the stream is still live.
    restart_backoff_seconds: int = 5
    max_restart_backoff_seconds: int = 60


@dataclass
class RefinedDigestConfig:
    enabled: bool = True
    delay_minutes: int = 30  # wait after stream end before pulling VOD captions
    retry_delay_minutes: int = 30  # single retry if captions not ready yet


@dataclass
class NtfyConfig:
    enabled: bool = False
    url: str = ""  # e.g. https://ntfy.example.ts.net/alerts
    token_env: str = ENV_NTFY_TOKEN
    # Alert after this many consecutive capture/transcribe/llm failures.
    failure_threshold: int = 3


@dataclass
class HealthConfig:
    heartbeat_file: str = "/var/lib/watchtower/heartbeat"
    heartbeat_interval_seconds: int = 30
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)


@dataclass
class WatchTarget:
    platform: str  # youtube | twitch
    handle: str  # YouTube @handle (without @) or Twitch login name
    name: str = ""  # friendly display name; defaults to handle
    # Per-channel overrides (None => use global default).
    poll_interval_minutes: int | None = None
    update_interval_minutes: int | None = None
    enabled: bool = True

    def display(self) -> str:
        return self.name or self.handle


@dataclass
class Config:
    state_db: str = "/var/lib/watchtower/watchtower.db"
    poll_interval_minutes: int = 10  # YouTube live-page polling
    update_interval_minutes: int = 15  # rolling summary cadence
    # Consecutive not-live poll results required before a YouTube stream is
    # declared offline. Fetch errors (429/timeout/non-200) never count, so a
    # transient blip can't trigger a premature digest + duplicate re-announce.
    youtube_offline_confirmations: int = 2
    # Max buffered chat messages per session before drop-oldest kicks in.
    chat_queue_max: int = 1000
    # Delete chat/transcript/link rows for streams ended more than this many days
    # ago. Pruned at startup and daily. 0 disables retention pruning.
    retention_days: int = 30
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    refined_digest: RefinedDigestConfig = field(default_factory=RefinedDigestConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    # Twitch app creds are env-backed; these name the env vars.
    twitch_client_id_env: str = ENV_TWITCH_CLIENT_ID
    twitch_client_secret_env: str = ENV_TWITCH_CLIENT_SECRET
    twitch_bot_token_env: str = ENV_TWITCH_BOT_TOKEN
    twitch_bot_refresh_env: str = ENV_TWITCH_BOT_REFRESH
    # Optional external YouTube-chat NDJSON emitter; absent => no YT chat.
    youtube_chat_binary: str = ""
    watch: list[WatchTarget] = field(default_factory=list)

    # ---- per-target resolution helpers --------------------------------- #
    def poll_interval_for(self, t: WatchTarget) -> int:
        return t.poll_interval_minutes or self.poll_interval_minutes

    def update_interval_for(self, t: WatchTarget) -> int:
        return t.update_interval_minutes or self.update_interval_minutes

    # ---- derived paths / timeouts -------------------------------------- #
    @property
    def state_dir(self) -> Path:
        return Path(self.state_db).parent

    def llm_workdir(self) -> Path:
        return Path(self.llm.workdir) if self.llm.workdir else self.state_dir / "llm"

    def subprocess_timeout(self) -> int:
        """Bounded timeout (s) for one-shot media subprocesses (whisper, caption/
        audio fetch): 4x a capture segment, never below 120s."""
        return self.stt.chunk_timeout_seconds or max(120, 4 * self.capture.segment_seconds)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _build(cls: type, data: Any):
    """Recursively build a (possibly nested) dataclass from plain dict data.

    Unknown keys raise ConfigError so typos in the YAML are caught early.
    """
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"expected a mapping for {cls.__name__}, got {type(data).__name__}")
    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"unknown config key '{key}' in {cls.__name__}")
        f = known[key]
        ftype = f.type
        # Resolve nested dataclasses declared via default_factory.
        nested = _nested_dataclass(cls, f.name)
        if nested is not None:
            kwargs[key] = _build(nested, value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def _nested_dataclass(parent: type, field_name: str) -> type | None:
    """Return the dataclass type for a nested field, if any."""
    mapping = {
        ("Config", "discord"): DiscordConfig,
        ("Config", "stt"): SttConfig,
        ("Config", "llm"): LlmConfig,
        ("Config", "capture"): CaptureConfig,
        ("Config", "refined_digest"): RefinedDigestConfig,
        ("Config", "health"): HealthConfig,
        ("HealthConfig", "ntfy"): NtfyConfig,
    }
    return mapping.get((parent.__name__, field_name))


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError("top-level config must be a mapping")

    watch_raw = raw.pop("watch", []) or []
    cfg = _build(Config, raw)

    targets: list[WatchTarget] = []
    if not isinstance(watch_raw, list):
        raise ConfigError("'watch' must be a list")
    for i, entry in enumerate(watch_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"watch[{i}] must be a mapping")
        unknown = set(entry) - {f.name for f in fields(WatchTarget)}
        if unknown:
            raise ConfigError(f"watch[{i}] has unknown keys: {sorted(unknown)}")
        if "platform" not in entry or "handle" not in entry:
            raise ConfigError(f"watch[{i}] requires 'platform' and 'handle'")
        if entry["platform"] not in ("youtube", "twitch"):
            raise ConfigError(f"watch[{i}] platform must be youtube|twitch, got {entry['platform']!r}")
        targets.append(WatchTarget(**entry))
    cfg.watch = targets

    validate(cfg)

    # Resolve derived defaults so downstream code (which only sees the sub-config)
    # gets concrete values.
    if not cfg.llm.workdir:
        cfg.llm.workdir = str(cfg.state_dir / "llm")
    if not cfg.stt.chunk_timeout_seconds:
        cfg.stt.chunk_timeout_seconds = max(120, 4 * cfg.capture.segment_seconds)

    return cfg


def validate(cfg: Config) -> None:
    """Structural validation (does not touch the environment)."""
    if cfg.stt.backend not in ("whispercpp", "groq"):
        raise ConfigError(f"stt.backend must be whispercpp|groq, got {cfg.stt.backend!r}")
    if cfg.llm.backend not in ("claude_cli", "codex_cli", "none"):
        raise ConfigError(f"llm.backend must be claude_cli|codex_cli|none, got {cfg.llm.backend!r}")
    if cfg.poll_interval_minutes <= 0 or cfg.update_interval_minutes <= 0:
        raise ConfigError("intervals must be positive")
    if cfg.capture.segment_seconds <= 0:
        raise ConfigError("capture.segment_seconds must be positive")


def check_secrets(cfg: Config) -> list[str]:
    """Return a list of human-readable warnings about missing env secrets.

    Empty list => everything the selected backends need is present.
    """
    warnings: list[str] = []

    def missing(env_name: str) -> bool:
        return not os.environ.get(env_name)

    # Discord: at least the default webhook, unless every purpose is overridden in YAML.
    if missing(cfg.discord.default_webhook_env) and not all(
        (cfg.discord.announce_webhook, cfg.discord.update_webhook, cfg.discord.digest_webhook)
    ):
        warnings.append(f"${cfg.discord.default_webhook_env} not set and no full per-purpose webhook overrides")

    has_twitch = any(t.platform == "twitch" and t.enabled for t in cfg.watch)
    if has_twitch:
        for env_name in (cfg.twitch_client_id_env, cfg.twitch_client_secret_env):
            if missing(env_name):
                warnings.append(f"twitch target configured but ${env_name} not set")
        if missing(cfg.twitch_bot_token_env):
            warnings.append(
                f"${cfg.twitch_bot_token_env} not set: websocket EventSub requires a user access token"
            )

    if cfg.stt.backend == "groq" and missing(cfg.stt.groq_api_key_env):
        warnings.append(f"stt.backend=groq but ${cfg.stt.groq_api_key_env} not set")

    if cfg.llm.backend == "claude_cli" and missing(ENV_CLAUDE_OAUTH_TOKEN):
        warnings.append(
            f"llm.backend=claude_cli but ${ENV_CLAUDE_OAUTH_TOKEN} not set "
            "(claude CLI will use whatever auth is on the box)"
        )

    if cfg.health.ntfy.enabled and missing(cfg.health.ntfy.token_env):
        warnings.append(f"health.ntfy.enabled but ${cfg.health.ntfy.token_env} not set")

    return warnings


# Silence "unused" linters for is_dataclass import kept for future extension.
assert is_dataclass(Config)
