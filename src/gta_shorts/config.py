"""config.toml (non-secret settings) + .env (secrets)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class Config:
    # [twitch]
    game_name: str = "Grand Theft Auto VI"
    game_id: Optional[str] = None
    language: Optional[str] = "en"
    # [selection]
    stretches_per_vod: int = 2
    stretch_minutes: float = 12.0
    lead_seconds: float = 60.0
    min_viewer_clips: int = 2
    # [openshorts]
    api_url: str = "http://localhost:8000"
    layouts: List[str] = field(default_factory=lambda: ["screencast"])   # see config.example.toml
    auto_hook: bool = True
    auto_hook_style: Optional[str] = None
    captions: bool = True
    clip_min_seconds: Optional[float] = None
    clip_max_seconds: Optional[float] = None
    target_clips: Optional[int] = None
    poll_seconds: float = 10.0
    timeout_minutes: float = 90.0
    # [download]
    max_height: int = 1080
    # [output]
    output_dir: Path = Path("runs")
    credit: str = "🎥 twitch.tv/{login}"
    keep_sources: bool = False
    # [watch] (the 24/7 loop)
    priority: List[str] = field(default_factory=list)
    discover_top: int = 20
    min_viewers: int = 2000
    track_days: float = 5.0
    poll_minutes: float = 30.0
    settle_minutes: float = 30.0
    max_vod_age_hours: float = 48.0
    max_stretches_per_day: int = 12
    max_usd_per_day: Optional[float] = 1.0
    max_attempts: int = 3
    max_muted_fraction: float = 0.25
    min_free_gb: float = 20.0
    keep_clip_days: float = 14.0
    state_db: Path = Path("state.db")
    # [post] (phase 3: clips for TikTok; keys are post_<key> here)
    post_mode: str = "folder"         # folder | upload-post | off
    post_folder_size: int = 6
    post_user: str = ""
    post_per_day: int = 6
    post_min_gap_minutes: float = 120.0
    post_hours: List[int] = field(default_factory=lambda: [9, 23])
    post_timezone: str = "America/New_York"
    post_min_score: int = 70
    post_max_per_vod: int = 3
    post_max_age_hours: float = 72.0
    post_priority_only: bool = False
    post_privacy: str = "PUBLIC_TO_EVERYONE"
    post_max_attempts: int = 3
    # secrets (.env)
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    upload_post_api_key: str = ""

    def process_options(self) -> dict:
        """The /api/process fields this config sets (absent = openshorts default)."""
        opts: dict = {"layouts": list(self.layouts), "auto_hook": bool(self.auto_hook)}
        if self.auto_hook_style:
            opts["auto_hook_style"] = self.auto_hook_style
        if not self.captions:
            opts["captions"] = False
        for key in ("clip_min_seconds", "clip_max_seconds", "target_clips"):
            if getattr(self, key) is not None:
                opts[key] = getattr(self, key)
        return opts


_SECTIONS = {
    "twitch": ("game_name", "game_id", "language"),
    "selection": ("stretches_per_vod", "stretch_minutes", "lead_seconds", "min_viewer_clips"),
    "openshorts": ("api_url", "layouts", "auto_hook", "auto_hook_style", "captions",
                   "clip_min_seconds", "clip_max_seconds", "target_clips",
                   "poll_seconds", "timeout_minutes"),
    "download": ("max_height",),
    "output": ("dir", "credit", "keep_sources"),
    "watch": ("priority", "discover_top", "min_viewers", "track_days", "poll_minutes",
              "settle_minutes", "max_vod_age_hours", "max_stretches_per_day", "max_usd_per_day",
              "max_attempts", "max_muted_fraction", "min_free_gb", "keep_clip_days", "state_db"),
    "post": ("mode", "folder_size", "min_score", "max_per_vod", "max_age_hours", "priority_only",
             "user", "per_day", "min_gap_minutes", "hours", "timezone", "privacy", "max_attempts"),
}
POST_MODES = ("folder", "upload-post", "off")
PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")


def load(path: Optional[Path] = None) -> Config:
    """Read ``path`` (default ./config.toml, optional) and .env next to it / in cwd."""
    from dotenv import load_dotenv

    path = Path(path) if path else Path("config.toml")
    cfg = Config()
    base = path.parent if path.exists() else Path.cwd()
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for section, keys in _SECTIONS.items():
            for key in keys:
                if key in (data.get(section) or {}):
                    value = data[section][key]
                    if section == "output" and key == "dir":
                        cfg.output_dir = Path(value) if Path(value).is_absolute() else base / value
                    elif key == "state_db":
                        cfg.state_db = Path(value)
                    elif section == "post":
                        setattr(cfg, f"post_{key}", value)
                    else:
                        setattr(cfg, key, value)
        unknown = set(data) - set(_SECTIONS)
        if unknown:
            raise ValueError(f"{path}: unknown section(s) {sorted(unknown)}")
    elif not cfg.output_dir.is_absolute():
        cfg.output_dir = base / cfg.output_dir
    if cfg.language == "":
        cfg.language = None
    if not cfg.state_db.is_absolute():
        cfg.state_db = cfg.output_dir / cfg.state_db
    cfg.priority = [str(l).strip().lower() for l in cfg.priority if str(l).strip()]
    if cfg.max_usd_per_day is not None and cfg.max_usd_per_day <= 0:
        cfg.max_usd_per_day = None
    cfg.post_mode = str(cfg.post_mode).strip().lower()
    if cfg.post_mode not in POST_MODES:
        raise ValueError(f"{path}: [post] mode must be one of {', '.join(POST_MODES)}")
    cfg.post_user = str(cfg.post_user).strip()
    cfg.post_privacy = str(cfg.post_privacy).strip().upper()
    if cfg.post_privacy not in PRIVACY_LEVELS:
        raise ValueError(f"{path}: [post] privacy must be one of {', '.join(PRIVACY_LEVELS)}")
    if len(cfg.post_hours) != 2 or not all(0 <= int(h) <= 24 for h in cfg.post_hours):
        raise ValueError(f"{path}: [post] hours must be [start, end] in 0-24, e.g. [9, 23]")
    cfg.post_hours = [int(h) for h in cfg.post_hours]
    try:
        ZoneInfo(cfg.post_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{path}: [post] timezone {cfg.post_timezone!r} is not a time zone "
                         "name like \"America/New_York\"") from None
    load_dotenv(base / ".env")
    load_dotenv()  # cwd .env too; never overrides variables already set
    cfg.twitch_client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    cfg.twitch_client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    cfg.upload_post_api_key = os.environ.get("UPLOAD_POST_API_KEY", "").strip()
    # The watcher container reaches openshorts by service name, not localhost.
    cfg.api_url = os.environ.get("GTA_SHORTS_API_URL", "").strip() or cfg.api_url
    return cfg
