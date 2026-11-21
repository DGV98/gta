"""gta-shorts: find GTA VI moments on Twitch and clip them with openshorts.

  gta-shorts doctor                 check keys, ffmpeg, Twitch and openshorts
  gta-shorts discover               top live GTA VI streams right now
  gta-shorts vods CHANNEL           a channel's recent VODs and their viewer clips
  gta-shorts plan VOD               which stretches of a VOD would be clipped
  gta-shorts clip VOD               download those stretches and clip them
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from .openshorts_client import OpenShorts
from .pipeline import clip_vod, vod_link
from .stretches import manual_stretch, plan_stretches
from .timefmt import fmt_time, parse_time
from .twitch import Helix, TwitchError, parse_vod_id


def _helix(cfg) -> Helix:
    return Helix(cfg.twitch_client_id, cfg.twitch_client_secret)


def _game_id(cfg, helix: Helix) -> str:
    return str(cfg.game_id) if cfg.game_id else helix.game_id(cfg.game_name)


def _vod_id(value: str) -> str:
    vid = parse_vod_id(value)
    if not vid:
        raise SystemExit(f"error: {value!r} is not a Twitch VOD id or twitch.tv/videos/<id> link")
    return vid


def _plan(cfg, args, vod, clips):
    if args.start is not None or args.end is not None:
        if args.start is None or args.end is None:
            raise SystemExit("error: --start and --end go together")
        return [manual_stretch(parse_time(args.start), parse_time(args.end), clips)]
    return plan_stretches(
        clips, vod.duration,
        length=(args.minutes or cfg.stretch_minutes) * 60,
        count=args.stretches or cfg.stretches_per_vod,
        lead=cfg.lead_seconds, min_clips=cfg.min_viewer_clips)


def _print_plan(vod, clips, stretches):
    print(f"{vod.user_name}: {vod.title[:70]!r}")
    print(f"  VOD {vod.id}, {fmt_time(vod.duration)} long, {len(clips)} viewer clip(s)")
    if not stretches:
        print("  No stretch has enough viewer clips. Pass --start/--end to pick one by hand.")
        return
    for i, st in enumerate(stretches, 1):
        print(f"  [{i}] {fmt_time(st.start)} -> {fmt_time(st.end)}  "
              f"{st.clips} clip(s), {st.views} views   {vod_link(vod, st.start)}")
        inside = sorted((c for c in clips if st.start <= c.start < st.end), key=lambda c: -c.views)
        for c in inside[:3]:
            print(f"        {fmt_time(c.start)}  {c.views:>6} views  {c.title[:60]}")


# -- commands -------------------------------------------------------------
def cmd_doctor(cfg, args) -> int:
    ok = True

    def check(name, passed, hint=""):
        nonlocal ok
        ok = ok and passed
        print(f"  [{'ok' if passed else 'FAIL'}] {name}" + (f"  ({hint})" if hint and not passed else ""))

    print("gta-shorts doctor")
    check("ffmpeg on PATH", shutil.which("ffmpeg") is not None, "apt install ffmpeg")
    check("ffprobe on PATH", shutil.which("ffprobe") is not None, "comes with ffmpeg")
    have_keys = bool(cfg.twitch_client_id and cfg.twitch_client_secret)
    check("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET set", have_keys, "see .env.example")
    if have_keys:
        try:
            gid = _game_id(cfg, _helix(cfg))
            check(f"Twitch category {cfg.game_name!r} -> {gid}", True)
        except TwitchError as e:
            check("Twitch API", False, str(e))
    api = OpenShorts(cfg.api_url)
    check(f"openshorts at {cfg.api_url}", api.healthy(),
          "cd openshorts && docker compose up -d; first build takes a while")
    print(f"  output dir: {cfg.output_dir}")
    return 0 if ok else 1


def cmd_discover(cfg, args) -> int:
    helix = _helix(cfg)
    streams = helix.live_streams(_game_id(cfg, helix), cfg.language, args.top)
    now = datetime.now(timezone.utc)
    print(f"Top {len(streams)} live {cfg.game_name} streams"
          + (f" ({cfg.language})" if cfg.language else "") + ":")
    for s in streams:
        up = ""
        if s.started_at:
            up = fmt_time((now - datetime.fromisoformat(s.started_at.replace("Z", "+00:00"))).total_seconds())
        print(f"  {s.viewer_count:>7,}  {s.user_login:<22} up {up:>8}  {s.title[:60]}")
    return 0


def cmd_vods(cfg, args) -> int:
    helix = _helix(cfg)
    vods = helix.archive_vods(helix.user_id(args.channel), args.limit)
    if not vods:
        print(f"{args.channel} has no saved VODs (disabled, expired, or never streamed).")
        return 0
    for v in vods:
        clips = helix.vod_clips(v)
        top = clips[0].views if clips else 0
        print(f"  {v.id}  {v.created_at[:16].replace('T', ' ')}  {fmt_time(v.duration):>8}  "
              f"{len(clips):>4} clips (top {top:>6} views)  {v.title[:50]}")
    return 0


def cmd_plan(cfg, args) -> int:
    helix = _helix(cfg)
    vod = helix.vod(_vod_id(args.vod))
    clips = helix.vod_clips(vod)
    _print_plan(vod, clips, _plan(cfg, args, vod, clips))
    return 0


def cmd_clip(cfg, args) -> int:
    helix = _helix(cfg)
    vod = helix.vod(_vod_id(args.vod))
    clips = helix.vod_clips(vod)
    stretches = _plan(cfg, args, vod, clips)
    _print_plan(vod, clips, stretches)
    if not stretches:
        return 1
    if args.dry_run:
        return 0
    if args.keep_source:
        cfg.keep_sources = True
    api = OpenShorts(cfg.api_url)
    if not api.healthy():
        print(f"error: openshorts is not answering at {cfg.api_url} (run `gta-shorts doctor`)",
              file=sys.stderr)
        return 2
    job_log = None if args.quiet else (lambda line: print(f"      | {line}"))
    manifest = clip_vod(cfg, vod, stretches, api, job_log=job_log)
    print(f"\nManifest: {manifest['path']}")
    done = [c for s in manifest["stretches"] for c in s.get("clips") or []]
    for c in sorted(done, key=lambda c: -(c.get("score") or 0)):
        print(f"  {c.get('score') or '?':>3}  {c['duration']:>5.1f}s  {c['file']:<28} {c.get('hook') or ''}")
    failed = [s for s in manifest["stretches"] if s.get("status") != "done"]
    if failed:
        print(f"  {len(failed)} stretch(es) not done; re-run the same command to resume.")
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gta-shorts", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", type=Path, default=None, help="config.toml (default ./config.toml)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the setup")

    d = sub.add_parser("discover", help="top live GTA VI streams")
    d.add_argument("--top", type=int, default=15)

    v = sub.add_parser("vods", help="a channel's recent VODs")
    v.add_argument("channel")
    v.add_argument("--limit", type=int, default=5)

    for name in ("plan", "clip"):
        c = sub.add_parser(name, help="show the stretches" if name == "plan" else "clip a VOD")
        c.add_argument("vod", help="VOD id or https://www.twitch.tv/videos/<id>")
        c.add_argument("--stretches", type=int, help="how many stretches (config default)")
        c.add_argument("--minutes", type=float, help="stretch length (config default)")
        c.add_argument("--start", help="pick the stretch by hand, e.g. 1:23:00")
        c.add_argument("--end", help="end of the hand-picked stretch, e.g. 1:35:00")
        if name == "clip":
            c.add_argument("--dry-run", action="store_true", help="plan only, download nothing")
            c.add_argument("--keep-source", action="store_true", help="keep the downloaded stretch")
            c.add_argument("--quiet", action="store_true", help="hide openshorts job logs")
    return p


COMMANDS = {"doctor": cmd_doctor, "discover": cmd_discover, "vods": cmd_vods,
            "plan": cmd_plan, "clip": cmd_clip}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_mod.load(args.config)
    try:
        return COMMANDS[args.command](cfg, args)
    except TwitchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; re-run the same command to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
