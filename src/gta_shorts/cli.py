"""gta-shorts: find GTA VI moments on Twitch and clip them with openshorts.

  gta-shorts doctor                 check keys, ffmpeg, Twitch and openshorts
  gta-shorts discover               top live GTA VI streams right now
  gta-shorts vods CHANNEL           a channel's recent VODs and their viewer clips
  gta-shorts plan VOD               which stretches of a VOD would be clipped
  gta-shorts clip VOD               download those stretches and clip them
  gta-shorts scan                   one watcher scan: track channels, queue ended VODs
  gta-shorts watch                  the 24/7 loop: scan every poll_minutes, clip the queue
  gta-shorts status                 what the watcher tracks, queued, made and spent
  gta-shorts posts                  what goes to TikTok next, and what was posted
  gta-shorts post                   the 24/7 poster: fill runs/to_post, or post via Upload-Post
  gta-shorts post --once            one round now (fill the folder / post the next clip)
  gta-shorts skip CLIP              never post that clip (path, or its name in to_post/)
"""
from __future__ import annotations

import argparse
import shutil
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import config as config_mod
from .openshorts_client import OpenShorts
from .pipeline import clip_vod, plan_vod, vod_link
from .state import LIVE_POSTS, State, parse_iso
from .stretches import manual_stretch
from .timefmt import fmt_time, parse_time
from .twitch import Helix, TwitchError, parse_vod_id
from .upload_post import UploadPost, UploadPostError


def _log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


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
    return plan_vod(cfg, vod, clips, count=args.stretches, minutes=args.minutes)


def _print_plan(vod, clips, stretches):
    print(f"{vod.user_name}: {vod.title[:70]!r}")
    muted = sum(d for _, d in vod.muted)
    print(f"  VOD {vod.id}, {fmt_time(vod.duration)} long, {len(clips)} GTA VI viewer clip(s)"
          + (f", {fmt_time(muted)} muted by Twitch" if muted else ""))
    if not stretches:
        print("  No stretch has enough unmuted viewer clips. Pass --start/--end to pick one by hand.")
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
          "run scripts/up.sh; the first build takes a while. Still failing: "
          "cd openshorts && docker compose logs --tail 30 backend")
    if cfg.post_mode == "upload-post" or cfg.upload_post_api_key:
        check("UPLOAD_POST_API_KEY set", bool(cfg.upload_post_api_key),
              "app.upload-post.com -> API Keys; put it in .env")
        check("[post] user set", bool(cfg.post_user),
              "your Upload-Post profile name (Manage Users), not the TikTok handle")
        if cfg.upload_post_api_key:
            up = UploadPost(cfg.upload_post_api_key)
            try:
                plan = str(up.me().get("plan") or "?")
                check(f"Upload-Post key works (plan: {plan})", plan.lower() != "free",
                      "TikTok posting needs a paid Upload-Post plan")
                if cfg.post_user:
                    tiktok = up.tiktok_account(cfg.post_user)
                    who = (tiktok or {}).get("handle") or (tiktok or {}).get("display_name") or ""
                    check(f"TikTok connected to profile {cfg.post_user!r}"
                          + (f" (@{who})" if who else ""),
                          bool(tiktok) and not tiktok.get("reauth_required"),
                          "connect or reconnect it at app.upload-post.com/manage-users")
            except (UploadPostError, httpx.HTTPError) as e:
                check("Upload-Post API", False, str(e))
    print(f"  posting: {_mode_text(cfg)}")
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
    gid = _game_id(cfg, helix)
    for v in vods:
        clips = helix.vod_clips(v)
        gta = [c for c in clips if not c.game_id or c.game_id == gid]
        top = gta[0].views if gta else 0
        print(f"  {v.id}  {v.created_at[:16].replace('T', ' ')}  {fmt_time(v.duration):>8}  "
              f"{len(gta):>4}/{len(clips):<4} GTA VI clips (top {top:>6} views)  {v.title[:45]}")
    return 0


def cmd_plan(cfg, args) -> int:
    helix = _helix(cfg)
    vod = helix.vod(_vod_id(args.vod))
    clips = helix.vod_clips(vod, game_id=_game_id(cfg, helix))
    _print_plan(vod, clips, _plan(cfg, args, vod, clips))
    return 0


def cmd_clip(cfg, args) -> int:
    helix = _helix(cfg)
    vod = helix.vod(_vod_id(args.vod))
    clips = helix.vod_clips(vod, game_id=_game_id(cfg, helix))
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


def _watcher(cfg, args, state):
    from .watch import Watcher
    job_log = None if getattr(args, "quiet", True) else (lambda line: _log(f"      | {line}"))
    return Watcher(cfg, _helix(cfg), OpenShorts(cfg.api_url), state, log=_log, job_log=job_log)


def cmd_scan(cfg, args) -> int:
    state = State(cfg.state_db)
    try:
        rep = _watcher(cfg, args, state).scan()
    finally:
        state.close()
    print(rep.summary())
    for line in rep.queued:
        print(f"  queued   {line}")
    for line in rep.skipped:
        print(f"  skipped  {line}")
    print(f"State: {cfg.state_db}  (`gta-shorts status` for the queue)")
    return 0


def cmd_watch(cfg, args) -> int:
    def on_term(signum, frame):
        raise KeyboardInterrupt          # docker stop: leave like Ctrl-C (state is already saved)
    signal.signal(signal.SIGTERM, on_term)
    state = State(cfg.state_db)
    try:
        _log(f"watching {cfg.game_name!r} (scan every {cfg.poll_minutes:g} min, "
             f"cap {cfg.max_stretches_per_day} stretches/24 h); openshorts at {cfg.api_url}; "
             f"state {cfg.state_db}")
        _watcher(cfg, args, state).run(once=args.once)
    finally:
        state.close()
    return 0


def cmd_status(cfg, args) -> int:
    if not cfg.state_db.exists():
        print(f"No watcher state yet at {cfg.state_db} (run `gta-shorts scan` or `watch`).")
        return 1
    state = State(cfg.state_db, readonly=True)
    try:
        now = datetime.now(timezone.utc)
        day = now - timedelta(hours=24)
        last = state.get_meta("last_scan")
        ago = f"{fmt_time((now - parse_iso(last)).total_seconds())} ago" if last else "never"
        channels = state.tracked_channels(now, cfg.track_days)
        prio = [c["login"] for c in channels if c["priority"]]
        print(f"Last scan: {ago}.  Tracking {len(channels)} channel(s)"
              + (f", priority: {', '.join(prio)}" if prio else ", no priority channels set"))
        n, usd = state.usage_since(day)
        usd_cap = f" / ${cfg.max_usd_per_day:.2f}" if cfg.max_usd_per_day is not None else ""
        print(f"Last 24 h: {n}/{cfg.max_stretches_per_day} stretches submitted, "
              f"${usd:.3f}{usd_cap} Gemini, {state.clip_count_since(day)} clip(s) made")
        vods = state.counts("vods")
        print("VODs: " + (", ".join(f"{k} {v}" for k, v in sorted(vods.items())) or "none"))
        queue = state.queue(args.limit)
        print(f"Queue ({sum(v for k, v in state.counts('stretches').items() if k in ('queued', 'processing'))}):")
        for r in queue:
            flag = "*" if r["priority"] else " "
            extra = ""
            if r["error"]:
                when = f" after {r['next_try'][11:16]}Z" if r["next_try"] else ""
                extra = f"  retry{when}: {r['error'][:60]}"
            print(f"  {flag} {r['status']:<10} {r['login']:<18} {r['vod_id']} @ {fmt_time(r['start']):>8}  "
                  f"{r['clips']:>3} clips {r['views']:>8,} views{extra}")
        recent = state.finished_since(day)
        if recent:
            print("Finished in the last 24 h:")
            for r in recent[:args.limit]:
                what = (f"{r['clips_made']} clip(s)" if r["status"] == "done"
                        else f"{r['status']}: {(r['error'] or '')[:70]}")
                print(f"    {r['finished_at'][11:16]}Z {r['login']:<18} {r['vod_id']} @ "
                      f"{fmt_time(r['start']):>8}  {what}")
        if state.has_table("posts"):
            print(_posting_line(cfg, state, now) + "  (`gta-shorts posts` for details)")
    finally:
        state.close()
    return 0


# -- posting (phase 3) ----------------------------------------------------
def _poster(cfg, state):
    from .post import Poster
    return Poster(cfg, UploadPost(cfg.upload_post_api_key), state, log=_log)


def _outbox(cfg, state):
    from .post import Outbox
    return Outbox(cfg, state, log=_log)


def _mode_text(cfg) -> str:
    if cfg.post_mode == "folder":
        return (f"folder mode: the next {cfg.post_folder_size} clips wait in "
                f"{cfg.output_dir / 'to_post'} for you to upload")
    if cfg.post_mode == "upload-post":
        return (f"through Upload-Post, {cfg.post_per_day}/24 h, {cfg.post_min_gap_minutes:g}+ min "
                f"apart, {cfg.post_hours[0]}-{cfg.post_hours[1]} h {cfg.post_timezone}, "
                f"{cfg.post_privacy}")
    return 'off ([post] mode = "off")'


def _posting_line(cfg, state, now) -> str:
    day = now - timedelta(hours=24)
    ok, low = state.unposted_counts(now - timedelta(hours=cfg.post_max_age_hours),
                                    cfg.post_min_score, cfg.post_priority_only)
    waiting = f"{ok} clip(s) waiting (score {cfg.post_min_score}+), {low} below"
    if cfg.post_mode == "folder":
        return (f"Posting: folder mode, {len(state.exported())}/{cfg.post_folder_size} clip(s) in "
                f"to_post/, {state.count_finished('manual', day)} taken out in the last 24 h, "
                f"{waiting} more")
    if cfg.post_mode == "off":
        return f"Posting: OFF, {waiting}"
    line = (f"Posting: through Upload-Post, {len(state.live_posts_since(day))}/{cfg.post_per_day} "
            f"posted in the last 24 h, {waiting}")
    until = state.get_meta("post_paused_until")
    if until and now < parse_iso(until):
        line += f"; PAUSED until {until[11:16]}Z: {state.get_meta('post_pause_reason')}"
    return line


def _post_result(r) -> str:
    if r["status"] == "posted":
        return r["url"] or "posted (TikTok link not ready)"
    if r["status"] == "exported":
        return f"waiting in to_post/ as {r['file']}"
    if r["status"] == "manual":
        return "taken out of to_post/ (counted as posted)"
    return r["status"] + (f": {r['error'][:80]}" if r["error"] else "")


def cmd_posts(cfg, args) -> int:
    state = State(cfg.state_db)
    try:
        now = datetime.now(timezone.utc)
        poster = _poster(cfg, state)
        print(_posting_line(cfg, state, now))
        if cfg.post_mode == "folder":
            folder = state.exported()
            print(f"In {cfg.output_dir / 'to_post'}:" if folder else
                  f"{cfg.output_dir / 'to_post'} is empty (the poster fills it every minute).")
            for r in folder:
                print(f"  {r['file']}  (score {r['score'] or '?'})")
        elif cfg.post_mode == "upload-post":
            print(f"  as {cfg.post_privacy} on Upload-Post profile {cfg.post_user or '(not set)'}, "
                  f"{cfg.post_min_gap_minutes:g}+ min apart, {cfg.post_hours[0]}:00-"
                  f"{cfg.post_hours[1]}:00 {cfg.post_timezone}")
            ok, why, _ = poster.due(now)
            print("  next post: " + ("as soon as the poster checks" if ok else why))
        recent = [r for r in state.recent_posts(now - timedelta(days=args.days))
                  if r["status"] != "exported"]
        if recent:
            print(f"Last {args.days:g} day(s):")
            for r in recent[:args.limit]:
                when = poster.local(parse_iso(r["started_at"]))
                print(f"  {when}  {r['login']:<16} {r['score'] or '?':>3}  {r['path']}\n"
                      f"      {_post_result(r)}")
        last = state.last_live_post()
        nxt = state.clips_to_post(now, since=now - timedelta(hours=cfg.post_max_age_hours),
                                  min_score=cfg.post_min_score, max_per_vod=cfg.post_max_per_vod,
                                  avoid_login=last["login"] if last is not None else None,
                                  priority_only=cfg.post_priority_only, limit=args.limit)
        print("Up next:" if nxt else "Up next: nothing (no unposted clip qualifies).")
        for r in nxt:
            flag = "*" if r["priority"] else " "
            retry = f"  (attempt {r['post_attempts'] + 1})" if r["post_attempts"] else ""
            print(f"  {flag} {r['score'] or '?':>3}  {r['login']:<16} {r['path']}{retry}\n"
                  f"        {r['hook'] or ''}")
    finally:
        state.close()
    return 0


def cmd_post(cfg, args) -> int:
    def on_term(signum, frame):
        raise KeyboardInterrupt          # docker stop: an in-flight post is resumed next start
    if (args.clip or args.private) and not (args.once and cfg.post_mode == "upload-post"):
        raise SystemExit('error: --clip and --private go with --once and mode = "upload-post"')
    signal.signal(signal.SIGTERM, on_term)
    state = State(cfg.state_db)
    try:
        poster = _poster(cfg, state)
        if not args.once:
            _log(f"poster: {_mode_text(cfg)}; clips scoring {cfg.post_min_score}+; "
                 f"state {cfg.state_db}")
            poster.run()
            return 0
        if cfg.post_mode == "off":
            print('error: posting is off ([post] mode = "off")', file=sys.stderr)
            return 2
        if cfg.post_mode == "folder":
            added = _outbox(cfg, state).sync()
            print(f"Added {len(added)} clip(s). In {cfg.output_dir / 'to_post'}:")
            for r in state.exported():
                print(f"  {r['file']}  (score {r['score'] or '?'})")
            return 0
        missing = poster.missing()
        if missing:
            print(f"error: {missing}", file=sys.stderr)
            return 2
        if args.clip:
            clip = state.clip_for_post(args.clip)
            if clip is None:
                print(f"error: no clip {args.clip!r} in {cfg.state_db} "
                      "(paths as `gta-shorts posts` prints them)", file=sys.stderr)
                return 2
            if clip["post_status"] in LIVE_POSTS:
                print(f"error: {args.clip} is already {clip['post_status']}", file=sys.stderr)
                return 1
        else:
            clip = poster.pick(datetime.now(timezone.utc))
            if clip is None:
                print("Nothing to post: no unposted clip qualifies (see `gta-shorts posts`).")
                return 1
        row = poster.post_now(clip, privacy="SELF_ONLY" if args.private else None)
        if row["status"] in ("posted", "inbox"):
            print(f"{row['status']}: {row['url'] or row['error'] or 'TikTok link not ready yet'}")
            return 0
        print(f"{row['status']}: {row['error'] or 'no answer yet; `gta-shorts posts` shows it later'}")
        return 1
    finally:
        state.close()


def cmd_skip(cfg, args) -> int:
    state = State(cfg.state_db)
    try:
        in_folder = {r["file"]: r["path"] for r in state.exported()}
        for arg in args.clips:
            path = in_folder.get(Path(arg).name, arg)
            clip = state.clip_for_post(path)
            if clip is None:
                print(f"  no clip {path!r}")
            elif clip["post_status"] == "exported":
                _outbox(cfg, state).skip(state.post(path))
                print(f"  {path}: taken out of to_post/, won't be posted")
            elif clip["post_status"] in LIVE_POSTS:
                print(f"  {path} is already {clip['post_status']}")
            else:
                state.skip_post(path, "skipped by hand")
                print(f"  {path}: won't be posted")
    finally:
        state.close()
    return 0


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

    sub.add_parser("scan", help="one watcher scan (no clipping)")
    w = sub.add_parser("watch", help="run the 24/7 loop")
    w.add_argument("--once", action="store_true",
                   help="one scan, clip what is ready, then exit")
    w.add_argument("--quiet", action="store_true", help="hide openshorts job logs")
    st = sub.add_parser("status", help="watcher queue, budget and recent results")
    st.add_argument("--limit", type=int, default=10)

    ps = sub.add_parser("posts", help="TikTok: what goes out next, what was posted")
    ps.add_argument("--limit", type=int, default=10)
    ps.add_argument("--days", type=float, default=2, help="history to show (default 2)")
    po = sub.add_parser("post", help="run the 24/7 poster")
    po.add_argument("--once", action="store_true",
                    help="folder mode: top up to_post/ now; upload-post mode: post the next "
                         "clip now (ignores the schedule), wait for TikTok")
    po.add_argument("--clip", help="upload-post --once: this clip instead (path as `posts` shows it)")
    po.add_argument("--private", action="store_true",
                    help="upload-post --once: only you can see it on TikTok (SELF_ONLY)")
    sk = sub.add_parser("skip", help="never post these clips")
    sk.add_argument("clips", nargs="+",
                    help="paths as `gta-shorts posts` shows them, or file names in to_post/")
    return p


COMMANDS = {"doctor": cmd_doctor, "discover": cmd_discover, "vods": cmd_vods,
            "plan": cmd_plan, "clip": cmd_clip, "scan": cmd_scan, "watch": cmd_watch,
            "status": cmd_status, "posts": cmd_posts, "post": cmd_post, "skip": cmd_skip}


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
