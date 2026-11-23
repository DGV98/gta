"""Python-level stand-ins for the watcher: a scriptable Twitch, a clock, a downloader."""
from datetime import datetime, timedelta, timezone

from gta_shorts.state import iso
from gta_shorts.twitch import Stream, ViewerClip, Vod, VodGone, vod_url

GTA = "999"
T0 = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


def stream(uid, login, viewers, stream_id=None, game=GTA):
    return Stream(user_id=uid, user_login=login, user_name=login.title(), viewer_count=viewers,
                  title="GTA 6", language="en", started_at="", id=stream_id or f"s{uid}",
                  game_id=game)


def vod(vid, uid, login, ended, hours=4.0, stream_id=None, viewable="public", muted=()):
    """A VOD of ``hours`` that ended at ``ended`` (a datetime)."""
    created = ended - timedelta(hours=hours)
    return Vod(id=vid, user_id=uid, user_login=login, user_name=login.title(), title="GTA 6",
               created_at=iso(created), duration=hours * 3600, view_count=0, url=vod_url(vid),
               stream_id=stream_id or f"s{uid}-{vid}", viewable=viewable, muted=tuple(muted))


def clip(start, views, game=GTA, duration=30.0):
    return ViewerClip(start=float(start), duration=duration, views=views, title="c", url="u",
                      game_id=game)


class FakeHelix:
    def __init__(self):
        self.top = []          # live GTA VI streams, most viewers first
        self.live = {}         # user_id -> Stream (any game)
        self.vods = {}         # user_id -> [Vod], newest first
        self.clips = {}        # vod id -> [ViewerClip]
        self.logins = {}       # login -> (user id, display name)
        self.gone = set()      # vod ids that 404

    def game_id(self, name):
        return GTA

    def users(self, logins):
        return {l: self.logins[l] for l in logins if l in self.logins}

    def live_streams(self, game_id, language, limit=20):
        return list(self.top[:limit])

    def streams_for_users(self, user_ids):
        ids = set(user_ids)
        return {k: v for k, v in self.live.items() if k in ids}

    def archive_vods(self, user_id, limit=5):
        return [v for v in self.vods.get(user_id, []) if v.id not in self.gone][:limit]

    def vod(self, vod_id):
        if vod_id in self.gone:
            raise VodGone(f"VOD {vod_id} not found")
        for vods in self.vods.values():
            for v in vods:
                if v.id == vod_id:
                    return v
        raise VodGone(f"VOD {vod_id} not found")

    def vod_clips(self, v, max_clips=500, now=None, game_id=None):
        out = [c for c in self.clips.get(v.id, [])
               if not game_id or not c.game_id or c.game_id == game_id]
        return sorted(out, key=lambda c: -c.views)


class FakeDownloader:
    def __init__(self):
        self.calls = []

    def __call__(self, url, start, end, dest_dir, max_height=1080):
        self.calls.append((url, start, end))
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "source.mp4"
        path.write_bytes(b"\x00" * 2048)
        return path
