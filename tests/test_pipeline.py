import json

import httpx
import pytest

from fake_openshorts import FakeOpenShorts
from gta_shorts.config import Config
from gta_shorts.openshorts_client import JobFailed, OpenShorts
from gta_shorts.pipeline import clip_entry, clip_vod, run_dir_for
from gta_shorts.stretches import Stretch
from gta_shorts.twitch import Vod

VOD = Vod(id="2600000001", user_id="42", user_login="streamer", user_name="Streamer",
          title="GTA 6", created_at="2026-11-20T18:00:00Z", duration=6 * 3600.0,
          view_count=1, url="https://www.twitch.tv/videos/2600000001")
ST = Stretch(start=3540.0, end=4260.0, clips=3, views=1200)


def _api(fake):
    return OpenShorts("http://openshorts:8000", transport=httpx.MockTransport(fake),
                      sleep=lambda s: None)


class FakeDownloader:
    def __init__(self):
        self.calls = []

    def __call__(self, url, start, end, dest_dir, max_height=1080):
        self.calls.append((url, start, end))
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "source.mp4"
        path.write_bytes(b"\x00" * 2048)
        return path


@pytest.fixture()
def cfg(tmp_path):
    return Config(output_dir=tmp_path / "runs")


# -- client ---------------------------------------------------------------
def test_upload_puts_to_our_base_url_not_the_servers(tmp_path):
    fake = FakeOpenShorts()
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 3000)
    uid = _api(fake).upload(f)
    assert fake.uploads[uid] == b"x" * 3000


def test_process_sends_rights_ack_options_and_vod_ref():
    fake = FakeOpenShorts()
    fake.uploads["up1"] = b"x"
    job = _api(fake).process("up1", options={"layouts": ["auto"], "auto_hook": True},
                             twitch_vod_url=VOD.url, twitch_vod_offset=3540)
    assert job == "job1"
    body = fake.processed[0]
    assert body["acknowledged"] is True and body["layouts"] == ["auto"]
    assert body["twitch_vod_url"] == VOD.url and body["twitch_vod_offset"] == 3540


def test_wait_streams_logs_and_raises_jobfailed():
    lines = []
    assert _api(FakeOpenShorts()).wait("job1", on_log=lines.append)["clips"]
    assert lines == ["Transcribing...", "Done"]       # each line once
    with pytest.raises(JobFailed):
        _api(FakeOpenShorts(fail=True)).wait("job1")


def test_config_process_options_only_sends_what_is_set():
    assert Config().process_options() == {"layouts": ["stream"], "auto_hook": True}
    opts = Config(captions=False, target_clips=4, clip_max_seconds=45).process_options()
    assert opts["captions"] is False and opts["target_clips"] == 4 and opts["clip_max_seconds"] == 45


# -- pipeline -------------------------------------------------------------
def test_clip_vod_end_to_end(cfg):
    fake, dl = FakeOpenShorts(), FakeDownloader()
    m = clip_vod(cfg, VOD, [ST], _api(fake), log=lambda s: None, downloader=dl,
                 probe=lambda p: 719.0)
    run_dir = run_dir_for(cfg, VOD)
    assert run_dir.name == "20261120_streamer_2600000001"
    assert dl.calls == [(VOD.url, 3540.0, 4260.0)]
    assert fake.processed[0]["twitch_vod_offset"] == 3540.0

    [s] = m["stretches"]
    assert s["status"] == "done" and s["source_seconds"] == 719.0
    # Best clip first, files downloaded, source deleted (keep_sources off).
    assert [c["score"] for c in s["clips"]] == [88, 71]
    assert (run_dir / s["clips"][0]["file"]).read_bytes() == b"MP4:/videos/job1/clip_b.mp4"
    assert not (run_dir / s["dir"] / "source.mp4").exists()
    on_disk = json.loads((run_dir / "manifest.json").read_text())
    assert on_disk["stretches"][0]["clips"][1]["vod_start"] == 3550.0


def test_rerun_skips_done_stretches(cfg):
    fake, dl = FakeOpenShorts(), FakeDownloader()
    clip_vod(cfg, VOD, [ST], _api(fake), log=lambda s: None, downloader=dl, probe=lambda p: 1.0)
    clip_vod(cfg, VOD, [ST], _api(fake), log=lambda s: None, downloader=dl, probe=lambda p: 1.0)
    assert len(dl.calls) == 1 and len(fake.processed) == 1


def test_interrupted_wait_resumes_the_same_job(cfg):
    class Boom(OpenShorts):
        def wait(self, *a, **k):
            raise TimeoutError("network blip")
    fake, dl = FakeOpenShorts(), FakeDownloader()
    m = clip_vod(cfg, VOD, [ST], Boom("http://o", transport=httpx.MockTransport(fake)),
                 log=lambda s: None, downloader=dl, probe=lambda p: 1.0)
    assert m["stretches"][0]["status"] == "processing" and m["stretches"][0]["job_id"] == "job1"
    m = clip_vod(cfg, VOD, [ST], _api(fake), log=lambda s: None, downloader=dl, probe=lambda p: 1.0)
    assert m["stretches"][0]["status"] == "done"
    assert len(dl.calls) == 1 and len(fake.processed) == 1   # polled, not resubmitted


def test_failed_job_is_resubmitted_from_the_kept_source(cfg):
    dl = FakeDownloader()
    m = clip_vod(cfg, VOD, [ST], _api(FakeOpenShorts(fail=True)), log=lambda s: None,
                 downloader=dl, probe=lambda p: 719.0)
    s = m["stretches"][0]
    assert s["status"] == "failed" and "job_id" not in s and s["failed_job_id"] == "job1"
    fake = FakeOpenShorts()
    m = clip_vod(cfg, VOD, [ST], _api(fake), log=lambda s: None, downloader=dl, probe=lambda p: 719.0)
    assert m["stretches"][0]["status"] == "done"
    assert len(dl.calls) == 1 and len(fake.processed) == 1    # reused the download


def test_a_kept_source_much_shorter_than_the_stretch_is_downloaded_again(cfg):
    # What the old downloader left on fMP4 VODs: a 261-byte mp4, source_seconds 0.
    dl = FakeDownloader()
    clip_vod(cfg, VOD, [ST], _api(FakeOpenShorts(fail=True)), log=lambda s: None,
             downloader=dl, probe=lambda p: 0.0)
    lines = []
    m = clip_vod(cfg, VOD, [ST], _api(FakeOpenShorts()), log=lines.append, downloader=dl,
                 probe=lambda p: 719.0)
    assert len(dl.calls) == 2 and m["stretches"][0]["status"] == "done"
    assert m["stretches"][0]["source_seconds"] == 719.0
    assert any("has only 0 s of 720; downloading again" in l for l in lines)


def test_clip_entry_adds_credit_and_vod_time():
    clip = {"start": 12.0, "end": 41.5, "predicted_score": 80, "viral_hook_text": "h",
            "video_description_for_tiktok": "desc #GTA6", "video_description_for_instagram": None,
            "video_title_for_youtube_short": "t"}
    e = clip_entry(clip, VOD, ST, "🎥 twitch.tv/{login}", "stretch/clip01.mp4")
    assert e["captions"]["tiktok"] == "desc #GTA6\n\n🎥 twitch.tv/streamer"
    assert e["captions"]["instagram"] == "🎥 twitch.tv/streamer"
    assert e["vod_start"] == 3552.0 and e["duration"] == 29.5
    assert e["vod_link"] == "https://www.twitch.tv/videos/2600000001?t=0h59m12s"


def test_unknown_job_is_jobfailed_not_polled_forever():
    fake = FakeOpenShorts()
    fake.lost.add("job9")
    with pytest.raises(JobFailed):
        _api(fake).wait("job9")


def test_clip_vod_reports_submissions_and_cost(cfg):
    fake, seen = FakeOpenShorts(cost=0.0123), []
    m = clip_vod(cfg, VOD, [ST], _api(fake), log=lambda s: None, downloader=FakeDownloader(),
                 probe=lambda p: 1.0, on_submit=lambda e: seen.append(e["job_id"]))
    assert seen == ["job1"] and m["stretches"][0]["cost_usd"] == 0.0123
