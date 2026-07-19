"""streamwatch CLI.

Subcommands:
  run           Run the daemon (detect, capture, transcribe, summarize, post).
  check-config  Validate a config file and report missing env secrets.
  test-webhook  Post a hello embed to the configured Discord webhook.
  simulate      Run the full pipeline against a local file or YouTube VOD URL
                (no live stream needed) — the end-to-end acceptance test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TextIO

import aiohttp

from .app import run_app
from .config import Config, ConfigError, check_secrets, load_config
from .discord import DiscordPoster, render
from .llm import build_digest_llm, build_llm
from .notify import Notification, WebhookTest
from .pipeline import chunk_local_file
from .stt import build_stt
from .summarize import Summarizer
from .util import extract_urls, minimal_env, now_utc, parse_vtt, setup_logging, terminate_process, utc_iso

log = logging.getLogger("streamwatch.main")

# Human-readable labels for the dry-run poster, keyed by post kind.
_DRY_RUN_LABELS = {
    "announce": "ANNOUNCE",
    "update": "UPDATE",
    "digest": "DIGEST",
    "refined": "REFINED DIGEST",
    "test": "TEST",
}


# --------------------------------------------------------------------------- #
# Dry-run poster for `simulate --dry-run`
# --------------------------------------------------------------------------- #
class DryRunPoster:
    """Poster stand-in that prints embeds instead of POSTing to Discord."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def post(self, note: Notification, **_) -> bool:
        # Render at the same delivery boundary the real poster uses, then print
        # instead of POSTing.
        kind, embed = render(note, max_desc=self.cfg.discord.max_description_chars)
        if embed is None:
            return True
        label = _DRY_RUN_LABELS.get(kind, kind.upper())
        print(f"\n===== [{label}] =====")
        print(f"{embed.get('title', '')}")
        if embed.get("url"):
            print(embed["url"])
        print("-" * 40)
        print(embed.get("description", ""))
        for field in embed.get("fields", []):
            print(f"\n[{field['name']}]\n{field['value']}")
        print("=" * 40)
        return True


# --------------------------------------------------------------------------- #
# Simulate transcript JSONL persistence (--transcripts-out / --transcripts-in)
# --------------------------------------------------------------------------- #
@dataclass
class StoredTranscript:
    seq: int
    started_at: str
    text: str


def write_transcript_line(fh: TextIO, seq: int, started_at: str, text: str) -> None:
    """Append one transcript chunk as a JSONL line and flush (crash-safe partial)."""
    fh.write(json.dumps({"seq": seq, "started_at": started_at, "text": text}) + "\n")
    fh.flush()


def load_transcripts_jsonl(path: str | Path) -> list[StoredTranscript]:
    """Load a transcript JSONL file (written by --transcripts-out) in seq order."""
    out: list[StoredTranscript] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        out.append(
            StoredTranscript(
                seq=int(obj["seq"]),
                started_at=str(obj["started_at"]),
                text=str(obj.get("text") or ""),
            )
        )
    out.sort(key=lambda t: t.seq)
    return out


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_check_config(args) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    print(f"Config OK: {args.config}")
    print(f"  state_db:        {cfg.state_db}")
    print(f"  poll interval:   {cfg.poll_interval_minutes} min")
    print(f"  update interval: {cfg.update_interval_minutes} min")
    print(f"  stt backend:     {cfg.stt.backend}")
    print(f"  llm backend:     {cfg.llm.backend} (model={cfg.llm.model})")
    print(f"  watch targets:   {len(cfg.watch)}")
    for t in cfg.watch:
        flag = "" if t.enabled else " [disabled]"
        print(f"    - {t.platform}:{t.handle} ({t.display()}){flag}")

    warnings = check_secrets(cfg)
    if warnings:
        print("\nSecret / env warnings:")
        for w in warnings:
            print(f"  ! {w}")
        return 1
    print("\nAll required env secrets present.")
    return 0


async def _test_webhook(cfg: Config) -> int:
    async with DiscordPoster(cfg) as poster:
        ok = await poster.post(WebhookTest())
    print("Webhook post: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def cmd_test_webhook(args) -> int:
    cfg = load_config(args.config)
    return asyncio.run(_test_webhook(cfg))


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    warnings = check_secrets(cfg)
    for w in warnings:
        log.warning("config: %s", w)
    try:
        asyncio.run(run_app(cfg))
    except KeyboardInterrupt:
        pass
    return 0


async def _download_audio(cfg: Config, url: str, dest_dir: Path) -> Path:
    """Download bestaudio of a VOD URL to a local file via yt-dlp."""
    out_tmpl = str(dest_dir / "input.%(ext)s")
    argv = [
        cfg.capture.yt_dlp,
        "--quiet",
        "--no-warnings",
        *(["--proxy", cfg.capture.proxy] if cfg.capture.proxy else []),
        *(["--cookies", cfg.capture.cookies_file] if cfg.capture.cookies_file else []),
        "-f",
        "bestaudio/best",
        "-o",
        out_tmpl,
        url,
    ]
    timeout = cfg.subprocess_timeout()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=minimal_env(),  # yt-dlp never needs process secrets
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"yt-dlp download timed out after {timeout}s") from e
    finally:
        await terminate_process(proc)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {stderr.decode('utf-8', 'replace')[:300]}")
    files = [p for p in dest_dir.iterdir() if p.name.startswith("input.")]
    if not files:
        raise RuntimeError("yt-dlp produced no output file")
    return files[0]


async def _simulate(cfg: Config, args) -> int:
    from .chat.live_chat_replay import parse_live_chat_file
    from .config import WatchTarget
    from .db import Database

    transcripts_in = getattr(args, "transcripts_in", None)
    transcripts_out = getattr(args, "transcripts_out", None)
    chat_file = getattr(args, "chat_file", None)
    refined_vtt = getattr(args, "refined_vtt", None)

    # Parse the yt-dlp live-chat replay up-front (offset-sorted). Fail fast on a
    # bad path before spinning up ffmpeg / the DB.
    chat_events = parse_live_chat_file(chat_file) if chat_file else []
    if chat_file:
        log.info("loaded %d chat message(s) from %s", len(chat_events), chat_file)

    with tempfile.TemporaryDirectory(prefix="streamwatch-sim-") as tmp:
        tmpdir = Path(tmp)

        # Two ways to get transcript chunks: replay a stored JSONL (cheap, no STT)
        # or transcribe real audio. --transcripts-in never opens `input` as audio
        # and never constructs an STT backend.
        stored: list[StoredTranscript] = []
        chunks: list = []
        is_url = args.input.startswith("http://") or args.input.startswith("https://")
        if transcripts_in:
            stored = load_transcripts_jsonl(transcripts_in)
            log.info("replaying %d stored transcript chunk(s) from %s", len(stored), transcripts_in)
            total = len(stored)
        else:
            if is_url:
                log.info("downloading VOD audio: %s", args.input)
                input_path = await _download_audio(cfg, args.input, tmpdir)
            else:
                input_path = Path(args.input)
                if not input_path.exists():
                    print(f"input not found: {input_path}", file=sys.stderr)
                    return 2

            workdir = tmpdir / "work"
            log.info("chunking %s (segment=%ss)", input_path, cfg.capture.segment_seconds)
            chunks = await chunk_local_file(cfg, str(input_path), workdir)
            if not chunks:
                print("no audio chunks produced (is ffmpeg installed?)", file=sys.stderr)
                return 1
            log.info("produced %d chunks", len(chunks))
            total = len(chunks)

        db = Database(str(tmpdir / "sim.db"))
        await db.connect()
        # Dedicated empty cwd for the LLM CLI (contains prompt-injection). Created
        # here so simulate exercises the same isolation as the daemon.
        cfg.llm_workdir().mkdir(parents=True, exist_ok=True)
        # STT is only needed when actually transcribing audio. --transcripts-in
        # skips STT entirely so LLM-comparison runs never touch whisper/groq.
        stt = None if transcripts_in else build_stt(cfg.stt)
        llm = build_llm(cfg.llm)
        poster = DryRunPoster(cfg) if args.dry_run else DiscordPoster(cfg)
        summarizer = Summarizer(cfg, db, llm, poster, digest_llm=build_digest_llm(cfg.llm))
        target = WatchTarget(platform="simulate", handle="simulate", name=args.name)

        # Transcript sink: open once, flush per line so a killed run keeps partial output.
        tout: TextIO | None = open(transcripts_out, "a", encoding="utf-8") if transcripts_out else None

        # Use an aiohttp session only when posting for real.
        session_cm = poster if not args.dry_run else None
        if session_cm is not None:
            await session_cm.__aenter__()
        try:
            stream_id = await db.open_stream(
                platform="simulate",
                channel=args.name,
                title=args.title,
                url=args.input if is_url else "",
                video_id="simulate",
            )
            per_window = max(1, args.window_chunks)
            seg = cfg.capture.segment_seconds
            # Chat timestamps are derived from the (compressed) chunk timeline so the
            # summarizer sees them in order: base + the message's video offset.
            base_time = now_utc()
            chat_idx = 0  # next unposted event in the offset-sorted list
            chat_ingested = 0
            last_seq = 0

            async def ingest_chat_until(covered_seconds: float) -> None:
                """Insert every not-yet-inserted chat message whose offset falls within
                the audio covered so far, mirroring the live chat ingest path
                (db.add_chat + link extraction into add_link)."""
                nonlocal chat_idx, chat_ingested
                while chat_idx < len(chat_events) and chat_events[chat_idx].offset_s <= covered_seconds:
                    ev = chat_events[chat_idx]
                    ts = utc_iso(base_time + timedelta(seconds=ev.offset_s))
                    await db.add_chat(stream_id, ev.author, ev.text, ts)
                    for url in extract_urls(ev.text):
                        await db.add_link(stream_id, url, "chat", ts)
                    chat_idx += 1
                    chat_ingested += 1

            for i in range(total):
                if transcripts_in:
                    item = stored[i]
                    seq, started_at, text = item.seq, item.started_at, item.text
                else:
                    chunk = chunks[i]
                    seq, started_at = chunk.seq, chunk.started_at
                    try:
                        text = await stt.transcribe(chunk.path)
                    except Exception as e:
                        log.warning("transcribe failed for chunk %s: %s", chunk.seq, e)
                        text = ""
                    if tout is not None and text:
                        write_transcript_line(tout, seq, started_at, text)

                last_seq = seq
                if text:
                    await db.add_chunk(stream_id, seq, started_at, text)
                    for url in extract_urls(text):
                        await db.add_link(stream_id, url, "transcript", started_at)
                    log.info("chunk %d/%d transcribed (%d chars)", i + 1, total, len(text))

                if (i + 1) % per_window == 0:
                    # chunk seq N covers audio up to N * segment_seconds.
                    await ingest_chat_until(last_seq * seg)
                    if args.dry_run:
                        print(
                            f"[UPDATE STATS] chunks={i + 1}/{total} "
                            f"chat_ingested={chat_ingested} chat_present={chat_ingested > 0}"
                        )
                    await summarizer.post_update(stream_id, target)

            await db.end_stream(stream_id)
            # Flush any remaining chat before the final digest so it reflects the
            # whole stream, not just up to the last window boundary.
            await ingest_chat_until(float("inf"))
            if args.dry_run:
                print(f"[DIGEST STATS] chat_ingested={chat_ingested} chat_present={chat_ingested > 0}")
            await summarizer.post_digest(stream_id, target, refined=False)

            # --refined-vtt: reuse the daemon's refined-digest code path
            # (replace_transcript + post_digest refined=True) fed from a VTT file.
            if refined_vtt:
                transcript = parse_vtt(Path(refined_vtt).read_text(encoding="utf-8", errors="replace"))
                if transcript:
                    await db.replace_transcript(stream_id, transcript)
                    for url in extract_urls(transcript):
                        await db.add_link(stream_id, url, "transcript", utc_iso(now_utc()))
                    await summarizer.post_digest(stream_id, target, refined=True)
                    log.info("refined digest posted from %s", refined_vtt)
                else:
                    log.warning("refined VTT %s parsed to empty transcript; skipping", refined_vtt)
        finally:
            if tout is not None:
                tout.close()
            if session_cm is not None:
                await session_cm.__aexit__(None, None, None)
            if stt is not None and hasattr(stt, "close"):
                await stt.close()
            await db.close()
    return 0


def cmd_simulate(args) -> int:
    cfg = load_config(args.config)
    return asyncio.run(_simulate(cfg, args))


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="streamwatch", description=__doc__.splitlines()[0])
    p.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR (default INFO)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_config(sp):
        sp.add_argument("--config", "-c", required=True, help="path to config YAML")

    sp = sub.add_parser("run", help="run the daemon")
    add_config(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("check-config", help="validate config + report missing secrets")
    add_config(sp)
    sp.set_defaults(func=cmd_check_config)

    sp = sub.add_parser("test-webhook", help="post a hello embed to Discord")
    add_config(sp)
    sp.set_defaults(func=cmd_test_webhook)

    sp = sub.add_parser("simulate", help="run the full pipeline on a local file or VOD URL")
    add_config(sp)
    sp.add_argument("input", help="local audio/video file path OR a YouTube VOD URL")
    sp.add_argument("--dry-run", action="store_true", help="print updates instead of posting")
    sp.add_argument("--name", default="simulation", help="synthetic channel name")
    sp.add_argument("--title", default="Simulated stream", help="synthetic stream title")
    sp.add_argument(
        "--window-chunks",
        type=int,
        default=2,
        help="chunks per rolling update (compressed timeline; default 2 = ~2 min of audio)",
    )
    sp.add_argument(
        "--transcripts-out",
        metavar="PATH",
        help="append each transcribed chunk as a JSONL line to PATH (for later --transcripts-in replay)",
    )
    sp.add_argument(
        "--transcripts-in",
        metavar="PATH",
        help="replay transcripts from a JSONL file instead of transcribing audio "
        "(skips STT entirely; makes LLM-comparison runs cheap)",
    )
    sp.add_argument(
        "--chat-file",
        metavar="PATH",
        help="yt-dlp live_chat replay file (--write-subs --sub-langs live_chat) to ingest as chat",
    )
    sp.add_argument(
        "--refined-vtt",
        metavar="PATH",
        help="after the final digest, post a refined digest built from this VTT caption file",
    )
    sp.set_defaults(func=cmd_simulate)

    return p


def cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
