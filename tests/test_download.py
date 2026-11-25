from pathlib import Path

import pytest

from gta_shorts import download
from gta_shorts.download import (DownloadError, download_stretch, local_playlist, parse_playlist,
                                 pick_segments)

BASE = "https://cdn.example/abc/1080p60/index-muted-X.m3u8"

# The shape Twitch serves now (Nov 2026): fMP4 segments behind an init segment.
FMP4 = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-TWITCH-ELAPSED-SECS:0.000
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
0.mp4
#EXTINF:10.000,
1.mp4
#EXTINF:10.000,
2-muted.mp4
#EXTINF:10.000,
3.mp4
#EXTINF:4.500,
4.mp4
#EXT-X-ENDLIST
"""

# The older shape: MPEG-TS segments, no init segment.
TS = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1.ts
#EXT-X-ENDLIST
"""


def test_parse_fmp4_playlist_gives_vod_times_absolute_uris_and_the_init_segment():
    segs = parse_playlist(FMP4, BASE)
    assert [s.start for s in segs] == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert segs[-1].end == 44.5
    assert segs[2].uri == "https://cdn.example/abc/1080p60/2-muted.mp4"
    assert {s.init for s in segs} == {"https://cdn.example/abc/1080p60/init-0.mp4"}


def test_parse_ts_playlist_has_no_init_and_master_or_encrypted_ones_are_not_handled():
    segs = parse_playlist(TS, BASE)
    assert [s.uri.rsplit("/", 1)[1] for s in segs] == ["0.ts", "1.ts"]
    assert segs[0].init is None
    assert parse_playlist('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n', BASE) == []
    assert parse_playlist('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:10,\n0.ts\n',
                          BASE) == []


def test_pick_segments_covers_the_range_and_nothing_more():
    segs = parse_playlist(FMP4, BASE)
    assert [s.start for s in pick_segments(segs, 12.0, 25.0)] == [10.0, 20.0]
    assert [s.start for s in pick_segments(segs, 10.0, 20.0)] == [10.0]       # boundaries
    assert [s.start for s in pick_segments(segs, 35.0, 999.0)] == [30.0, 40.0]
    assert pick_segments(segs, 50.0, 60.0) == []


def test_local_playlist_is_a_finished_vod_of_just_those_segments():
    parts = pick_segments(parse_playlist(FMP4, BASE), 12.0, 25.0)
    text = local_playlist(parts)
    assert text.startswith("#EXTM3U\n") and text.rstrip().endswith("#EXT-X-ENDLIST")
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
    assert text.count('#EXT-X-MAP:URI="https://cdn.example/abc/1080p60/init-0.mp4"') == 1
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert lines == ["https://cdn.example/abc/1080p60/1.mp4",
                     "https://cdn.example/abc/1080p60/2-muted.mp4"]


class FakeYDL:
    def __init__(self, info=None, error=None):
        self.info, self.error, self.opts = info or {}, error, []

    def __call__(self, opts):
        self.opts.append(opts)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download):
        assert download is False
        if self.error:
            raise self.error
        return self.info


class FakeFFmpeg:
    """Writes each ffmpeg output file and records the commands."""

    def __init__(self, fail_on=None):
        self.cmds, self.playlists, self.fail_on = [], [], fail_on

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        src = cmd[cmd.index("-i") + 1]
        if src.endswith(".m3u8"):
            self.playlists.append(Path(src).read_text())
        n = len(self.cmds)
        rc = 1 if self.fail_on == n else 0
        if rc == 0:
            Path(cmd[-1]).write_bytes(b"mp4")
        return type("R", (), {"returncode": rc, "stderr": "boom" if rc else ""})()


HLS_INFO = {"protocol": "m3u8_native", "url": BASE, "http_headers": {"User-Agent": "x"}}


@pytest.fixture(autouse=True)
def ffmpeg_on_path(monkeypatch):
    monkeypatch.setattr(download.shutil, "which", lambda name: "/usr/bin/" + name)


def test_hls_download_reads_only_the_needed_segments_then_cuts_locally(tmp_path):
    ff, fetched = FakeFFmpeg(), []

    def fetch(url, headers):
        fetched.append((url, headers))
        return FMP4

    path = download_stretch("https://twitch.tv/videos/1", 12.0, 25.0, tmp_path,
                            ydl_factory=FakeYDL(HLS_INFO), fetch=fetch, runner=ff,
                            probe=lambda p: 13.0)
    assert path == tmp_path / "source.mp4" and path.exists()
    assert fetched == [(BASE, {"User-Agent": "x"})]
    first, cut = ff.cmds
    # Step 1: the local playlist from the top: no -ss, no remote playlist.
    assert "-ss" not in first and first[first.index("-i") + 1].endswith("source.parts.m3u8")
    assert "-seg_max_retry" in first and "-rw_timeout" in first
    assert "1.mp4" in ff.playlists[0] and "0.mp4\n" not in ff.playlists[0]
    # Step 2: seek inside the local copy (first part starts at 10 s).
    assert cut[cut.index("-ss") + 1] == "2.000" and cut[cut.index("-t") + 1] == "13.000"
    assert cut[cut.index("-i") + 1].endswith("source.parts.mp4")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source.mp4"]    # temp files gone


def test_a_stretch_past_the_vod_end_is_cut_at_the_end(tmp_path):
    ff = FakeFFmpeg()
    download_stretch("u", 35.0, 120.0, tmp_path, ydl_factory=FakeYDL(HLS_INFO),
                     fetch=lambda u, h: FMP4, runner=ff, probe=lambda p: 9.5)
    assert ff.cmds[1][ff.cmds[1].index("-t") + 1] == "9.500"


def test_a_download_much_shorter_than_asked_is_an_error_and_is_deleted(tmp_path):
    with pytest.raises(DownloadError, match="has 1 s of video, expected 13 s"):
        download_stretch("u", 12.0, 25.0, tmp_path, ydl_factory=FakeYDL(HLS_INFO),
                         fetch=lambda u, h: FMP4, runner=FakeFFmpeg(), probe=lambda p: 1.0)
    assert list(tmp_path.iterdir()) == []


def test_ffmpeg_failure_is_a_download_error_and_leaves_no_temp_files(tmp_path):
    with pytest.raises(DownloadError, match=r"ffmpeg failed \(1\): boom"):
        download_stretch("u", 12.0, 25.0, tmp_path, ydl_factory=FakeYDL(HLS_INFO),
                         fetch=lambda u, h: FMP4, runner=FakeFFmpeg(fail_on=1),
                         probe=lambda p: 13.0)
    assert list(tmp_path.iterdir()) == []


def test_a_range_outside_the_vod_is_an_error(tmp_path):
    with pytest.raises(DownloadError, match="the VOD is 44 s long"):
        download_stretch("u", 50.0, 60.0, tmp_path, ydl_factory=FakeYDL(HLS_INFO),
                         fetch=lambda u, h: FMP4, runner=FakeFFmpeg(), probe=lambda p: 10.0)


def test_yt_dlp_errors_keep_their_message_for_the_subscriber_only_check(tmp_path):
    err = RuntimeError("ERROR: [twitch:vod] v1: ... subscriber-only content")
    with pytest.raises(DownloadError, match="yt-dlp failed on u: .*subscriber-only"):
        download_stretch("u", 0.0, 10.0, tmp_path, ydl_factory=FakeYDL(error=err),
                         fetch=lambda u, h: FMP4, runner=FakeFFmpeg(), probe=lambda p: 10.0)
