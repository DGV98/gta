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
CREATE TABLE IF NOT EXISTS posts (             -- clips going to TikTok (phase 3), one row per clip
    path       TEXT PRIMARY KEY,               -- clips.path
    status     TEXT NOT NULL,                  -- upload-post: posting | posted | inbox | retry | failed
                                               -- folder: exported | manual | expired; either: skipped
    file       TEXT,                           -- its copy's name in the to_post folder
    attempts   INTEGER NOT NULL DEFAULT 0,
    request_id TEXT,                           -- Upload-Post request of the current attempt
    privacy    TEXT,
    started_at TEXT,                           -- current attempt; the daily cap counts these
    sent_at    TEXT,                           -- its last upload (resent if never received)
    posted_at  TEXT,
    next_try   TEXT,
    url        TEXT,
    error      TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS stretches_queue ON stretches (status, priority, views);
CREATE INDEX IF NOT EXISTS submissions_at ON submissions (at);
CREATE INDEX IF NOT EXISTS posts_started ON posts (started_at);
"""
# Posted, or on its way (in the folder): count toward caps and max_per_vod.
LIVE_POSTS = ("posting", "posted", "inbox", "exported", "manual")


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

    # -- posting (phase 3) ------------------------------------------------
    _PRIORITY = "COALESCE((SELECT MAX(ch.priority) FROM channels ch WHERE ch.login = c.login), 0)"
    _CLIP_POST = ("SELECT c.*, p.status AS post_status, COALESCE(p.attempts, 0) AS post_attempts, "
                  f"{_PRIORITY} AS priority FROM clips c LEFT JOIN posts p ON p.path = c.path ")

    def clip_for_post(self, path: str) -> Optional[sqlite3.Row]:
        return self._one(self._CLIP_POST + "WHERE c.path = ?", (path,))

    def clips_to_post(self, now: datetime, *, since: datetime, min_score: int, max_per_vod: int,
                      avoid_login: Optional[str] = None, priority_only: bool = False,
                      limit: int = 1) -> List[sqlite3.Row]:
        """Unposted clips in posting order: priority channels, then not the
        streamer of the last post, then the best score."""
        live = ", ".join(f"'{s}'" for s in LIVE_POSTS)
        return self._all(
            self._CLIP_POST +
            "WHERE c.deleted_at IS NULL AND c.posted_at IS NULL AND c.created_at >= ? "
            "AND COALESCE(c.score, 0) >= ? "
            + (f"AND {self._PRIORITY} = 1 " if priority_only else "") +
            "AND (p.path IS NULL OR (p.status = 'retry' AND (p.next_try IS NULL OR p.next_try <= ?))) "
            "AND (SELECT COUNT(*) FROM posts p2 JOIN clips c2 ON c2.path = p2.path "
            f"     WHERE c2.vod_id = c.vod_id AND p2.status IN ({live})) < ? "
            "ORDER BY priority DESC, c.login = ?, c.score DESC, c.created_at DESC LIMIT ?",
            (iso(since), int(min_score), iso(now), int(max_per_vod), avoid_login or "", limit))

    def unposted_counts(self, since: datetime, min_score: int,
                        priority_only: bool = False) -> Tuple[int, int]:
        """(clips at min_score or above, clips below) not posted or given up on yet."""
        row = self._one(
            "SELECT COALESCE(SUM(COALESCE(c.score, 0) >= ?), 0) AS ok, "
            "COALESCE(SUM(COALESCE(c.score, 0) < ?), 0) AS low FROM clips c "
            "LEFT JOIN posts p ON p.path = c.path WHERE c.deleted_at IS NULL "
            "AND c.posted_at IS NULL AND c.created_at >= ? AND (p.path IS NULL OR p.status = 'retry')"
            + (f" AND {self._PRIORITY} = 1" if priority_only else ""),
            (int(min_score), int(min_score), iso(since)))
        return int(row["ok"]), int(row["low"])

    def post(self, path: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM posts WHERE path = ?", (path,))

    def post_in_flight(self) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM posts WHERE status = 'posting' ORDER BY started_at LIMIT 1")

    def start_post(self, path: str, request_id: str, attempts: int, privacy: str,
                   now: datetime) -> None:
        self.db.execute(
            "INSERT INTO posts(path, status, attempts, request_id, privacy, started_at, sent_at) "
            "VALUES (?, 'posting', ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
            "status = 'posting', attempts = excluded.attempts, request_id = excluded.request_id, "
            "privacy = excluded.privacy, started_at = excluded.started_at, "
            "sent_at = excluded.sent_at, next_try = NULL",
            (path, attempts, request_id, privacy, iso(now), iso(now)))

    def update_post(self, path: str, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE posts SET {cols} WHERE path = ?", (*fields.values(), path))

    def finish_post(self, path: str, status: str, now: datetime, *, url: Optional[str] = None,
                    error: Optional[str] = None) -> None:
        """``posted`` (or ``inbox``: TikTok holds it as a draft for you to publish)."""
        self.update_post(path, status=status, posted_at=iso(now), url=url, error=error)
        self.db.execute("UPDATE clips SET posted_at = ? WHERE path = ?", (iso(now), path))

    def export_post(self, path: str, file: str, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO posts(path, status, file, started_at) VALUES (?, 'exported', ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET status = 'exported', file = excluded.file, "
            "started_at = excluded.started_at, next_try = NULL",
            (path, file, iso(now)))

    def exported(self) -> List[sqlite3.Row]:
        """Clips in the to_post folder, oldest first."""
        return self._all("SELECT p.*, c.login, c.score, c.hook, c.created_at FROM posts p "
                         "JOIN clips c ON c.path = p.path WHERE p.status = 'exported' "
                         "ORDER BY p.started_at, p.file")

    def skip_post(self, path: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO posts(path, status, error) VALUES (?, 'skipped', ?) "
            "ON CONFLICT(path) DO UPDATE SET status = 'skipped', error = excluded.error",
            (path, reason))

    def live_posts_since(self, since: datetime) -> List[sqlite3.Row]:
        """Posts that count toward the daily cap, oldest first."""
        live = ", ".join(f"'{s}'" for s in LIVE_POSTS)
        return self._all(f"SELECT * FROM posts WHERE status IN ({live}) AND started_at >= ? "
                         "ORDER BY started_at", (iso(since),))

    def last_live_post(self) -> Optional[sqlite3.Row]:
        live = ", ".join(f"'{s}'" for s in LIVE_POSTS)
        return self._one(
            f"SELECT p.*, c.login FROM posts p JOIN clips c ON c.path = p.path "
            f"WHERE p.status IN ({live}) ORDER BY p.started_at DESC, p.file DESC, p.rowid DESC "
            "LIMIT 1")

    def count_finished(self, status: str, since: datetime) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM posts WHERE status = ? AND posted_at >= ?",
                             (status, iso(since)))["n"])

    def recent_posts(self, since: datetime) -> List[sqlite3.Row]:
        return self._all(
            "SELECT p.*, c.login, c.score, c.hook FROM posts p JOIN clips c ON c.path = p.path "
            "WHERE p.started_at >= ? ORDER BY p.started_at DESC", (iso(since),))

    def has_table(self, name: str) -> bool:
        return self._one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                         (name,)) is not None

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
