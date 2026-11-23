"""The 24/7 loop: find GTA VI streams, wait for their VODs to end, clip the hottest parts.

Every ``poll_minutes`` a scan:
  1. tracks English GTA VI channels streaming to at least ``min_viewers``,
     plus the ``priority`` channels (clipping programs) whatever they play;
  2. checks each tracked channel's recent VODs. A VOD whose stream is still
     live waits; once it ended ``settle_minutes`` ago (viewers have clipped
     it), its GTA VI viewer clips are fetched and its hot stretches queued.

Between scans the queue is worked one stretch at a time: a job openshorts is
already running first, then priority channels, then the most-viewed viewer
clips, within the rolling-24h budget (``max_stretches_per_day``,
``max_usd_per_day``). Stretches of VODs older than ``max_vod_age_hours`` are
dropped as stale.
"""
from __future__ import annotations

import shutil
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import Config
from .download import download_stretch, media_duration
from .openshorts_client import OpenShorts
from .pipeline import clip_vod, plan_vod, run_dir_for, vod_link
from .state import State, iso, parse_iso
from .stretches import Stretch
from .timefmt import fmt_time
from .twitch import Helix, TwitchError, Vod, VodGone

Log = Callable[[str], None]
VODS_PER_CHANNEL = 3          # recent archives looked at per channel per scan
IDLE_RECHECK = 300.0          # seconds between queue/health checks while idle
SOURCE_MAX_AGE = timedelta(days=2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def free_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 1e9


@dataclass
class ScanReport:
    tracked: int = 0
    live: int = 0
    new_channels: List[str] = field(default_factory=list)
    queued: List[str] = field(default_factory=list)       # "login vod: N stretch(es)"
    skipped: List[str] = field(default_factory=list)      # "login vod: reason"

    def summary(self) -> str:
        parts = [f"{self.tracked} channel(s) tracked, {self.live} live"]
        if self.new_channels:
            parts.append("new: " + ", ".join(self.new_channels))
        if self.queued:
            parts.append("queued " + "; ".join(self.queued))
        if self.skipped:
            parts.append("skipped " + "; ".join(self.skipped))
        return " | ".join(parts)


class Watcher:
    def __init__(self, cfg: Config, helix: Helix, api: OpenShorts, state: State, *,
                 log: Log = print, job_log: Optional[Log] = None,
                 clock: Callable[[], datetime] = utcnow, sleep=time.sleep,
                 downloader=download_stretch, probe=media_duration,
                 disk_free: Callable[[Path], float] = free_gb):
        self.cfg, self.helix, self.api, self.state = cfg, helix, api, state
        self.log, self.job_log = log, job_log
        self.clock, self.sleep = clock, sleep
        self.downloader, self.probe, self.disk_free = downloader, probe, disk_free
        self._game_id: Optional[str] = cfg.game_id
        self._last_note: Optional[str] = None

    def note(self, msg: str) -> None:
        """Log a waiting reason once, not on every idle recheck."""
        if msg != self._last_note:
            self.log(msg)
            self._last_note = msg

    def game_id(self) -> str:
        if not self._game_id:
            self._game_id = self.helix.game_id(self.cfg.game_name)
        return str(self._game_id)

    # -- scanning ---------------------------------------------------------
    def scan(self) -> ScanReport:
        now = self.clock()
        cfg, state, rep = self.cfg, self.state, ScanReport()
        gid = self.game_id()

        if cfg.priority:
            found = self.helix.users(cfg.priority)
            missing = sorted(set(cfg.priority) - set(found))
            if missing:
                self.note(f"priority channel(s) not on Twitch: {', '.join(missing)}")
            state.set_priority(found, now)
        else:
            state.set_priority({}, now)

        for s in self.helix.live_streams(gid, cfg.language, cfg.discover_top):
            if s.viewer_count >= cfg.min_viewers:
                if state.saw_gta_stream(s.user_id, s.user_login, s.user_name, s.viewer_count, now):
                    rep.new_channels.append(f"{s.user_login} ({s.viewer_count:,})")

        channels = state.tracked_channels(now, cfg.track_days)
        rep.tracked = len(channels)
        live = self.helix.streams_for_users(c["user_id"] for c in channels)
        rep.live = len(live)
        for ch in channels:
            try:
                vods = self.helix.archive_vods(ch["user_id"], VODS_PER_CHANNEL)
                for i, vod in enumerate(vods):
                    self._check_vod(vod, live.get(ch["user_id"]), newest=(i == 0),
                                    priority=bool(ch["priority"]), gid=gid, now=now, rep=rep)
            except TwitchError as e:                 # one channel's hiccup must not stop the scan
                rep.skipped.append(f"{ch['login']}: Twitch error, retried next scan ({e})")
        state.set_meta("last_scan", iso(now))
        return rep

    def _check_vod(self, vod: Vod, live_stream, *, newest: bool, priority: bool, gid: str,
                   now: datetime, rep: ScanReport) -> None:
        cfg, state = self.cfg, self.state
        row = state.vod(vod.id)
        if row is not None and row["status"] not in ("live", "settling"):
            return                                   # planned or skipped already
        if live_stream is not None and (vod.stream_id == live_stream.id
                                        if vod.stream_id and live_stream.id else newest):
            state.put_vod(vod, "live", now)
            return
        tag = f"{vod.user_login} {vod.id}"
        age = now - vod.ended_at
        if age > timedelta(hours=cfg.max_vod_age_hours):
            if row is not None:                      # don't log every old VOD of a new channel
                rep.skipped.append(f"{tag}: stale")
            state.put_vod(vod, "skipped", now, reason="stale")
            return
        if vod.viewable != "public":
            state.put_vod(vod, "skipped", now, reason="not public")
            rep.skipped.append(f"{tag}: not public")
            return
        if age < timedelta(minutes=cfg.settle_minutes):
            state.put_vod(vod, "settling", now)
            return
        clips = self.helix.vod_clips(vod, game_id=gid)
        stretches = plan_vod(cfg, vod, clips)
        if not stretches:
            reason = ("no GTA VI viewer clips" if not clips
                      else f"no stretch with {cfg.min_viewer_clips}+ unmuted viewer clips")
            state.put_vod(vod, "skipped", now, reason=reason, gta_clips=len(clips))
            rep.skipped.append(f"{tag}: {reason}")
            return
        state.put_vod(vod, "planned", now, gta_clips=len(clips))
        state.queue_stretches(vod.id, stretches, priority, now)
        rep.queued.append(f"{tag}: {len(stretches)} stretch(es), "
                          f"{sum(s.views for s in stretches):,} clip views")

    # -- working the queue -----------------------------------------------
    def budget_left(self, now: datetime) -> Tuple[bool, str]:
        n, usd = self.state.usage_since(now - timedelta(hours=24))
        cfg = self.cfg
        if n >= cfg.max_stretches_per_day:
            return False, f"daily cap: {n}/{cfg.max_stretches_per_day} stretches in the last 24 h"
        if cfg.max_usd_per_day is not None and usd >= cfg.max_usd_per_day:
            return False, f"daily cap: ${usd:.2f}/${cfg.max_usd_per_day:.2f} Gemini in the last 24 h"
        return True, ""

    def process_next(self) -> bool:
        """Work one stretch. False when there is nothing to do right now."""
        now = self.clock()
        cfg, state = self.cfg, self.state
        row = state.next_stretch(now)
        if row is None:
            self.note("queue empty, waiting for streams to end")
            return False
        vod_id, start = row["vod_id"], row["start"]
        if row["attempts"] >= cfg.max_attempts:      # an attempt that crashed before recording
            state.update_stretch(vod_id, start, status="failed", finished_at=iso(now),
                                 error=row["error"] or f"gave up after {row['attempts']} attempts")
            return True
        if row["status"] == "queued":
            if row["ended_at"] and now - parse_iso(row["ended_at"]) > timedelta(hours=cfg.max_vod_age_hours):
                state.update_stretch(vod_id, start, status="skipped", error="stale",
                                     finished_at=iso(now))
                return True
            ok, why = self.budget_left(now)
            if not ok:
                self.note(f"{why}; waiting")
                return False
        if self.disk_free(cfg.output_dir) < cfg.min_free_gb:
            self.note(f"less than {cfg.min_free_gb:.0f} GB free under {cfg.output_dir}; waiting")
            return False
        if not self.api.healthy():
            self.note(f"openshorts not answering at {cfg.api_url}; waiting")
            return False
        try:
            vod = self.helix.vod(vod_id)
        except VodGone as e:
            state.skip_vod(vod_id, "VOD gone", now)
            self.log(f"{row['login']} {vod_id}: {e}; dropped")
            return True
        self._last_note = None
        attempts = row["attempts"] + 1
        state.update_stretch(vod_id, start, status="processing", attempts=attempts, error=None)
        st = Stretch(row["start"], row["end"], row["clips"], row["views"])
        self.log(f"{vod.user_login} {vod_link(vod, st.start)}: stretch {fmt_time(st.start)} "
                 f"(+{st.length / 60:.0f} min, {st.clips} viewer clips, {st.views:,} views), "
                 f"attempt {attempts}")

        def submitted(entry: dict) -> None:
            state.add_submission(vod_id, start, entry.get("job_id"), self.clock())

        manifest = clip_vod(cfg, vod, [st], self.api, log=self.log, job_log=self.job_log,
                            downloader=self.downloader, probe=self.probe, on_submit=submitted)
        entry = next(e for e in manifest["stretches"]
                     if e["start"] == st.start and e["end"] == st.end)
        self._record(vod, st, entry, attempts)
        return True

    def _record(self, vod: Vod, st: Stretch, entry: dict, attempts: int) -> None:
        now, state, cfg = self.clock(), self.state, self.cfg
        if entry.get("status") == "done":
            run = run_dir_for(cfg, vod).name
            for clip in entry.get("clips") or []:
                state.add_clip(f"{run}/{clip['file']}", vod.id, vod.user_login, st.start, clip, now)
            if entry.get("cost_usd") is not None and entry.get("job_id"):
                state.set_submission_cost(entry["job_id"], entry["cost_usd"])
            state.update_stretch(vod.id, st.start, status="done", finished_at=iso(now), error=None,
                                 clips_made=len(entry.get("clips") or []),
                                 cost_usd=entry.get("cost_usd"), next_try=None)
            return
        error = entry.get("error") or "unknown error"
        if "subscriber-only" in error:
            # yt-dlp's message for a sub-only VOD (Helix doesn't flag them):
            # no retry will help, and the VOD's other stretches would fail too.
            state.skip_vod(vod.id, "subscriber-only", now)
            self.log(f"{vod.user_login} {vod.id}: subscriber-only VOD; dropped")
            return
        if attempts >= cfg.max_attempts:
            state.update_stretch(vod.id, st.start, status="failed", error=error,
                                 finished_at=iso(now))
            self.log(f"{vod.user_login} {vod.id} @ {fmt_time(st.start)}: giving up after "
                     f"{attempts} attempts: {error}")
            return
        # A job still running in openshorts is polled again soon; anything
        # else (download failed, job failed or lost) is retried with backoff.
        running = entry.get("status") == "processing" and entry.get("job_id")
        retry = now + (timedelta(minutes=5) if running else timedelta(minutes=20 * attempts))
        state.update_stretch(vod.id, st.start, status="processing" if running else "queued",
                             error=error, next_try=iso(retry))

    # -- housekeeping -----------------------------------------------------
    def cleanup(self) -> int:
        """Delete clip files older than keep_clip_days and stray downloads. Returns files removed."""
        now, cfg = self.clock(), self.cfg
        removed = 0
        for clip in self.state.clips_to_expire(now - timedelta(days=cfg.keep_clip_days)):
            path = cfg.output_dir / clip["path"]
            if path.exists():
                path.unlink()
                removed += 1
            self.state.mark_clip_deleted(clip["path"], now)
        if not cfg.keep_sources and cfg.output_dir.exists():
            cutoff = (now - SOURCE_MAX_AGE).timestamp()
            for src in cfg.output_dir.glob("*/stretch_*/source.*"):
                if src.stat().st_mtime < cutoff:
                    src.unlink()
                    removed += 1
        return removed

    # -- the loop ---------------------------------------------------------
    def _guarded(self, what: str, fn):
        try:
            return fn()
        except TwitchError as e:
            self.log(f"{what}: Twitch: {e}")
        except Exception as e:                       # keep the 24/7 loop alive
            self.log(f"{what}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return None

    def run(self, once: bool = False, stop: Callable[[], bool] = lambda: False) -> None:
        """Scan every poll_minutes and work the queue in between.

        ``once``: one scan, then work the queue until nothing is ready, then return.
        """
        next_scan: Optional[datetime] = None
        while not stop():
            if next_scan is None or self.clock() >= next_scan:
                rep = self._guarded("scan", self.scan)
                if rep is not None:
                    self.log(f"scan: {rep.summary()}")
                removed = self._guarded("cleanup", self.cleanup)
                if removed:
                    self.log(f"cleanup: removed {removed} old file(s)")
                next_scan = self.clock() + timedelta(minutes=self.cfg.poll_minutes)
            worked = self._guarded("process", self.process_next)
            if worked:
                continue
            if once:
                return
            wait = (next_scan - self.clock()).total_seconds()
            self.sleep(max(1.0, min(wait, IDLE_RECHECK)))
