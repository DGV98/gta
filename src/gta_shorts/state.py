"""Watcher state in one SQLite file: tracked channels, VODs, the stretch queue, clips.

Every write commits at once, so a killed process (docker stop, reboot) loses
nothing. Progress inside a stretch (download, openshorts job id) stays in the
VOD's manifest.json as in phase 1; this file decides *what* to work on next.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    user_id       TEXT PRIMARY KEY,
    login         TEXT NOT NULL,
    name          TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,  -- 1 = listed in [watch].priority
    first_seen    TEXT NOT NULL,
    last_seen_gta TEXT,                        -- last seen live in GTA VI above min_viewers
    peak_viewers  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vods (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    login       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    ended_at    TEXT,
    duration    REAL NOT NULL,
    status      TEXT NOT NULL,   -- live | settling | planned | skipped
    reason      TEXT,            -- why skipped
    gta_clips   INTEGER,
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stretches (
    vod_id      TEXT NOT NULL,
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    clips       INTEGER NOT NULL,           -- viewer clips inside
    views       INTEGER NOT NULL,           -- their views: the queue order
    priority    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,              -- queued | processing | done | failed | skipped
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try    TEXT,
    error       TEXT,
    clips_made  INTEGER,
    cost_usd    REAL,
    queued_at   TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (vod_id, start)
);
CREATE TABLE IF NOT EXISTS submissions (       -- one row per openshorts job started
    id       INTEGER PRIMARY KEY,
    vod_id   TEXT NOT NULL,
    start    REAL NOT NULL,
    job_id   TEXT,
    at       TEXT NOT NULL,
    cost_usd REAL
);
CREATE TABLE IF NOT EXISTS clips (             -- finished shorts, for posting (phase 3)
    path          TEXT PRIMARY KEY,            -- relative to the output dir
    vod_id        TEXT NOT NULL,
    login         TEXT NOT NULL,
    stretch_start REAL NOT NULL,
    score         INTEGER,
    duration      REAL,
    hook          TEXT,
    vod_link      TEXT,
    created_at    TEXT NOT NULL,
    posted_at     TEXT,
    deleted_at    TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS stretches_queue ON stretches (status, priority, views);
CREATE INDEX IF NOT EXISTS submissions_at ON submissions (at);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


class State:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = Path(path)
        if readonly:
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30,
                                      isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    def _all(self, sql: str, args: Iterable = ()) -> List[sqlite3.Row]:
        return self.db.execute(sql, tuple(args)).fetchall()

    def _one(self, sql: str, args: Iterable = ()) -> Optional[sqlite3.Row]:
        return self.db.execute(sql, tuple(args)).fetchone()

    # -- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # -- channels ---------------------------------------------------------
    def saw_gta_stream(self, user_id: str, login: str, name: str, viewers: int,
                       now: datetime) -> bool:
        """Record a discovered GTA VI stream; True when the channel is new."""
        new = self._one("SELECT 1 FROM channels WHERE user_id = ?", (user_id,)) is None
        self.db.execute(
            "INSERT INTO channels(user_id, login, name, first_seen, last_seen_gta, peak_viewers) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "login = excluded.login, name = excluded.name, last_seen_gta = excluded.last_seen_gta, "
            "peak_viewers = MAX(peak_viewers, excluded.peak_viewers)",
            (user_id, login, name, iso(now), iso(now), int(viewers)))
        return new

    def set_priority(self, users: Dict[str, Tuple[str, str]], now: datetime) -> None:
        """``users``: login -> (user id, display name). Everyone else loses priority."""
        self.db.execute("UPDATE channels SET priority = 0")
        for login, (user_id, name) in users.items():
            self.db.execute(
                "INSERT INTO channels(user_id, login, name, priority, first_seen) "
                "VALUES (?, ?, ?, 1, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "login = excluded.login, name = excluded.name, priority = 1",
                (user_id, login, name, iso(now)))

    def tracked_channels(self, now: datetime, track_days: float) -> List[sqlite3.Row]:
        cutoff = iso(now - timedelta(days=track_days))
        return self._all("SELECT * FROM channels WHERE priority = 1 OR last_seen_gta >= ? "
                         "ORDER BY priority DESC, peak_viewers DESC", (cutoff,))

    # -- vods -------------------------------------------------------------
    def vod(self, vod_id: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM vods WHERE id = ?", (vod_id,))

    def put_vod(self, vod, status: str, now: datetime, *, reason: Optional[str] = None,
                gta_clips: Optional[int] = None) -> None:
        ended = iso(vod.ended_at) if status != "live" else None
        self.db.execute(
            "INSERT INTO vods(id, user_id, login, created_at, ended_at, duration, status, reason, "
            "gta_clips, first_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ended_at = excluded.ended_at, "
            "duration = excluded.duration, status = excluded.status, reason = excluded.reason, "
            "gta_clips = COALESCE(excluded.gta_clips, gta_clips), updated_at = excluded.updated_at",
            (vod.id, vod.user_id, vod.user_login, vod.created_at, ended, float(vod.duration),
             status, reason, gta_clips, iso(now), iso(now)))

    def skip_vod(self, vod_id: str, reason: str, now: datetime) -> None:
        self.db.execute("UPDATE vods SET status = 'skipped', reason = ?, updated_at = ? "
                        "WHERE id = ?", (reason, iso(now), vod_id))
        self.db.execute("UPDATE stretches SET status = 'skipped', error = ?, finished_at = ? "
                        "WHERE vod_id = ? AND status IN ('queued', 'processing')",
                        (reason, iso(now), vod_id))

    # -- stretch queue ----------------------------------------------------
    def queue_stretches(self, vod_id: str, stretches, priority: bool, now: datetime) -> None:
        for st in stretches:
            self.db.execute(
                "INSERT OR IGNORE INTO stretches(vod_id, start, end, clips, views, priority, "
                "status, queued_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (vod_id, st.start, st.end, st.clips, st.views, int(priority), iso(now)))

    def next_stretch(self, now: datetime) -> Optional[sqlite3.Row]:
        """A stretch whose job is already running first (it is paid for), then
        priority channels, then the most-viewed viewer clips."""
        return self._one(
            "SELECT s.*, v.login, v.ended_at FROM stretches s JOIN vods v ON v.id = s.vod_id "
            "WHERE s.status IN ('queued', 'processing') AND (s.next_try IS NULL OR s.next_try <= ?) "
            "ORDER BY s.status = 'processing' DESC, s.priority DESC, s.views DESC, s.queued_at "
            "LIMIT 1", (iso(now),))

    def queue(self, limit: int = 10) -> List[sqlite3.Row]:
        return self._all(
            "SELECT s.*, v.login FROM stretches s JOIN vods v ON v.id = s.vod_id "
            "WHERE s.status IN ('queued', 'processing') "
            "ORDER BY s.status = 'processing' DESC, s.priority DESC, s.views DESC, s.queued_at "
            "LIMIT ?", (limit,))

    def update_stretch(self, vod_id: str, start: float, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE stretches SET {cols} WHERE vod_id = ? AND start = ?",
                        (*fields.values(), vod_id, start))

    def stretch(self, vod_id: str, start: float) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM stretches WHERE vod_id = ? AND start = ?", (vod_id, start))

    # -- budget -----------------------------------------------------------
    def add_submission(self, vod_id: str, start: float, job_id: Optional[str], now: datetime) -> None:
        self.db.execute("INSERT INTO submissions(vod_id, start, job_id, at) VALUES (?, ?, ?, ?)",
                        (vod_id, start, job_id, iso(now)))

    def set_submission_cost(self, job_id: str, cost_usd: float) -> None:
        self.db.execute("UPDATE submissions SET cost_usd = ? WHERE job_id = ?", (cost_usd, job_id))

    def usage_since(self, since: datetime) -> Tuple[int, float]:
        row = self._one("SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS usd "
                        "FROM submissions WHERE at >= ?", (iso(since),))
        return int(row["n"]), float(row["usd"])

    # -- clips ------------------------------------------------------------
    def add_clip(self, path: str, vod_id: str, login: str, stretch_start: float, clip: dict,
                 now: datetime) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO clips(path, vod_id, login, stretch_start, score, duration, hook, "
            "vod_link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path, vod_id, login, stretch_start, clip.get("score"), clip.get("duration"),
             clip.get("hook"), clip.get("vod_link"), iso(now)))

    def clips_to_expire(self, before: datetime) -> List[sqlite3.Row]:
        return self._all("SELECT * FROM clips WHERE deleted_at IS NULL AND created_at < ?",
                         (iso(before),))

    def mark_clip_deleted(self, path: str, now: datetime) -> None:
        self.db.execute("UPDATE clips SET deleted_at = ? WHERE path = ?", (iso(now), path))

    # -- reporting --------------------------------------------------------
    def counts(self, table: str) -> Dict[str, int]:
        assert table in ("vods", "stretches")
        return {r["status"]: r["n"] for r in
                self._all(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status")}

    def finished_since(self, since: datetime) -> List[sqlite3.Row]:
        return self._all(
            "SELECT s.*, v.login FROM stretches s JOIN vods v ON v.id = s.vod_id "
            "WHERE s.finished_at >= ? ORDER BY s.finished_at DESC", (iso(since),))

    def clip_count_since(self, since: datetime) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM clips WHERE created_at >= ?",
                             (iso(since),))["n"])
