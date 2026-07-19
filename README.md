# watchtower

A self-hosted information-gathering daemon. Point it at sources you care about
and it turns them into structured, searchable knowledge: a permanent SQLite
database of transcripts, chat, links, summaries, and 🔎 finds (products, tools,
ideas worth knowing about). That store is the product: plain SQLite, queryable
by anything. Notifications (rolling updates while live, digests, and finds
recaps) currently ship to Discord via webhooks, but Discord is only the current
delivery adapter over the store.

The first source module is **livestreams** (YouTube + Twitch): it detects
go-live, captures audio + chat, transcribes locally, and summarizes while the
stream is still running. The source architecture is pluggable so other kinds
of feeds can be added later.

Built to run unattended on a small ARM64 VM (Ubuntu 24.04, 2 cores / 12 GB)
behind Tailscale, outbound-only, as a hardened non-root systemd service.

## What it does

- **Detects go-live**
  - YouTube: polls `https://www.youtube.com/@<handle>/live` with a browser UA and
    parses the page for `isLive` + the canonical watch URL. No API key.
  - Twitch: EventSub over WebSocket via TwitchIO v3 (`stream.online` /
    `stream.offline`). Websocket transport needs no public callback URL.
- **Captures** the live audio (`yt-dlp` for YouTube, `streamlink` for Twitch)
  piped into `ffmpeg -f segment` → rolling 60s mono 16 kHz WAV chunks. The
  capture subprocess is supervised and restarted on crash while still live.
- **Transcribes** each chunk locally with **whisper.cpp** (`whisper-cli`), or
  optionally **Groq** Whisper as a fallback.
- **Ingests chat**: Twitch via TwitchIO; YouTube via an optional external
  NDJSON-emitting binary (absent → transcript-only). URLs are extracted into a
  `links` table.
- **Summarizes** every ~15 min while live and posts a rolling Discord update; on
  stream end it posts a final digest (topics, timeline, all product/tool links).
  Long transcripts are map-reduce condensed first so a 4 h stream's digest sees
  the whole stream, not the first 40 min. Summaries come from a pluggable
  **LLM backend**:
  - `claude_cli`: headless Claude Code CLI, locked down for untrusted input
    (`claude -p --model haiku --output-format text --disallowedTools "*" --max-turns 1 --setting-sources ""`, run in an isolated empty cwd)
  - `codex_cli`: headless OpenAI Codex CLI (`codex exec --model … -`)
  - `none`: stats + links only.
  A cheap model handles rolling updates while `llm.digest_model` (+
  `digest_effort`) can route digests to a stronger one. If the LLM times out or
  fails, the update still goes out as a stats-only post.
- **🔎 Finds**: the discovery layer. Each window gets a second cheap-LLM pass
  extracting concrete discoverables (products, tools, games, benchmarks,
  recommendations) as structured JSON: stored forever in a `finds` table,
  surfaced on every rolling update (top 5, with YouTube `&t=` deep links), and
  recapped as a standalone deduped message after the final digest. The point
  is learning about things like a "GMKtec K8 Plus" without watching the stream.
- **Refined digest** (YouTube): ~30 min after a stream ends it pulls the VOD
  auto-captions (`yt-dlp --write-auto-subs`), which are far cleaner than live STT,
  regenerates the digest, and reposts it marked *refined*.
- **Health**: heartbeat file for an external watchdog + optional ntfy alerts on
  capture/transcribe/LLM crash-loops.
- **YouTube from a datacenter IP**: YouTube bot-walls media requests from VPS
  ranges. watchtower tunnels yt-dlp traffic through **Cloudflare WARP** via
  [wireproxy](https://github.com/whyvl/wireproxy) (free, userspace, no root, no
  cookies, no Google account). Set `capture.proxy: http://127.0.0.1:25345`.
  See `deploy/install.md`. Validated on metadata, VOD captions, VOD media and
  live HLS capture. Twitch needs no proxy.

## Architecture

```
                                  ┌──────────────────────────────────────────┐
                                  │              watchtower (1 asyncio proc) │
                                  │                                            │
   YouTube /live poll ─┐          │  ┌────────────┐   go-live   ┌───────────┐ │
                       ├─ detect ─┼─▶│  Detectors │────────────▶│  Stream   │ │
   Twitch EventSub ────┘          │  └────────────┘             │  Session  │ │
        (websocket)               │                             │ (per live)│ │
                                  │                             └─────┬─────┘ │
                                  │        ┌──────────────────────────┼─────┐ │
                                  │        ▼            ▼             ▼     ▼ │
                                  │   ┌─────────┐ ┌──────────┐ ┌────────┐ ┌─────────┐
   yt-dlp/streamlink │ ffmpeg ───▶│   │ Capture │ │Transcribe│ │  Chat  │ │Summarize│
        60s WAV segments          │   │ supervis│ │whisper.cpp│ │ ingest │ │  loop   │
                                  │   └────┬────┘ └────┬─────┘ └───┬────┘ └────┬────┘
                                  │        │ chunks    │ text      │ msgs      │ prompt
                                  │        └───────────┴───────────┴──────┐    ▼
                                  │                                       ▼  ┌──────────┐
                                  │                             ┌──────────┐ │   LLM    │
                                  │                             │  SQLite  │ │ backend  │
                                  │                             │  state   │ │(claude / │
                                  │                             └──────────┘ │ codex /  │
                                  │                                          │  none)   │
                                  │   ┌─────────┐   embeds                   └────┬─────┘
                                  │   │ Health  │        ┌──────────────────┐     │
                                  │   │heartbeat│        │  Discord poster  │◀────┘
                                  │   │ + ntfy  │        │  (webhooks, 429) │
                                  │   └─────────┘        └────────┬─────────┘
                                  └───────────────────────────────┼───────────┘
                                                                  ▼
                                go-live · rolling update (+finds) · final · finds recap · refined
```

## Quickstart (local dev)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[twitch,dev]'          # drop [twitch] for YouTube-only

# Validate a config and see which env secrets are missing:
watchtower check-config --config config/config.example.yaml

# Post a hello embed (needs $DISCORD_WEBHOOK_URL):
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
watchtower test-webhook --config config/config.example.yaml

# End-to-end acceptance test: run the FULL pipeline on a local file or VOD URL,
# printing updates instead of posting:
watchtower simulate --config config/config.example.yaml --dry-run some_talk.mp4
watchtower simulate --config config/config.example.yaml --dry-run \
  'https://www.youtube.com/watch?v=VIDEO_ID'

# Run the daemon:
watchtower run --config config/config.example.yaml
```

`simulate` chunks the input faster-than-realtime and treats every couple of
minutes of audio as one "window", so you can exercise capture → transcribe →
summarize → digest in seconds without a live stream. Tune with `--window-chunks`.

## Configuration

Everything non-secret lives in the YAML (`--config`); see the fully-commented
[`config/config.example.yaml`](config/config.example.yaml). Highlights:

| Key | Meaning |
| --- | --- |
| `poll_interval_minutes` | YouTube `/live` poll cadence (per-target override allowed) |
| `update_interval_minutes` | Rolling summary cadence while live |
| `stt.backend` | `whispercpp` \| `groq` |
| `stt.whisper_cli` / `whisper_model` | Paths to the whisper.cpp binary + GGML model |
| `llm.backend` | `claude_cli` \| `codex_cli` \| `none` |
| `llm.model` | Claude alias (`haiku`/`sonnet`/`opus`) or Codex model id |
| `llm.digest_model` / `digest_effort` | Optional stronger model + effort for digests only |
| `capture.segment_seconds` | Chunk length (default 60) |
| `capture.proxy` | HTTP proxy for YouTube (wireproxy/WARP; see install.md) |
| `refined_digest.*` | YouTube VOD-caption re-digest timing |
| `health.ntfy.*` | Optional push alerts on crash-loops |
| `watch:` | List of channels (`platform`, `handle`, per-channel overrides) |

**Secrets are never read from YAML.** They come from the environment (systemd
`EnvironmentFile`): `DISCORD_WEBHOOK_URL`, `TWITCH_CLIENT_ID` /
`TWITCH_CLIENT_SECRET` / `TWITCH_BOT_TOKEN` / `TWITCH_BOT_REFRESH`,
`GROQ_API_KEY`, `NTFY_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`. The YAML only names the
env vars.

## State

SQLite (`state_db`) with tables: `streams`, `transcript_chunks`, `chat_messages`,
`links`, `finds`, `updates_posted`. With `retention_days: 0` everything is kept
forever (a 4 h stream ≈ 1.6 MB). The transcript + chat + finds corpus is a
deliberate long-term asset.

## Deployment

See [`deploy/install.md`](deploy/install.md) and the hardened
[`deploy/watchtower.service`](deploy/watchtower.service) +
[`deploy/wireproxy.service`](deploy/wireproxy.service) units.

## Tests

```bash
python -m pytest
```

No network required (118 tests): config parsing, window assembly/dedup, YouTube
live-page parsing (HTML fixtures), whisper output parsing, VTT caption parsing,
LLM backend argv construction, digest map-reduce condensation, finds
parsing/dedup/deep-links, and Discord embed building.
