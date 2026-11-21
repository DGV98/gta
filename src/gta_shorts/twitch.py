"""Minimal Twitch Helix client: GTA VI streams, a channel's VODs, viewer clips.

Uses an app access token (client credentials), which reads public data only;
no Twitch user account is involved.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional
from urllib.parse import urlparse

import httpx

from .timefmt import parse_time

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
CLIP_LOOKBACK = timedelta(days=60)

_VOD_PATH = re.compile(r"^/(?:[^/]+/)?(?:videos?|v)/(\d+)/?$")


class TwitchError(RuntimeError):
    pass


def parse_vod_id(value: str) -> Optional[str]:
    """``"2600000001"`` or ``twitch.tv/videos/2600000001`` -> the numeric id."""
    text = str(value or "").strip()
    if text.isdigit():
        return text
    if text[:1] in ("v", "V") and text[1:].isdigit():    # yt-dlp prints ids as v<digits>
        return text[1:]
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in ("twitch.tv", "www.twitch.tv", "m.twitch.tv"):
        return None
    m = _VOD_PATH.match(parsed.path or "")
    return m.group(1) if m else None


def vod_url(vod_id: str) -> str:
    return f"https://www.twitch.tv/videos/{vod_id}"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Stream:
    user_id: str
    user_login: str
    user_name: str
    viewer_count: int
    title: str
    language: str
    started_at: str


@dataclass(frozen=True)
class Vod:
    id: str
    user_id: str
    user_login: str
    user_name: str
    title: str
    created_at: str
    duration: float          # seconds (grows while the stream is still live)
    view_count: int
    url: str

    @classmethod
    def from_helix(cls, d: dict) -> "Vod":
        return cls(id=str(d["id"]), user_id=str(d["user_id"]), user_login=d["user_login"],
                   user_name=d.get("user_name") or d["user_login"], title=d.get("title") or "",
                   created_at=d["created_at"], duration=parse_time(d.get("duration") or "0s"),
                   view_count=int(d.get("view_count") or 0), url=d.get("url") or vod_url(d["id"]))


@dataclass(frozen=True)
class ViewerClip:
    start: float             # seconds into the VOD
    duration: float
    views: int
    title: str
    url: str


class Helix:
    def __init__(self, client_id: str, client_secret: str, *,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = 20.0,
                 sleep=time.sleep):
        if not client_id or not client_secret:
            raise TwitchError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required "
                              "(create an app at dev.twitch.tv/console).")
        self._id, self._secret = client_id, client_secret
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._token: Optional[str] = None
        self._sleep = sleep

    # -- plumbing ---------------------------------------------------------
    def _fetch_token(self) -> str:
        r = self._http.post(TOKEN_URL, data={"client_id": self._id, "client_secret": self._secret,
                                             "grant_type": "client_credentials"})
        if r.status_code != 200:
            raise TwitchError(f"Twitch refused the app credentials ({r.status_code}): {r.text[:200]}")
        return r.json()["access_token"]

    def get(self, path: str, params: dict) -> dict:
        for attempt in range(4):
            if self._token is None:
                self._token = self._fetch_token()
            r = self._http.get(f"{HELIX}/{path}", params=params, headers={
                "Client-Id": self._id, "Authorization": f"Bearer {self._token}"})
            if r.status_code == 401 and attempt == 0:
                self._token = None          # expired token: fetch a new one once
                continue
            if r.status_code == 429:
                reset = float(r.headers.get("Ratelimit-Reset") or 0)
                self._sleep(min(60.0, max(1.0, reset - time.time())))
                continue
            if r.status_code >= 500 and attempt < 3:
                self._sleep(2.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise TwitchError(f"Helix {path} -> {r.status_code}: {r.text[:200]}")
            return r.json()
        raise TwitchError(f"Helix {path}: gave up after retries")

    def paginate(self, path: str, params: dict, limit: int) -> Iterator[dict]:
        params = dict(params)
        seen = 0
        while True:
            params["first"] = min(100, max(1, limit - seen))
            body = self.get(path, params)
            for item in body.get("data") or []:
                yield item
                seen += 1
                if seen >= limit:
                    return
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor or not body.get("data"):
                return
            params["after"] = cursor

    # -- queries ----------------------------------------------------------
    def game_id(self, name: str) -> str:
        data = self.get("games", {"name": name}).get("data") or []
        if not data:
            raise TwitchError(f"No Twitch category named {name!r}. Set twitch.game_id in config.toml.")
        return str(data[0]["id"])

    def live_streams(self, game_id: str, language: Optional[str], limit: int = 20) -> List[Stream]:
        params = {"game_id": game_id, "type": "live"}
        if language:
            params["language"] = language
        return [Stream(user_id=str(d["user_id"]), user_login=d["user_login"],
                       user_name=d.get("user_name") or d["user_login"],
                       viewer_count=int(d.get("viewer_count") or 0), title=d.get("title") or "",
                       language=d.get("language") or "", started_at=d.get("started_at") or "")
                for d in self.paginate("streams", params, limit)]

    def user_id(self, login: str) -> str:
        data = self.get("users", {"login": login.strip().lower()}).get("data") or []
        if not data:
            raise TwitchError(f"No Twitch channel named {login!r}.")
        return str(data[0]["id"])

    def archive_vods(self, user_id: str, limit: int = 5) -> List[Vod]:
        return [Vod.from_helix(d) for d in
                self.paginate("videos", {"user_id": user_id, "type": "archive"}, limit)]

    def vod(self, vod_id: str) -> Vod:
        data = self.get("videos", {"id": vod_id}).get("data") or []
        if not data:
            raise TwitchError(f"VOD {vod_id} not found (deleted, expired or sub-only).")
        return Vod.from_helix(data[0])

    def vod_clips(self, vod: Vod, max_clips: int = 500, now: Optional[datetime] = None) -> List[ViewerClip]:
        """Clips viewers cut from this VOD (during the stream or after), most viewed first."""
        created = _ts(vod.created_at)
        now = now or datetime.now(timezone.utc)
        params = {"broadcaster_id": vod.user_id, "started_at": _iso(created),
                  "ended_at": _iso(min(now, created + CLIP_LOOKBACK))}
        out = []
        for c in self.paginate("clips", params, max_clips):
            if str(c.get("video_id") or "") != vod.id or c.get("vod_offset") is None:
                continue      # another VOD's clip, or cut while the VOD was unavailable
            out.append(ViewerClip(start=float(c["vod_offset"]), duration=float(c.get("duration") or 0),
                                  views=int(c.get("view_count") or 0), title=c.get("title") or "",
                                  url=c.get("url") or ""))
        out.sort(key=lambda c: -c.views)
        return out
