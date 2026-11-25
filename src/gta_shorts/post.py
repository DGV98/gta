"""Phase 3: get the best finished clips onto TikTok.

Either way the order is the same, best clip first: priority (clipping-program)
channels, then a different streamer than the last one, then openshorts' score.
Only clips that scored ``min_score`` or more, made in the last
``max_age_hours``, at most ``max_per_vod`` from one stream, and with
``priority_only`` only priority channels. ``gta-shorts skip`` keeps a clip out.

``mode = "folder"`` (Outbox): ``runs/to_post`` always holds the next
``folder_size`` clips, each with a .txt caption, for you to upload by hand.
A clip you take out of the folder counts as posted and the next one takes
its place. One left there past ``max_age_hours`` is swapped for a newer one.

``mode = "upload-post"`` (Poster): posts them for you through Upload-Post,
``per_day`` per rolling 24 h, ``min_gap_minutes`` apart, between ``hours``
(local time in ``timezone``). Every attempt gets its own Upload-Post request
id, saved before the upload starts. After a crash or restart the poster asks
Upload-Post about that request instead of uploading again, so one attempt
never posts twice.

The caption is the clip's ``captions.tiktok`` from its manifest.json
(openshorts' description + the streamer credit), so it can be edited there
before the clip goes out.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .pipeline import load_manifest
from .state import State, iso, parse_iso
from .upload_post import Outcome, UploadPost, UploadPostError, tiktok_outcome

Log = Callable[[str], None]
POLL_SECONDS = 15.0                  # Upload-Post status checks while a post is in flight
IDLE_RECHECK = 300.0
NOT_RECEIVED_GRACE = timedelta(minutes=10)   # unknown to Upload-Post this long: upload again
URL_WAIT = timedelta(minutes=15)     # TikTok's link can lag the publish by a few minutes
GIVE_UP = timedelta(hours=3)         # still no result: stop asking (never re-post blindly)
ACCOUNT_PAUSE = timedelta(hours=1)   # key, plan, cap or TikTok-connection trouble
CAPTION_MAX = 2200
FOLDER = "to_post"                   # under the output dir
FOLDER_RECHECK = 60.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def next_clip(cfg: Config, state: State, now: datetime):
    """The next clip to post (None: nothing waiting)."""
    last = state.last_live_post()
    while True:
        rows = state.clips_to_post(
            now, since=now - timedelta(hours=cfg.post_max_age_hours),
            min_score=cfg.post_min_score, max_per_vod=cfg.post_max_per_vod,
            avoid_login=last["login"] if last is not None else None,
            priority_only=cfg.post_priority_only)
        if not rows:
            return None
        if (cfg.output_dir / rows[0]["path"]).exists():
            return rows[0]
        state.skip_post(rows[0]["path"], "clip file missing")


def nothing_to_post(cfg: Config) -> str:
    return (f"nothing to post: no unposted clip scoring {cfg.post_min_score}+ from the last "
            f"{cfg.post_max_age_hours:g} h" + (" of a priority channel" if cfg.post_priority_only else ""))


def slug(text: Optional[str], limit: int = 40) -> str:
    """A file-name-safe version of a hook: "He flew 💀" -> "he-flew"."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:limit].rstrip("-")


def caption_for(cfg: Config, clip) -> str:
    """The clip's TikTok caption from its manifest (credit included)."""
    run, _, file = clip["path"].partition("/")
    manifest = load_manifest(cfg.output_dir / run / "manifest.json")
    for st in manifest.get("stretches") or []:
        for c in st.get("clips") or []:
            if c.get("file") == file:
                text = (c.get("captions") or {}).get("tiktok") or c.get("credit")
                if text:
                    return str(text)[:CAPTION_MAX]
    credit = cfg.credit.format(login=clip["login"], name=clip["login"])
    hook = str(clip["hook"] or "").strip()
    return (f"{hook}\n\n{credit}" if hook else credit)[:CAPTION_MAX]


class Outbox:
    """The to_post folder for uploading by hand (``mode = "folder"``)."""

    def __init__(self, cfg: Config, state: State, *, log: Log = print,
                 clock: Callable[[], datetime] = utcnow):
        self.cfg, self.state, self.log, self.clock = cfg, state, log, clock
        self.dir = cfg.output_dir / FOLDER

    def _remove(self, file: str) -> None:
        for p in (self.dir / file, (self.dir / file).with_suffix(".txt")):
            p.unlink(missing_ok=True)

    def sync(self) -> List[str]:
        """Note what you took out, swap stale clips, top the folder up. Returns names added."""
        now, cfg, state = self.clock(), self.cfg, self.state
        self.dir.mkdir(parents=True, exist_ok=True)
        for part in self.dir.glob(".*.part"):               # a copy cut short by a restart
            part.unlink()
        stale = now - timedelta(hours=cfg.post_max_age_hours)
        held = 0
        for row in state.exported():
            if not (self.dir / row["file"]).exists():
                state.finish_post(row["path"], "manual", now)
                self._remove(row["file"])                   # its caption .txt
                self.log(f"{row['file']} was taken out of {FOLDER}/: counted as posted")
            elif parse_iso(row["created_at"]) < stale:
                self._remove(row["file"])
                state.update_post(row["path"], status="expired",
                                  error=f"left in {FOLDER}/ past max_age_hours")
                self.log(f"{row['file']}: older than {cfg.post_max_age_hours:g} h, "
                         "swapped for a newer clip")
            else:
                held += 1
        added = []
        while held < cfg.post_folder_size:
            clip = next_clip(cfg, state, now)
            if clip is None:
                break
            n = int(state.get_meta("folder_seq") or 0) + 1
            state.set_meta("folder_seq", str(n))
            name = f"{n:04d}_{clip['login']}_{slug(clip['hook']) or 'clip'}.mp4"
            # Recorded first: a crash mid-copy loses this clip at worst, it never
            # leaves an untracked copy behind that could be uploaded twice.
            state.export_post(clip["path"], name, now)
            tmp = self.dir / f".{name}.part"                # never a half-copied file to upload
            try:
                shutil.copyfile(cfg.output_dir / clip["path"], tmp)
                (self.dir / name).with_suffix(".txt").write_text(caption_for(cfg, clip) + "\n",
                                                                 encoding="utf-8")
                os.replace(tmp, self.dir / name)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                self._remove(name)
                state.update_post(clip["path"], status="retry", file=None,
                                  error=f"copy to {FOLDER}/ failed: {e}")
                self.log(f"could not copy {clip['path']} to {FOLDER}/: {e}")
                break
            held += 1
            added.append(name)
        if added:
            self.log(f"added to {FOLDER}/: {', '.join(added)}")
        return added

    def skip(self, row) -> None:
        """Take a clip back out of the folder without counting it as posted."""
        if row["file"]:
            self._remove(row["file"])
        self.state.skip_post(row["path"], "skipped by hand")


class Poster:
    def __init__(self, cfg: Config, api: UploadPost, state: State, *, log: Log = print,
                 clock: Callable[[], datetime] = utcnow, sleep=time.sleep):
        self.cfg, self.api, self.state = cfg, api, state
        self.log, self.clock, self.sleep = log, clock, sleep
        self.tz = ZoneInfo(cfg.post_timezone)
        self._last_note: Optional[str] = None

    def note(self, msg: str) -> None:
        """Log a waiting reason once, not on every recheck."""
        if msg != self._last_note:
            self.log(msg)
            self._last_note = msg

    def local(self, dt: datetime) -> str:
        return dt.astimezone(self.tz).strftime("%a %H:%M %Z")

    # -- when -------------------------------------------------------------
    def in_hours(self, now: datetime) -> bool:
        start, end = self.cfg.post_hours
        h = now.astimezone(self.tz).hour
        if start == end:
            return True
        return start <= h < end if start < end else (h >= start or h < end)

    def paused(self, now: datetime) -> Optional[Tuple[str, float]]:
        """(why, seconds left) while an account problem pauses all posting."""
        until = self.state.get_meta("post_paused_until")
        if until and now < parse_iso(until):
            return (f"posting paused until {self.local(parse_iso(until))}: "
                    f"{self.state.get_meta('post_pause_reason')}",
                    (parse_iso(until) - now).total_seconds())
        return None

    def due(self, now: datetime) -> Tuple[bool, str, float]:
        """(may a new post start now, why not, seconds until it might)."""
        cfg, state = self.cfg, self.state
        paused = self.paused(now)
        if paused:
            return (False, *paused)
        if cfg.post_per_day <= 0:
            return False, "[post] per_day is 0", IDLE_RECHECK
        recent = state.live_posts_since(now - timedelta(hours=24))
        if len(recent) >= cfg.post_per_day:
            frees = parse_iso(recent[len(recent) - cfg.post_per_day]["started_at"]) + timedelta(hours=24)
            return (False, f"daily cap: {len(recent)}/{cfg.post_per_day} posts in the last 24 h, "
                    f"next after {self.local(frees)}", (frees - now).total_seconds())
        last = state.last_live_post()
        if last is not None:
            next_at = parse_iso(last["started_at"]) + timedelta(minutes=cfg.post_min_gap_minutes)
            if now < next_at:
                return False, f"next post after {self.local(next_at)}", (next_at - now).total_seconds()
        if not self.in_hours(now):
            start, end = cfg.post_hours
            return False, f"outside posting hours ({start}:00-{end}:00 {cfg.post_timezone})", IDLE_RECHECK
        return True, "", 0.0

    def pause(self, reason: str, now: datetime, length: timedelta = ACCOUNT_PAUSE) -> None:
        self.state.set_meta("post_paused_until", iso(now + length))
        self.state.set_meta("post_pause_reason", reason[:300])
        self.log(f"posting paused for {length.total_seconds() / 3600:g} h: {reason}")

    # -- what -------------------------------------------------------------
    def pick(self, now: datetime):
        return next_clip(self.cfg, self.state, now)

    # -- how --------------------------------------------------------------
    def start(self, clip, privacy: Optional[str] = None) -> str:
        """Begin posting ``clip`` (a clip_for_post/clips_to_post row). Returns its path."""
        now, path = self.clock(), clip["path"]
        attempt = clip["post_attempts"] + 1
        # Unique per attempt: a resend of the same attempt replays it (Idempotency-Key)
        # instead of posting again; a new attempt is never mistaken for an old one.
        request_id = (f"gta-{hashlib.sha1(path.encode()).hexdigest()[:12]}-{attempt}-"
                      f"{int(now.timestamp())}")
        privacy = privacy or self.cfg.post_privacy
        self.state.start_post(path, request_id, attempt, privacy, now)
        self._last_note = None
        self.log(f"posting {clip['login']} {path} (score {clip['score']}, attempt {attempt}"
                 + (f", {privacy}" if privacy != "PUBLIC_TO_EVERYONE" else "") + f"): "
                 f"{clip['hook'] or ''}")
        self._send(self.state.post(path))
        return path

    def _send(self, row) -> None:
        file = self.cfg.output_dir / row["path"]
        if not file.exists():
            self.state.update_post(row["path"], status="skipped", error="clip file missing")
            self.log(f"{row['path']}: clip file missing; skipped")
            return
        try:
            body = self.api.upload_video(
                file, user=self.cfg.post_user, request_id=row["request_id"],
                caption=caption_for(self.cfg, self.state.clip_for_post(row["path"])),
                privacy=row["privacy"], external_id=row["path"])
        except UploadPostError as e:
            if e.transient:
                self.log(f"{row['path']}: {e}; asking Upload-Post again shortly")
                return
            self._failed(row, Outcome("failed", error=str(e), code=e.code), account=e.account)
            return
        except httpx.HTTPError as e:
            # It may or may not have arrived: the next check asks Upload-Post.
            self.log(f"{row['path']}: upload interrupted ({type(e).__name__}: {e}); checking back")
            return
        except OSError as e:                              # reading the file: nothing was sent
            self._failed(row, Outcome("failed", error=f"{type(e).__name__}: {e}"))
            return
        self._apply(row, tiktok_outcome(body))

    def check(self, row) -> None:
        """Ask Upload-Post how the in-flight post ``row`` is doing and record it."""
        now = self.clock()
        try:
            body = self.api.status(row["request_id"])
        except UploadPostError as e:
            if e.account:
                self.pause(str(e), now)
            else:
                self.log(f"{row['path']}: status check failed: {e}")
            return
        except httpx.HTTPError as e:
            self.log(f"{row['path']}: status check failed: {type(e).__name__}: {e}")
            return
        if body is None:                                  # Upload-Post never got it
            if now - parse_iso(row["started_at"]) >= GIVE_UP:
                # Resends reuse the Idempotency-Key, which only lasts 24 h: stop well before.
                self.state.update_post(row["path"], status="failed",
                                       error="Upload-Post never confirmed receiving it "
                                             "(check TikTok before posting it by hand)")
                self.log(f"{row['path']}: Upload-Post never confirmed it; marked failed")
            elif now - parse_iso(row["sent_at"] or row["started_at"]) >= NOT_RECEIVED_GRACE:
                self.log(f"{row['path']}: Upload-Post never received it; uploading again")
                self.state.update_post(row["path"], sent_at=iso(now))
                self._send(self.state.post(row["path"]))
            return
        self._apply(row, tiktok_outcome(body))

    def _apply(self, row, out: Outcome) -> None:
        now, path = self.clock(), row["path"]
        age = now - parse_iso(row["started_at"])
        if out.state == "pending" or (out.state == "posted" and not out.url and age < URL_WAIT):
            if age >= GIVE_UP:
                self.state.update_post(path, status="failed",
                                       error=f"no result from Upload-Post after {age} "
                                             "(check TikTok before posting it by hand)")
                self.log(f"{path}: no result after {age}; marked failed, not re-posted")
            return
        if out.state in ("posted", "inbox"):
            self.state.finish_post(path, out.state, now, url=out.url, error=out.error)
            self.log(f"posted {path}: {out.url or out.error or 'TikTok link not ready yet'}")
            return
        self._failed(row, out)

    def _failed(self, row, out: Outcome, *, account: bool = False) -> None:
        now, path, attempts = self.clock(), row["path"], row["attempts"]
        if account or out.account:
            # Not this clip's fault: it stays first in line, the attempt uncounted.
            self.state.update_post(path, status="retry", attempts=attempts - 1, next_try=None,
                                   error=out.error)
            self.pause(out.error or "Upload-Post refused the post", now)
            return
        if out.final or attempts >= self.cfg.post_max_attempts:
            self.state.update_post(path, status="failed", error=out.error)
            self.log(f"{path}: not posting it: {out.error}")
            return
        retry = now + timedelta(minutes=30 * attempts)
        self.state.update_post(path, status="retry", error=out.error, next_try=iso(retry))
        self.log(f"{path}: post failed ({out.error}); retrying after {self.local(retry)}")

    # -- the loop ---------------------------------------------------------
    def step(self, start_new: bool = True) -> float:
        """One action: check the post in flight, or start the next one when due.
        Returns how long to wait before the next step."""
        now = self.clock()
        paused = self.paused(now)
        if paused:
            self.note(paused[0])
            return paused[1]
        row = self.state.post_in_flight()
        if row is not None:
            self.check(row)
            still = self.state.post(row["path"])["status"] == "posting"
            return POLL_SECONDS if still else 1.0
        if not start_new:
            return IDLE_RECHECK
        ok, why, wait = self.due(now)
        if not ok:
            self.note(why)
            return wait
        clip = self.pick(now)
        if clip is None:
            self.note(nothing_to_post(self.cfg))
            return IDLE_RECHECK
        self.start(clip)
        return POLL_SECONDS

    def post_now(self, clip, privacy: Optional[str] = None, timeout: float = 1800.0):
        """Post ``clip`` right away (ignores the schedule) and wait for TikTok's answer."""
        path = self.start(clip, privacy)
        deadline = time.monotonic() + timeout
        while self.state.post(path)["status"] == "posting" and time.monotonic() < deadline:
            self.sleep(POLL_SECONDS)
            self.check(self.state.post(path))
        return self.state.post(path)

    def missing(self) -> Optional[str]:
        """What the setup lacks before anything can be posted."""
        if not self.cfg.upload_post_api_key:
            return "UPLOAD_POST_API_KEY is not set in .env"
        if not self.cfg.post_user:
            return "[post] user (your Upload-Post profile name) is not set in config.toml"
        return None

    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        """Work ``[post] mode`` until ``stop()``. In any mode, a post Upload-Post
        still has under way is seen through."""
        cfg, mode = self.cfg, self.cfg.post_mode
        outbox = Outbox(cfg, self.state, log=self.log, clock=self.clock)
        if mode == "off":
            self.log('posting is off ([post] mode = "off" in config.toml)')
        elif mode == "folder":
            self.log(f"keeping the next {cfg.post_folder_size} clips in {outbox.dir} "
                     "for you to upload")
        while not stop():
            wait = IDLE_RECHECK
            try:
                if mode == "folder":
                    if outbox.sync():
                        self._last_note = None
                    if len(self.state.exported()) < cfg.post_folder_size:
                        self.note(nothing_to_post(cfg))
                    wait = FOLDER_RECHECK
                if mode == "upload-post" or self.state.post_in_flight() is not None:
                    missing = self.missing()
                    if missing:
                        self.note(f"not posting through Upload-Post: {missing} "
                                  "(then run scripts/up.sh)")
                    else:
                        wait = min(wait, self.step(start_new=mode == "upload-post"))
            except Exception as e:                       # keep the 24/7 loop alive
                self.log(f"post: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                wait = 60.0
            self.sleep(max(1.0, min(wait, IDLE_RECHECK)))
