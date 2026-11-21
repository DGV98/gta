"""Download one stretch of a Twitch VOD with yt-dlp (only that stretch).

yt-dlp hands a ranged HLS download to ffmpeg, which fetches just the segments
covering the range: a minute of 1080p60 is ~65 MB and took ~3 s. The file's
second 0 is VOD second ``start`` to the frame (measured on a GTA 6 VOD with
even and odd start offsets), so viewer-clip offsets map onto it exactly.
Needs ``ffmpeg`` on PATH.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional


class DownloadError(RuntimeError):
    pass


def ydl_options(start: float, end: float, outtmpl: str, max_height: int) -> dict:
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


def download_stretch(vod_url: str, start: float, end: float, dest_dir: Path, *,
                     max_height: int = 1080, name: str = "source",
                     ydl_factory: Optional[Callable] = None) -> Path:
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is not on PATH (yt-dlp needs it for ranged downloads).")
    dest_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest_dir / f"{name}.%(ext)s")
    if ydl_factory is None:
        import yt_dlp
        ydl_factory = yt_dlp.YoutubeDL
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
    if path is None or not path.exists() or path.stat().st_size == 0:
        raise DownloadError(f"yt-dlp reported success but no file landed in {dest_dir}")
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
