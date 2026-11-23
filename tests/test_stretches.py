from dataclasses import dataclass

import pytest

from gta_shorts.stretches import manual_stretch, plan_stretches
from gta_shorts.timefmt import fmt_time, parse_time


@dataclass
class C:
    start: float
    duration: float = 30.0
    views: int = 10


HOUR = 3600.0


def test_picks_the_hottest_stretch_with_lead():
    clips = [C(2 * HOUR, views=5000), C(2 * HOUR + 120, views=3000), C(30 * 60, views=10)]
    [best] = plan_stretches(clips, 6 * HOUR, length=600, count=1, lead=60)
    assert best.start == 2 * HOUR - 60 and best.end == 2 * HOUR + 540
    assert best.clips == 2 and best.views == 8000


def test_stretches_never_overlap_and_come_hottest_first():
    clips = [C(1000, views=100), C(1100, views=100), C(5000, views=900), C(9000, views=50)]
    out = plan_stretches(clips, 4 * HOUR, length=600, count=3, lead=60, min_clips=1)
    assert [s.views for s in out] == [900, 200, 50]
    for a in out:
        for b in out:
            if a is not b:
                assert a.end <= b.start or b.end <= a.start


def test_min_clips_drops_thin_stretches():
    clips = [C(1000, views=99999), C(8000), C(8100), C(8200)]
    out = plan_stretches(clips, 4 * HOUR, length=600, count=2, lead=60, min_clips=2)
    assert len(out) == 1 and out[0].clips == 3


def test_stretch_is_clamped_to_the_end_of_the_vod():
    clips = [C(HOUR - 40, duration=20, views=1)]
    [s] = plan_stretches(clips, HOUR, length=600, count=1)
    assert s.end == HOUR and s.start == HOUR - 600


def test_short_vod_is_one_whole_stretch():
    [s] = plan_stretches([C(100)], 500, length=600, count=2)
    assert (s.start, s.end, s.clips) == (0.0, 500, 1)


def test_no_clips_no_stretches():
    assert plan_stretches([], 6 * HOUR, length=600, count=2) == []


def test_clips_outside_the_vod_are_ignored():
    # A clip offset past the VOD's (current) duration: still-live VOD, bad data.
    assert plan_stretches([C(7 * HOUR)], 6 * HOUR, length=600, count=1) == []


def test_manual_stretch_counts_its_clips():
    s = manual_stretch(100, 700, [C(200, views=5), C(690, views=7)])
    assert (s.clips, s.views) == (1, 5)
    with pytest.raises(ValueError):
        manual_stretch(700, 100)


@pytest.mark.parametrize("text,secs", [
    ("5025", 5025), ("1:23:45", 5025), ("83:45", 5025), ("1h23m45s", 5025),
    ("90m", 5400), ("2h", 7200), (12.5, 12.5),
])
def test_parse_time(text, secs):
    assert parse_time(text) == secs


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "5x", "h"])
def test_parse_time_rejects(bad):
    with pytest.raises(ValueError):
        parse_time(bad)


def test_fmt_time():
    assert fmt_time(3725.4) == "1:02:05" and fmt_time(65) == "1:05"


def test_muted_spans_are_avoided():
    from gta_shorts.stretches import muted_seconds
    clips = [C(3600, 30, 900), C(7200, 30, 10)]
    muted = [(3000.0, 1800.0)]                     # 0:50:00-1:20:00 has no audio
    assert muted_seconds(3540, 4260, muted) == 720
    got = plan_stretches(clips, 6 * 3600, length=720, count=2, muted=muted,
                         max_muted_fraction=0.25)
    assert [s.start for s in got] == [7140.0]
    # Without the muting info the hot clip wins, as before.
    assert plan_stretches(clips, 6 * 3600, length=720, count=1)[0].start == 3540.0


def test_partly_muted_stretch_is_fine_under_the_fraction():
    clips = [C(3600, 30, 900)]
    got = plan_stretches(clips, 6 * 3600, length=720, count=1, muted=[(4100.0, 100.0)],
                         max_muted_fraction=0.25)
    assert [s.start for s in got] == [3540.0]
