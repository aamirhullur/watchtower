# Deploying streamwatch on Ubuntu 24.04 ARM64

Target: a 2-core / 12 GB `aarch64` VM, Python 3.12, running as a dedicated
non-root systemd user, outbound-only behind Tailscale.

## 1. System dependencies

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv ffmpeg
# Downloaders:
sudo apt install -y pipx
pipx install yt-dlp
pipx install streamlink            # only if you watch Twitch
# (or install yt-dlp/streamlink into the service venv instead — see step 4)
```

### whisper.cpp (STT)

Build the ARM64 binary + fetch a model:

```bash
sudo mkdir -p /opt/whisper/bin /opt/whisper/models
cd /tmp && git clone https://github.com/ggml-org/whisper.cpp
cd whisper.cpp && cmake -B build && cmake --build build --config Release -j2
# whisper-cli's RUNPATH points at the build dir (hidden from the service by
# ProtectHome), so co-locate the shared libs; the unit sets
# LD_LIBRARY_PATH=/opt/whisper/bin.
sudo cp build/bin/whisper-cli build/bin/*.so* /opt/whisper/bin/
sudo bash ./models/download-ggml-model.sh base.en-q5_1 /opt/whisper/models
```

Point `stt.whisper_cli` and `stt.whisper_model` in the config at these paths
(model: `ggml-base.en-q5_1.bin` — benchmarked 4.6x realtime / 14.2% WER on
2×Ampere Altra, beating small.en on both axes).

### YouTube egress (Cloudflare WARP via wireproxy)

YouTube bot-walls media requests from datacenter IPs. Fix: tunnel yt-dlp's
traffic through Cloudflare WARP (free, no account) with a userspace WireGuard
proxy. No cookies, no Google account, no root, no tun device.

```bash
# 1. Generate a WARP profile with wgcf. NOTE: Cloudflare 429s registration from
#    datacenter IPs — run register/generate on any other machine and copy the
#    two files over if needed.
wgcf register --accept-tos && wgcf generate     # -> wgcf-profile.conf
sudo install -m 0640 -o root -g streamwatch wgcf-profile.conf /etc/streamwatch/
# 2. Install the wireproxy binary (github.com/whyvl/wireproxy, linux_arm64).
sudo install -m 0755 wireproxy /opt/wireproxy/wireproxy
# 3. Config: loopback SOCKS5 :25344 + HTTP CONNECT :25345.
sudo tee /etc/streamwatch/wireproxy.conf >/dev/null <<'EOF'
WGConfig = /etc/streamwatch/wgcf-profile.conf

[Socks5]
BindAddress = 127.0.0.1:25344

[http]
BindAddress = 127.0.0.1:25345
EOF
sudo chown root:streamwatch /etc/streamwatch/wireproxy.conf
sudo chmod 640 /etc/streamwatch/wireproxy.conf
sudo cp deploy/wireproxy.service /etc/systemd/system/
sudo systemctl enable --now wireproxy
curl -x http://127.0.0.1:25345 https://www.cloudflare.com/cdn-cgi/trace | grep warp=on
```

Set `capture.proxy: http://127.0.0.1:25345` in the config — the **HTTP**
listener, not SOCKS5, because yt-dlp delegates live-HLS downloads to ffmpeg,
which only honors HTTP proxies. Twitch/streamlink stays unproxied.

### LLM CLI (choose one, or use `llm.backend: none`)

- **Claude Code** (`llm.backend: claude_cli`): install the CLI and generate a
  long-lived OAuth token; put it in the env file as `CLAUDE_CODE_OAUTH_TOKEN`.
- **Codex** (`llm.backend: codex_cli`): install the Codex CLI and authenticate;
  Codex reads its own config/credentials from `$HOME` (the unit sets
  `HOME=/var/lib/streamwatch`).

## 2. Service user

```bash
sudo useradd --system --home-dir /var/lib/streamwatch --create-home \
  --shell /usr/sbin/nologin streamwatch
sudo mkdir -p /var/lib/streamwatch/work
sudo chown -R streamwatch:streamwatch /var/lib/streamwatch
```

## 3. Push the code

From your workstation:

```bash
rsync -av --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
  ./streamwatch/ deploy-host:/opt/streamwatch/src/
```

## 4. Virtualenv (as the service user)

```bash
sudo -u streamwatch python3.12 -m venv /opt/streamwatch/venv
sudo -u streamwatch /opt/streamwatch/venv/bin/pip install --upgrade pip
# Core + Twitch extra (drop [twitch] for YouTube-only):
sudo -u streamwatch /opt/streamwatch/venv/bin/pip install '/opt/streamwatch/src[twitch]'
# Optional: keep yt-dlp/streamlink in the venv so upgrades are self-contained:
sudo -u streamwatch /opt/streamwatch/venv/bin/pip install yt-dlp streamlink
```

## 5. Config + secrets

```bash
sudo mkdir -p /etc/streamwatch
sudo cp /opt/streamwatch/src/config/config.example.yaml /etc/streamwatch/config.yaml
sudo $EDITOR /etc/streamwatch/config.yaml
```

Create the secrets env file (**root-owned, `0600`**):

```bash
sudo tee /etc/streamwatch/env >/dev/null <<'EOF'
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy
# Twitch (only if watching Twitch). Websocket EventSub needs a USER token:
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
TWITCH_BOT_TOKEN=...
TWITCH_BOT_REFRESH=...
# Optional:
GROQ_API_KEY=...
NTFY_TOKEN=...
CLAUDE_CODE_OAUTH_TOKEN=...
EOF
sudo chmod 600 /etc/streamwatch/env
sudo chown root:root /etc/streamwatch/env
```

> **Why `0600 root:root` and not `0640 root:streamwatch`?** systemd reads
> `EnvironmentFile=` as **root** and injects the values into the service's
> environment itself — the `streamwatch` user never needs to read the file. Making
> it group-readable by `streamwatch` would let a compromised service process (or
> anything it spawns) read every secret straight off disk. Keep it root-only.

### One-time Twitch user-token bootstrap

Websocket EventSub rejects app tokens. Generate a **user** access token +
refresh token for your bot account (scopes: at minimum `user:read:chat` for chat;
`stream.online`/`stream.offline` EventSub subscriptions themselves need no extra
scope beyond a valid user token) using the Twitch CLI or an OAuth flow, and store
them as `TWITCH_BOT_TOKEN` / `TWITCH_BOT_REFRESH`. TwitchIO refreshes the token
automatically thereafter. (Verify TwitchIO 3.x's exact scope/token handling
against your installed version before first run.)

## 6. Validate before starting

Load the secrets inside a **root subshell** (`set -a; . env`) and drop to the
service user with `runuser`. This keeps secrets out of any process's argv — the
`env $(cat …)` form exposes every secret in `/proc/<pid>/cmdline`, readable by any
local user.

```bash
sudo bash -c '
  set -a
  . /etc/streamwatch/env
  set +a
  runuser -u streamwatch -- \
    /opt/streamwatch/venv/bin/streamwatch check-config --config /etc/streamwatch/config.yaml
'
# Post a hello embed (same pattern):
sudo bash -c '
  set -a
  . /etc/streamwatch/env
  set +a
  runuser -u streamwatch -- \
    /opt/streamwatch/venv/bin/streamwatch test-webhook --config /etc/streamwatch/config.yaml
'
```

## 7. Install + start the unit

```bash
sudo cp /opt/streamwatch/src/deploy/streamwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now streamwatch     # pulls in wireproxy.service via Wants=
journalctl -u streamwatch -f
```

## 8. End-to-end acceptance test

Run the full pipeline against a real VOD without waiting for a live stream:

```bash
sudo bash -c '
  set -a
  . /etc/streamwatch/env
  set +a
  runuser -u streamwatch -- \
    /opt/streamwatch/venv/bin/streamwatch simulate --config /etc/streamwatch/config.yaml \
    "https://www.youtube.com/watch?v=SOME_VOD_ID"
'
# Add --dry-run to print updates instead of posting to Discord.
```

## Upgrades

```bash
rsync ... ; sudo -u streamwatch /opt/streamwatch/venv/bin/pip install --upgrade '/opt/streamwatch/src[twitch]'
sudo systemctl restart streamwatch   # SIGTERM => graceful capture shutdown
```
