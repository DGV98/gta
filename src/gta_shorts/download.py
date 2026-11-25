"""Download one stretch of a Twitch VOD (only that stretch).

Twitch serves a VOD as an HLS playlist of 10 s segments. We read the playlist,
keep only the segments that cover the stretch, have ffmpeg copy those from the
start (no seek), then cut that local file to the exact range with an input
seek, as a stream copy. Why not ``ffmpeg -ss <start> -i <playlist>``: on
Twitch's fMP4 playlists (``#EXT-X-MAP``, timestamps starting near 64 s) ffmpeg
7.1's HLS seek reads the VOD from the top and keeps nothing, so a 12-min
stretch took ~10 min and came back as an empty 261-byte mp4. Seeking in a
local mp4 is reliable, and like before the file's second 0 is VOD second
``start`` (to within a frame or two), so viewer-clip offsets map onto it.

A format that isn't HLS falls back to yt-dlp's own ranged download. A result
much shorter than the stretch raises DownloadError instead of being returned.
Needs ``ffmpeg`` and ``ffprobe`` on PATH.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urljoin

import httpx


class DownloadError(RuntimeError):
    pass


# ffmpeg reading a local playlist whose segments are on https. rw_timeout (µs)
# turns a stalled connection into an error instead of a hang; seg_max_retry
# re-requests a segment that failed.
FFMPEG_HLS_INPUT = ["-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                    "-rw_timeout", "30000000", "-seg_max_retry", "5"]
MIN_FRACTION = 0.9        # of the stretch's length, or the download is an error


@dataclass
class Segment:
    start: float              # seconds into the VOD
    duration: float
    uri: str                  # absolute
    init: Optional[str] = None    # absolute #EXT-X-MAP URI in effect (fMP4)
    discontinuity: bool = False

    @property
    def end(self) -> float:
        return self.start + self.duration


def parse_playlist(text: str, base_url: str) -> List[Segment]:
    """The media segments of an HLS media playlist, with their VOD start times.

    Empty for a master playlist or an encrypted one (we don't handle those).
    """
    if "#EXT-X-STREAM-INF" in text:
        return []
    segs: List[Segment] = []
    t, dur, init, disc = 0.0, None, None, False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line:
            return []
        if line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            init = urljoin(base_url, m.group(1)) if m else None
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            disc = True
        elif line.startswith("#EXTINF:"):
            dur = float(line[len("#EXTINF:"):].split(",")[0])
        elif line and not line.startswith("#"):
            if dur is None:
                raise DownloadError(f"playlist segment {line!r} has no #EXTINF")
            segs.append(Segment(t, dur, urljoin(base_url, line), init, disc))
            t, dur, disc = t + dur, None, False
    return segs


def pick_segments(segs: List[Segment], start: float, end: float) -> List[Segment]:
    """The segments overlapping [start, end)."""
    return [s for s in segs if s.start < end and s.end > start]


def local_playlist(parts: List[Segment]) -> str:
    """A VOD playlist of just ``parts`` (absolute URIs), for ffmpeg to read from the top."""
    out = ["#EXTM3U", "#EXT-X-VERSION:6",
           f"#EXT-X-TARGETDURATION:{max(1, math.ceil(max(p.duration for p in parts)))}",
           "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0"]
    init = None
    for i, p in enumerate(parts):
        if p.discontinuity and i:
            out.append("#EXT-X-DISCONTINUITY")
        if p.init and p.init != init:
            out.append(f'#EXT-X-MAP:URI="{p.init}"')
            init = p.init
        out += [f"#EXTINF:{p.duration:.3f},", p.uri]
    out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


def ydl_options(start: float, end: float, outtmpl: str, max_height: int) -> dict:
    """yt-dlp's own ranged download (the fallback for formats that aren't HLS)."""
    from yt_dlp.utils import download_range_func
    return {
        "format": f"best[height<={max_height}]/best",
        "download_ranges": download_range_func(None, [(float(start), float(end))]),
        "force_keyframes_at_cuts": False,     # stream copy: fast, no re-encode
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "overwrites": True,
        "retries": 5,
        "fragment_retries": 10,
    }


def _fetch_text(url: str, headers: dict) -> str:
    last: Optional[Exception] = None
    for _ in range(3):
        try:
            r = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
            r.raise_for_status()
            return r.text
        except httpx.HTTPError as e:
            last = e
    raise DownloadError(f"could not fetch the VOD playlist: {last}")


def _ffmpeg(args: List[str], timeout: float, runner: Callable) -> None:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DownloadError(f"ffmpeg took longer than {timeout / 60:.0f} min") from None
    if r.returncode != 0:
        tail = " | ".join((r.stderr or "").strip().splitlines()[-3:])
        raise DownloadError(f"ffmpeg failed ({r.returncode}): {tail or 'no output'}")


def _hls_stretch(parts: List[Segment], start: float, end: float, dest: Path,
                 runner: Callable) -> None:
    pl = dest.with_name(dest.stem + ".parts.m3u8")
    whole = dest.with_name(dest.stem + ".parts.mp4")
    try:
        pl.write_text(local_playlist(parts))
        length = min(end, parts[-1].end) - start
        _ffmpeg([*FFMPEG_HLS_INPUT, "-i", str(pl), "-c", "copy", "-f", "mp4", str(whole)],
                timeout=300 + 2 * (parts[-1].end - parts[0].start), runner=runner)
        _ffmpeg(["-ss", f"{start - parts[0].start:.3f}", "-t", f"{length:.3f}",
                 "-i", str(whole), "-c", "copy", "-f", "mp4", str(dest)],
                timeout=600, runner=runner)
    finally:
        pl.unlink(missing_ok=True)
        whole.unlink(missing_ok=True)


def _ytdlp_stretch(ydl_factory: Callable, vod_url: str, start: float, end: float,
                   dest_dir: Path, name: str, max_height: int) -> Path:
    outtmpl = str(dest_dir / f"{name}.%(ext)s")
    try:
        with ydl_factory(ydl_options(start, end, outtmpl, max_height)) as ydl:
            info = ydl.extract_info(vod_url, download=True)
    except Exception as e:  # yt-dlp raises its own DownloadError subclasses
        raise DownloadError(f"yt-dlp failed on {vod_url}: {e}") from e
    downloads = (info or {}).get("requested_downloads") or []
    path = Path(downloads[0].get("filepath")) if downloads and downloads[0].get("filepath") else None
    if path is None or not path.exists():
        matches = sorted(dest_dir.glob(f"{name}.*"))
        path = matches[0] if matches else None
    if path is None or not path.exists():
        raise DownloadError(f"yt-dlp reported success but no file landed in {dest_dir}")
    return path


def download_stretch(vod_url: str, start: float, end: float, dest_dir: Path, *,
                     max_height: int = 1080, name: str = "source",
                     ydl_factory: Optional[Callable] = None,
                     fetch: Callable[[str, dict], str] = _fetch_text,
                     runner: Callable = subprocess.run,
                     probe: Optional[Callable[[Path], float]] = None) -> Path:
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is not on PATH (needed to cut the stretch out of the VOD).")
    probe = probe or media_duration
    dest_dir.mkdir(parents=True, exist_ok=True)
    if ydl_factory is None:
        import yt_dlp
        ydl_factory = yt_dlp.YoutubeDL
    try:
        with ydl_factory({"format": f"best[height<={max_height}]/best", "noplaylist": True,
                          "quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(vod_url, download=False) or {}
    except Exception as e:  # sub-only, deleted, geo-blocked: yt-dlp's message says which
        raise DownloadError(f"yt-dlp failed on {vod_url}: {e}") from e

    parts: List[Segment] = []
    if str(info.get("protocol", "")).startswith("m3u8") and info.get("url"):
        segs = parse_playlist(fetch(info["url"], info.get("http_headers") or {}), info["url"])
        parts = pick_segments(segs, start, end)
        if segs and not parts:
            raise DownloadError(f"the VOD is {segs[-1].end:.0f} s long; nothing at {start:.0f}-{end:.0f} s")
    if parts:
        path = dest_dir / f"{name}.mp4"
        _hls_stretch(parts, start, end, path, runner)
        expected = min(end, parts[-1].end) - start
    else:
        path = _ytdlp_stretch(ydl_factory, vod_url, start, end, dest_dir, name, max_height)
        expected = end - start

    got = probe(path) if path.exists() else 0.0
    if got < MIN_FRACTION * expected:
        path.unlink(missing_ok=True)
        raise DownloadError(f"the download has {got:.0f} s of video, expected {expected:.0f} s")
    return path


def media_duration(path: Path) -> float:
    """Duration in seconds via ffprobe, 0.0 if it cannot be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True).stdout
        return float(json.loads(out)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        return 0.0
