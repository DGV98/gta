from datetime import datetime, timezone

import httpx
import pytest

from gta_shorts.twitch import Helix, TwitchError, Vod, VodGone, parse_vod_id

VOD_JSON = {"id": "2600000001", "user_id": "42", "user_login": "streamer", "user_name": "Streamer",
            "title": "GTA 6 DAY 2", "created_at": "2026-11-20T18:00:00Z", "duration": "6h3m12s",
            "view_count": 1234, "url": "https://www.twitch.tv/videos/2600000001",
            "stream_id": "315000001", "viewable": "public",
            "muted_segments": [{"duration": 360, "offset": 1800}]}


class FakeTwitch:
    """Just enough Helix: token, games, streams (2 pages), videos, clips."""

    def __init__(self):
        self.tokens = 0
        self.expire_next = False
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "id.twitch.tv":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}"})
        assert request.headers["Client-Id"] == "cid"
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"message": "invalid token"})
        path = url.path.removeprefix("/helix/")
        params = dict(url.params)
        multi = url.params.get_list("user_id") or url.params.get_list("login")
        self.calls.append((path, params))
        if path == "streams" and url.params.get_list("user_id"):
            return httpx.Response(200, json={"data": [
                {"id": "77", "user_id": uid, "user_login": f"u{uid}", "user_name": f"U{uid}",
                 "viewer_count": 10, "game_id": "123", "title": "t", "language": "en",
                 "started_at": "2026-11-21T10:00:00Z"} for uid in multi if uid != "404"],
                "pagination": {}})
        if path == "users":
            return httpx.Response(200, json={"data": [
                {"id": f"id-{l}", "login": l, "display_name": l.upper()} for l in multi
                if l != "nobody"]})
        if path == "videos" and params.get("id") == "404":
            return httpx.Response(404, json={"message": "not found"})
        if path == "videos" and params.get("id") == "0":
            return httpx.Response(200, json={"data": []})
        if path == "games":
            return httpx.Response(200, json={"data": [{"id": "999", "name": params["name"]}]
                                             if params["name"] == "Grand Theft Auto VI" else []})
        if path == "streams":
            page = [{"user_id": str(i), "user_login": f"s{i}", "user_name": f"S{i}",
                     "viewer_count": 1000 - i, "title": "t", "language": "en",
                     "started_at": "2026-11-21T10:00:00Z"} for i in range(3)]
            if "after" not in params:
                return httpx.Response(200, json={"data": page[:2], "pagination": {"cursor": "c1"}})
            return httpx.Response(200, json={"data": page[2:], "pagination": {}})
        if path == "videos":
            return httpx.Response(200, json={"data": [VOD_JSON]})
        if path == "clips":
            return httpx.Response(200, json={"data": [
                {"video_id": "2600000001", "vod_offset": 7200, "duration": 30, "view_count": 50,
                 "title": "small", "url": "u1"},
                {"video_id": "2600000001", "vod_offset": 3600, "duration": 25, "view_count": 900,
                 "title": "big", "url": "u2"},
                {"video_id": "1111", "vod_offset": 10, "duration": 30, "view_count": 5000,
                 "title": "other vod", "url": "u3"},
                {"video_id": "2600000001", "vod_offset": None, "duration": 30, "view_count": 7,
                 "title": "no offset", "url": "u4"},
                {"video_id": "2600000001", "vod_offset": 9000, "duration": 30, "view_count": 600,
                 "title": "just chatting", "url": "u5", "game_id": "509658"},
            ], "pagination": {}})
        return httpx.Response(404)


@pytest.fixture()
def fake():
    return FakeTwitch()


@pytest.fixture()
def helix(fake):
    return Helix("cid", "secret", transport=httpx.MockTransport(fake), sleep=lambda s: None)


def test_needs_credentials():
    with pytest.raises(TwitchError):
        Helix("", "")


def test_game_id_and_missing_category(helix):
    assert helix.game_id("Grand Theft Auto VI") == "999"
    with pytest.raises(TwitchError):
        helix.game_id("Nope")


def test_live_streams_follow_pagination(helix, fake):
    streams = helix.live_streams("999", "en", limit=10)
    assert [s.user_login for s in streams] == ["s0", "s1", "s2"]
    first = [p for path, p in fake.calls if path == "streams"][0]
    assert first["game_id"] == "999" and first["language"] == "en" and first["type"] == "live"


def test_expired_token_is_refreshed_once(helix, fake):
    helix.game_id("Grand Theft Auto VI")
    fake.expire_next = True
    assert helix.game_id("Grand Theft Auto VI") == "999"
    assert fake.tokens == 2


def test_vod_and_its_viewer_clips(helix, fake):
    vod = helix.vod("2600000001")
    assert vod.duration == 6 * 3600 + 3 * 60 + 12 and vod.user_login == "streamer"
    clips = helix.vod_clips(vod, now=datetime(2026, 11, 21, tzinfo=timezone.utc))
    # Other VOD's clips and offset-less clips dropped; most viewed first.
    assert [(c.start, c.views) for c in clips] == [(3600.0, 900), (9000.0, 600), (7200.0, 50)]
    gta = helix.vod_clips(vod, now=datetime(2026, 11, 21, tzinfo=timezone.utc), game_id="999")
    assert [c.start for c in gta] == [3600.0, 7200.0]       # the Just Chatting clip is out
    params = [p for path, p in fake.calls if path == "clips"][0]
    assert params["broadcaster_id"] == "42"
    assert params["started_at"] == "2026-11-20T18:00:00Z"
    assert params["ended_at"] == "2026-11-21T00:00:00Z"


@pytest.mark.parametrize("value,expected", [
    ("2600000001", "2600000001"),
    ("v2600000001", "2600000001"),
    ("https://www.twitch.tv/videos/2600000001", "2600000001"),
    ("twitch.tv/videos/2600000001?t=1h2m3s", "2600000001"),
    ("https://www.twitch.tv/streamer/video/2600000001", "2600000001"),
    ("https://www.twitch.tv/streamer", None),
    ("https://clips.twitch.tv/SomeClip", None),
    ("https://youtube.com/videos/123", None),
])
def test_parse_vod_id(value, expected):
    assert parse_vod_id(value) == expected


def test_vod_from_helix_defaults():
    v = Vod.from_helix({**VOD_JSON, "user_name": None, "url": None})
    assert v.user_name == "streamer" and v.url.endswith("/videos/2600000001")


def test_vod_fields_for_the_watcher():
    v = Vod.from_helix(VOD_JSON)
    assert v.stream_id == "315000001" and v.viewable == "public"
    assert v.muted == ((1800.0, 360.0),)
    assert v.ended_at == datetime(2026, 11, 21, 0, 3, 12, tzinfo=timezone.utc)
    assert Vod.from_helix({**VOD_JSON, "stream_id": None, "muted_segments": None}).muted == ()


def test_streams_for_users_batches_ids(helix, fake):
    ids = [str(i) for i in range(150)] + ["404"]
    live = helix.streams_for_users(ids)
    assert len(live) == 150 and live["5"].id == "77" and live["5"].game_id == "123"
    batches = [p for path, p in fake.calls if path == "streams"]
    assert len(batches) == 2                                 # 100 + 51 ids


def test_users_maps_logins(helix):
    assert helix.users(["Prio", "nobody", " "]) == {"prio": ("id-prio", "PRIO")}


def test_missing_vod_is_vodgone(helix):
    with pytest.raises(VodGone):
        helix.vod("404")
    with pytest.raises(VodGone):
        helix.vod("0")
