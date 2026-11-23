from datetime import timedelta

import httpx
import pytest

from fake_openshorts import FakeOpenShorts
from fake_world import T0, Clock, FakeDownloader, FakeHelix, clip, stream, vod
from gta_shorts.config import Config
from gta_shorts.openshorts_client import OpenShorts
from gta_shorts.state import State
from gta_shorts.watch import Watcher


class World:
    def __init__(self, tmp_path, **cfg):
        defaults = dict(output_dir=tmp_path / "runs", state_db=tmp_path / "runs" / "state.db",
                        min_viewers=1000, settle_minutes=30, max_vod_age_hours=48,
                        stretches_per_vod=2, min_viewer_clips=1, max_usd_per_day=None,
                        poll_seconds=0)
        defaults.update(cfg)
        self.cfg = Config(**defaults)
        self.clock = Clock()
        self.helix = FakeHelix()
        self.os = FakeOpenShorts()
        self.dl = FakeDownloader()
        self.state = State(self.cfg.state_db)
        self.lines = []
        self.api = OpenShorts("http://openshorts:8000", transport=httpx.MockTransport(self.os),
                              sleep=lambda s: None)
        self.w = Watcher(self.cfg, self.helix, self.api, self.state, log=self.lines.append,
                         clock=self.clock, sleep=self.clock.sleep, downloader=self.dl,
                         probe=lambda p: 720.0, disk_free=lambda p: 500.0)

    def channel(self, uid, login, viewers=5000):
        self.helix.top.append(stream(uid, login, viewers))

    def ended_vod(self, vid, uid, login, clips, ago=timedelta(hours=1), **kw):
        v = vod(vid, uid, login, ended=self.clock() - ago, **kw)
        self.helix.vods.setdefault(uid, []).insert(0, v)
        self.helix.clips[vid] = clips
        return v

    def stretch_rows(self):
        return self.state._all("SELECT * FROM stretches ORDER BY vod_id, start")


@pytest.fixture()
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.state.close()


# -- scanning -------------------------------------------------------------
def test_discovery_tracks_big_gta_streams_only(world):
    world.channel("1", "big", 5000)
    world.channel("2", "small", 300)
    rep = world.w.scan()
    assert rep.tracked == 1 and rep.new_channels == ["big (5,000)"]
    assert world.w.scan().new_channels == []                  # only new once


def test_live_vod_waits_then_settles_then_queues_only_gta_stretches(world):
    world.channel("1", "big")
    live_vod = vod("v1", "1", "big", ended=world.clock(), stream_id="live1")
    world.helix.vods["1"] = [live_vod]
    world.helix.live["1"] = stream("1", "big", 5000, stream_id="live1")
    world.helix.clips["v1"] = [clip(3600, 500), clip(3650, 300),
                               clip(9000, 99999, game="other-game")]
    world.w.scan()
    assert world.state.vod("v1")["status"] == "live" and world.stretch_rows() == []

    world.helix.live.clear()                                  # stream ended
    world.clock.advance(minutes=10)
    world.w.scan()
    assert world.state.vod("v1")["status"] == "settling"

    world.clock.advance(minutes=25)                           # 35 min after the end
    rep = world.w.scan()
    assert world.state.vod("v1")["status"] == "planned"
    [row] = world.stretch_rows()                              # nothing around the other game
    assert (row["start"], row["clips"], row["views"]) == (3540.0, 2, 800)
    assert rep.queued and "big v1" in rep.queued[0]


def test_vod_of_a_new_stream_on_the_same_channel_is_not_live(world):
    world.channel("1", "big")
    world.ended_vod("old", "1", "big", [clip(600, 10)], stream_id="s-old")
    world.helix.live["1"] = stream("1", "big", 5000, stream_id="s-new")
    world.w.scan()
    assert world.state.vod("old")["status"] == "planned"


def test_stale_private_and_clipless_vods_are_skipped(world):
    world.channel("1", "big")
    world.ended_vod("stale", "1", "big", [clip(600, 10)], ago=timedelta(hours=60))
    world.ended_vod("private", "1", "big", [clip(600, 10)], viewable="private")
    world.ended_vod("noclips", "1", "big", [clip(600, 10, game="other")])
    world.w.scan()
    reasons = {v: world.state.vod(v)["reason"] for v in ("stale", "private", "noclips")}
    assert reasons == {"stale": "stale", "private": "not public",
                       "noclips": "no GTA VI viewer clips"}
    assert world.stretch_rows() == []


def test_muted_parts_are_not_queued(world):
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(3600, 900), clip(7200, 10)],
                    muted=[(3000, 1800)])                   # the hot part has no audio
    world.w.scan()
    assert [r["start"] for r in world.stretch_rows()] == [7140.0]


def test_priority_channel_is_tracked_without_discovery_and_goes_first(tmp_path):
    w = World(tmp_path, priority=["prio"])
    w.helix.logins["prio"] = ("7", "Prio")
    w.channel("1", "big")
    w.ended_vod("hot", "1", "big", [clip(600, 50000)])
    w.ended_vod("small", "7", "prio", [clip(600, 5)])
    w.w.scan()
    assert w.state.next_stretch(w.clock())["vod_id"] == "small"
    # Taken off the list: back to views order.
    w.cfg.priority = []
    w.w.scan()
    w.state.update_stretch("small", 540.0, priority=0)
    assert w.state.next_stretch(w.clock())["vod_id"] == "hot"
    w.state.close()


# -- working the queue ----------------------------------------------------
def test_run_once_clips_the_queue_and_records_clips(world):
    world.os.cost = 0.004
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(600, 100), clip(5000, 50)])
    world.w.run(once=True)
    rows = world.stretch_rows()
    assert [r["status"] for r in rows] == ["done", "done"]
    assert all(r["clips_made"] == 2 and r["cost_usd"] == 0.004 for r in rows)
    clips = world.state._all("SELECT * FROM clips ORDER BY path")
    assert len(clips) == 4 and clips[0]["login"] == "big"
    assert all((world.cfg.output_dir / c["path"]).exists() for c in clips)
    n, usd = world.state.usage_since(T0 - timedelta(hours=1))
    assert n == 2 and usd == pytest.approx(0.008)
    # Hottest stretch first.
    assert [c[1] for c in world.dl.calls] == [540.0, 4940.0]


def test_daily_stretch_cap_waits_for_the_24h_window(tmp_path):
    w = World(tmp_path, max_stretches_per_day=1, max_vod_age_hours=100)
    w.channel("1", "big")
    w.ended_vod("v1", "1", "big", [clip(600, 100), clip(5000, 50)])
    w.w.run(once=True)
    assert [r["status"] for r in w.stretch_rows()] == ["done", "queued"]
    assert any("daily cap: 1/1" in line for line in w.lines)
    w.clock.advance(hours=25)
    w.w.run(once=True)
    assert [r["status"] for r in w.stretch_rows()] == ["done", "done"]
    w.state.close()


def test_daily_usd_cap(tmp_path):
    w = World(tmp_path, max_usd_per_day=1.0, stretches_per_vod=3)
    w.os.cost = 0.6
    w.channel("1", "big")
    w.ended_vod("v1", "1", "big", [clip(600, 300), clip(5000, 200), clip(9000, 100)])
    w.w.run(once=True)
    assert [r["status"] for r in w.stretch_rows()] == ["done", "done", "queued"]
    assert any("$1.20/$1.00" in line for line in w.lines)
    w.state.close()


def test_failed_stretch_retries_with_backoff_then_gives_up(tmp_path):
    w = World(tmp_path, stretches_per_vod=1, max_attempts=3)
    w.os.fail = True
    w.channel("1", "big")
    w.ended_vod("v1", "1", "big", [clip(600, 100)])
    w.w.run(once=True)
    [r] = w.stretch_rows()
    assert (r["status"], r["attempts"]) == ("queued", 1) and "boom" in r["error"]
    assert r["next_try"] == "2026-11-21T12:20:00Z"
    w.w.run(once=True)                                       # not due yet
    assert w.stretch_rows()[0]["attempts"] == 1
    for minutes in (21, 41):
        w.clock.advance(minutes=minutes)
        w.w.run(once=True)
    [r] = w.stretch_rows()
    assert (r["status"], r["attempts"]) == ("failed", 3)
    assert len(w.dl.calls) == 1 and len(w.os.processed) == 3   # source reused each time
    w.state.close()


def test_job_still_running_is_polled_again_not_resubmitted(world):
    world.os.polls_before_done = 10 ** 6
    world.cfg.timeout_minutes = 0                            # the wait gives up at once
    world.cfg.stretches_per_vod = 1
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(600, 100)])
    world.w.run(once=True)
    [r] = world.stretch_rows()
    assert r["status"] == "processing" and r["next_try"] == "2026-11-21T12:05:00Z"
    world.os.polls_before_done = 0
    world.cfg.timeout_minutes = 90
    world.clock.advance(minutes=6)
    world.cfg.max_stretches_per_day = 0                      # a running job ignores the cap
    world.w.run(once=True)
    assert world.stretch_rows()[0]["status"] == "done" and len(world.os.processed) == 1


def test_lost_job_is_resubmitted(world):
    world.os.polls_before_done = 10 ** 6
    world.cfg.timeout_minutes = 0
    world.cfg.stretches_per_vod = 1
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(600, 100)])
    world.w.run(once=True)
    world.os.lost.add("job1")                                # openshorts restarted
    world.os.polls_before_done = 0
    world.cfg.timeout_minutes = 90
    world.clock.advance(minutes=6)
    world.w.run(once=True)                                   # 404 -> failed -> requeued
    world.clock.advance(minutes=41)
    world.w.run(once=True)
    assert world.stretch_rows()[0]["status"] == "done" and len(world.os.processed) == 2


def test_gone_vod_drops_its_stretches(world):
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(600, 100), clip(5000, 50)])
    world.w.scan()
    world.helix.gone.add("v1")
    assert world.w.process_next() is True
    assert {r["status"] for r in world.stretch_rows()} == {"skipped"}
    assert world.state.vod("v1")["reason"] == "VOD gone"


def test_openshorts_down_or_disk_full_waits_without_spending(world):
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(600, 100)])
    world.w.scan()
    world.os.down = True
    assert world.w.process_next() is False
    world.os.down = False
    world.w.disk_free = lambda p: 1.0
    assert world.w.process_next() is False
    assert all(r["status"] == "queued" and r["attempts"] == 0 for r in world.stretch_rows())
    assert world.dl.calls == [] and world.state.usage_since(T0 - timedelta(days=1))[0] == 0


def test_queued_stretch_goes_stale(world):
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(600, 100)])
    world.w.scan()
    world.clock.advance(hours=48)
    assert world.w.process_next() is True
    assert world.stretch_rows()[0]["error"] == "stale" and world.dl.calls == []


def test_cleanup_removes_old_clips_and_stray_sources(world):
    import os
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(600, 100)])
    world.cfg.stretches_per_vod = 1
    world.w.run(once=True)
    stray = world.cfg.output_dir / "x" / "stretch_000001" / "source.mp4"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    old = (T0 - timedelta(days=3)).timestamp()
    os.utime(stray, (old, old))
    assert world.w.cleanup() == 1                            # clips are still fresh
    world.clock.advance(days=15)
    assert world.w.cleanup() == 2
    assert all(c["deleted_at"] for c in world.state._all("SELECT * FROM clips"))


def test_loop_scans_every_poll_interval(world):
    scans = []
    real_scan = world.w.scan
    world.w.scan = lambda: scans.append(world.clock()) or real_scan()
    world.w.run(stop=lambda: world.clock() >= T0 + timedelta(minutes=65))
    assert [round((t - T0).total_seconds() / 60) for t in scans] == [0, 30, 60]


def test_loop_survives_errors(world):
    def boom():
        raise RuntimeError("bug")
    world.w.scan = boom
    world.w.run(once=True)
    assert any("scan: RuntimeError: bug" in line for line in world.lines)


def test_scan_against_the_real_helix_client(tmp_path):
    """The watcher's calls match the real Helix client (HTTP-level fake Twitch)."""
    from datetime import datetime, timezone
    from test_twitch import FakeTwitch
    from gta_shorts.twitch import Helix
    w = World(tmp_path, min_viewers=999, priority=["prio"], min_viewer_clips=1)
    w.w.helix = Helix("cid", "secret", transport=httpx.MockTransport(FakeTwitch()),
                      sleep=lambda s: None)
    w.clock.t = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)   # VOD ended 00:03
    rep = w.w.scan()
    assert [c.split(" ")[0] for c in rep.new_channels] == ["s0", "s1"]
    assert rep.tracked == 3 and rep.live == 3                    # s0, s1 + priority
    assert w.state.vod("2600000001")["status"] == "planned"
    assert [r["start"] for r in w.stretch_rows()] == [3540.0, 7140.0]
    w.state.close()


def test_subscriber_only_vod_is_dropped_on_the_first_try(world):
    from gta_shorts.download import DownloadError

    def sub_only(*a, **k):
        raise DownloadError("yt-dlp failed on x: ERROR: [twitch:vod] v1: You must be logged into "
                            "an account that has access to this subscriber-only content")
    world.w.downloader = sub_only
    world.channel("1", "big")
    world.ended_vod("v1", "1", "big", [clip(600, 100), clip(5000, 50)])
    world.w.run(once=True)
    assert {r["status"] for r in world.stretch_rows()} == {"skipped"}
    assert world.state.vod("v1")["reason"] == "subscriber-only"


def test_one_channel_failing_does_not_stop_the_scan(world):
    from gta_shorts.twitch import TwitchError
    world.channel("1", "flaky")
    world.channel("2", "fine")
    world.ended_vod("v2", "2", "fine", [clip(600, 100)])
    real = world.helix.archive_vods

    def archive_vods(uid, limit=5):
        if uid == "1":
            raise TwitchError("Helix videos -> 503")
        return real(uid, limit)
    world.helix.archive_vods = archive_vods
    rep = world.w.scan()
    assert world.state.vod("v2")["status"] == "planned"
    assert any(s.startswith("flaky: Twitch error") for s in rep.skipped)
