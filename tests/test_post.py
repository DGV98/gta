import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fake_upload_post import FakeUploadPost
from fake_world import T0, Clock
from gta_shorts import cli
from gta_shorts import post as post_mod
from gta_shorts.config import Config
from gta_shorts.post import Poster
from gta_shorts.state import State
from gta_shorts.upload_post import UploadPost, tiktok_outcome


class PostWorld:
    def __init__(self, tmp_path, **cfg):
        defaults = dict(output_dir=tmp_path / "runs", state_db=tmp_path / "runs" / "state.db",
                        post_mode="upload-post", post_user="me", upload_post_api_key="k",
                        post_hours=[0, 24], post_timezone="America/New_York",
                        post_min_gap_minutes=60, post_per_day=6, post_min_score=70)
        defaults.update(cfg)
        self.cfg = Config(**defaults)
        self.clock = Clock()
        self.up = FakeUploadPost()
        self.state = State(self.cfg.state_db)
        self.lines = []
        self.api = UploadPost("k", base_url="https://up", transport=httpx.MockTransport(self.up))
        self.p = self.poster()

    def poster(self):
        """A fresh poster on the same state, like the container after a restart."""
        return Poster(self.cfg, self.api, self.state, log=self.lines.append, clock=self.clock,
                      sleep=self.clock.sleep)

    def clip(self, vod_id, login, score, k=1, ago=timedelta(hours=1), caption="Bro got launched #GTA6"):
        run, file = f"20261121_{login}_{vod_id}", f"stretch_000540/clip{k:02d}.mp4"
        path = f"{run}/{file}"
        (self.cfg.output_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (self.cfg.output_dir / path).write_bytes(b"MP4" * 100)
        mpath = self.cfg.output_dir / run / "manifest.json"
        manifest = json.loads(mpath.read_text("utf-8")) if mpath.exists() else {
            "stretches": [{"dir": "stretch_000540", "clips": []}]}
        manifest["stretches"][0]["clips"].append(
            {"file": file, "captions": {"tiktok": f"{caption}\n\n🎥 twitch.tv/{login}"}})
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        self.state.add_clip(path, vod_id, login, 540.0,
                            {"score": score, "duration": 30.0, "hook": f"hook {k}"},
                            self.clock() - ago)
        return path

    def run(self, minutes):
        """Run the 24/7 loop for this long (fake time)."""
        end = self.clock() + timedelta(minutes=minutes)
        self.p.run(stop=lambda: self.clock() >= end)

    def post(self, path):
        return self.state.post(path)


@pytest.fixture()
def world(tmp_path):
    w = PostWorld(tmp_path)
    yield w
    w.state.close()


def test_posts_the_best_clip_with_its_caption_and_records_the_link(world):
    low = world.clip("v1", "big", 71, k=1)
    best = world.clip("v1", "big", 88, k=2, caption="Cops gave up 😭 #GTA6")
    world.run(minutes=2)
    assert world.post(best)["status"] == "posted" and world.post(low) is None
    assert world.post(best)["url"] == "https://www.tiktok.com/@gta6clips/video/1"
    form, headers = world.up.uploads[0]
    assert form["user"] == b"me" and form["platform[]"] == b"tiktok"
    assert form["title"].decode() == "Cops gave up 😭 #GTA6\n\n🎥 twitch.tv/big"
    assert form["privacy_level"] == b"PUBLIC_TO_EVERYONE"
    assert form["disable_inbox_fallback"] == b"true" and form["video"] == b"MP4" * 100
    assert headers["idempotency-key"] == form["request_id"].decode()
    assert world.state._one("SELECT posted_at FROM clips WHERE path = ?", (best,))[0]


def post_times(w):
    return [r["started_at"][8:16].replace("T", " ") for r in
            w.state._all("SELECT started_at FROM posts WHERE status = 'posted' ORDER BY started_at")]


def test_schedule_gap_daily_cap_and_posting_hours(tmp_path):
    # T0 is 12:00 UTC = 07:00 in New York, before the 9-23 window.
    w = PostWorld(tmp_path, post_per_day=2, post_min_gap_minutes=60, post_hours=[9, 23])
    for k in range(1, 5):
        w.clip(f"v{k}", f"s{k}", 80 + k)
    w.run(minutes=2 * 24 * 60)
    # 09:00 EST, an hour later, then each waits for a 24 h slot to free up.
    assert post_times(w) == ["21 14:00", "21 15:00", "22 14:00", "22 15:00"]
    assert any("outside posting hours (9:00-23:00 America/New_York)" in l for l in w.lines)
    assert any("daily cap: 2/2" in l for l in w.lines)
    w.state.close()


def test_score_age_and_per_stream_limits(tmp_path):
    w = PostWorld(tmp_path, post_max_per_vod=1, post_min_gap_minutes=0)
    low = w.clip("v1", "big", 40, k=1)
    old = w.clip("v2", "big", 99, k=1, ago=timedelta(hours=80))
    a = w.clip("v3", "big", 90, k=1)
    b = w.clip("v3", "big", 85, k=2)
    w.run(minutes=30)
    assert w.post(a)["status"] == "posted"
    assert w.post(b) is None and w.post(low) is None and w.post(old) is None
    assert any("nothing to post" in l for l in w.lines)
    w.state.close()


def test_priority_channels_first_then_a_different_streamer(tmp_path):
    w = PostWorld(tmp_path, post_min_gap_minutes=0)
    w.state.set_priority({"prio": ("7", "Prio")}, T0)
    w.clip("v1", "big", 95, k=1)
    w.clip("v1", "big", 94, k=2)
    w.clip("v2", "other", 75)
    w.clip("v3", "prio", 72)
    w.run(minutes=10)
    order = [r["login"] for r in w.state._all(
        "SELECT c.login FROM posts p JOIN clips c ON c.path = p.path ORDER BY p.started_at")]
    assert order == ["prio", "big", "other", "big"]          # not big twice in a row
    w.state.close()


def test_restart_mid_post_polls_instead_of_uploading_again(world):
    path = world.clip("v1", "big", 90)
    world.up.polls_before_done = 10 ** 6
    world.run(minutes=2)
    assert world.post(path)["status"] == "posting" and len(world.up.uploads) == 1
    world.up.polls_before_done = 0
    world.p = world.poster()                                  # container restarted
    world.run(minutes=2)
    assert world.post(path)["status"] == "posted"
    assert len(world.up.uploads) == 1 and len(world.up.published) == 1


def test_interrupted_upload_is_checked_then_sent_again_with_the_same_request(world):
    path = world.clip("v1", "big", 90)
    world.up.drop = True                                      # Upload-Post never got it
    world.run(minutes=5)
    assert len(world.up.uploads) == 1                         # still inside the grace period
    world.up.drop = False
    world.run(minutes=10)
    assert world.post(path)["status"] == "posted" and len(world.up.uploads) == 2
    first, second = (form["request_id"] for form, _ in world.up.uploads)
    assert first == second and len(world.up.published) == 1


def test_never_confirmed_upload_stops_being_resent(world):
    path = world.clip("v1", "big", 90)
    world.up.drop = True
    world.run(minutes=4 * 60)
    row = world.post(path)
    assert row["status"] == "failed" and "never confirmed" in row["error"]
    assert len({form["request_id"] for form, _ in world.up.uploads}) == 1   # one key throughout
    assert 10 <= len(world.up.uploads) <= 20


def test_clip_deleted_while_posting_is_skipped(world):
    path = world.clip("v1", "big", 90)
    world.up.drop = True
    world.run(minutes=1)
    (world.cfg.output_dir / path).unlink()
    world.run(minutes=15)
    assert world.post(path)["status"] == "skipped" and len(world.up.uploads) == 1


def test_failed_post_retries_with_new_requests_then_gives_up(tmp_path):
    w = PostWorld(tmp_path, post_max_attempts=3)
    path = w.clip("v1", "big", 90)
    w.up.fail_with = {"success": False, "error": "TikTok said no", "error_code": "tiktok_publish_failed"}
    w.run(minutes=5)
    row = w.post(path)
    assert (row["status"], row["attempts"]) == ("retry", 1) and "TikTok said no" in row["error"]
    w.run(minutes=20)                                         # retry after 30 min
    assert w.post(path)["attempts"] == 1
    w.run(minutes=90)                                         # then after 60 more
    row = w.post(path)
    assert (row["status"], row["attempts"]) == ("failed", 3)
    assert len({form["request_id"] for form, _ in w.up.uploads}) == 3
    w.state.close()


def test_rejected_media_is_not_retried(world):
    path = world.clip("v1", "big", 90)
    world.up.fail_with = {"success": False, "error": "bad video", "error_code": "tiktok_media_rejected"}
    world.run(minutes=60)
    assert world.post(path)["status"] == "failed" and len(world.up.uploads) == 1


def test_account_trouble_pauses_without_using_up_the_clip(world):
    path = world.clip("v1", "big", 90)
    world.up.upload_error = (429, {"success": False, "message": "Post verification failed",
                                   "violations": [{"platform": "tiktok", "type": "hard_cap",
                                                   "message": "Daily cap reached for tiktok"}]})
    world.run(minutes=30)
    row = world.post(path)
    assert (row["status"], row["attempts"]) == ("retry", 0) and len(world.up.uploads) == 1
    assert any("posting paused for 1 h" in l and "Daily cap reached" in l for l in world.lines)
    world.up.upload_error = None
    world.run(minutes=35)
    row = world.post(path)
    assert (row["status"], row["attempts"]) == ("posted", 1)


def test_inbox_fallback_is_recorded_not_posted_again(world):
    path = world.clip("v1", "big", 90)
    world.clip("v2", "other", 80)
    world.up.inbox = True
    world.run(minutes=30)
    row = world.post(path)
    assert row["status"] == "inbox" and "inbox" in row["error"]
    assert len(world.up.uploads) == 1                         # counts as a post: gap applies


def test_retryable_status_is_left_to_upload_post(world):
    path = world.clip("v1", "big", 90)
    world.up.polls_before_done = 10 ** 6
    world.up.step = "retryable"
    world.run(minutes=30)
    assert world.post(path)["status"] == "posting" and len(world.up.uploads) == 1


def test_off_switch_only_finishes_the_post_in_flight(world):
    path = world.clip("v1", "big", 90)
    world.clip("v2", "other", 80)
    world.up.polls_before_done = 10 ** 6
    world.run(minutes=1)
    world.cfg.post_mode = "off"
    world.up.polls_before_done = 0
    world.run(minutes=300)
    assert world.post(path)["status"] == "posted" and len(world.up.uploads) == 1
    assert any("posting is off" in l for l in world.lines)


def test_missing_key_waits_without_crashing(tmp_path):
    w = PostWorld(tmp_path, upload_post_api_key="")
    w.clip("v1", "big", 90)
    w.run(minutes=30)
    assert w.up.uploads == [] and any("UPLOAD_POST_API_KEY" in l for l in w.lines)
    w.state.close()


def test_missing_clip_file_is_skipped(world):
    gone = world.clip("v1", "big", 95)
    (world.cfg.output_dir / gone).unlink()
    other = world.clip("v2", "other", 80)
    world.run(minutes=2)
    assert world.post(gone)["status"] == "skipped" and world.post(other)["status"] == "posted"


def test_outcome_reads_sync_answers_and_inbox_links():
    sync = {"success": True, "results": {"tiktok": {"success": True, "publish_id": "1",
                                                     "status": "PUBLISH_SUCCESS"}}}
    assert tiktok_outcome(sync).state == "posted"
    inbox = {"status": "completed", "results": [
        {"platform": "tiktok", "success": True, "post_url": "Video sent to Inbox (No Public URL)"}]}
    assert tiktok_outcome(inbox).state == "inbox"
    assert tiktok_outcome({"success": True, "request_id": "r"}).state == "pending"
    skipped = {"status": "completed", "results": [
        {"platform": "tiktok", "skipped": True, "skip_reason": "profile_platform_not_configured"}]}
    out = tiktok_outcome(skipped)
    assert out.state == "failed" and out.account


# -- CLI --------------------------------------------------------------------
@pytest.fixture()
def cli_world(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
    conf = tmp_path / "config.toml"
    conf.write_text('[output]\ndir = "runs"\n[post]\nmode = "upload-post"\nuser = "me"\n'
                    'hours = [0, 24]\n', encoding="utf-8")
    w = PostWorld(tmp_path)
    w.clock.t = datetime.now(timezone.utc).replace(microsecond=0)   # the CLI uses the real clock
    w.up.polls_before_done = 0
    monkeypatch.setattr(cli, "UploadPost",
                        lambda key: UploadPost(key, transport=httpx.MockTransport(w.up)))
    monkeypatch.setattr(post_mod, "POLL_SECONDS", 0.0)
    w.conf = conf
    yield w
    w.state.close()


def test_cli_posts_post_once_private_and_skip(cli_world, capsys):
    w = cli_world
    bad = w.clip("v1", "big", 95, k=1)
    good = w.clip("v1", "big", 90, k=2)
    assert cli.main(["-c", str(w.conf), "posts"]) == 0
    out = capsys.readouterr().out
    assert "Posting: through Upload-Post, 0/6" in out and "2 clip(s) waiting" in out
    assert out.index(bad) < out.index(good)

    assert cli.main(["-c", str(w.conf), "skip", bad]) == 0
    assert cli.main(["-c", str(w.conf), "post", "--once", "--private"]) == 0
    out = capsys.readouterr().out
    assert "posted: https://www.tiktok.com/@gta6clips/video/1" in out
    form, _ = w.up.uploads[0]
    assert form["privacy_level"] == b"SELF_ONLY" and form["external_id"] == good.encode()
    assert w.post(bad)["status"] == "skipped"

    assert cli.main(["-c", str(w.conf), "post", "--once", "--clip", good]) == 1   # already posted
    assert cli.main(["-c", str(w.conf), "posts"]) == 0
    out = capsys.readouterr().out
    assert "https://www.tiktok.com/@gta6clips/video/1" in out and "Up next: nothing" in out


def test_doctor_checks_upload_post(cli_world, capsys, monkeypatch):
    monkeypatch.setattr(cli.OpenShorts, "healthy", lambda self: True)
    cli.main(["-c", str(cli_world.conf), "doctor"])
    out = capsys.readouterr().out
    assert "[ok] Upload-Post key works (plan: basic)" in out
    assert "[ok] TikTok connected to profile 'me' (@gta6clips)" in out
    cli_world.up.tiktok = None
    cli.main(["-c", str(cli_world.conf), "doctor"])
    assert "[FAIL] TikTok connected to profile 'me'" in capsys.readouterr().out


def test_priority_only_posts_clipping_program_channels(tmp_path):
    w = PostWorld(tmp_path, post_min_gap_minutes=0, post_priority_only=True)
    w.state.set_priority({"prio": ("7", "Prio")}, T0)
    other = w.clip("v1", "big", 99)
    mine = w.clip("v2", "prio", 75)
    w.run(minutes=10)
    assert w.post(mine)["status"] == "posted" and w.post(other) is None
    assert w.state.unposted_counts(T0 - timedelta(days=1), 70, priority_only=True) == (0, 0)
    w.state.close()
