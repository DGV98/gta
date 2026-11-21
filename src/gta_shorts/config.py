"""config.toml (non-secret settings) + .env (secrets)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


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
    layouts: List[str] = field(default_factory=lambda: ["auto"])
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
    # secrets (.env)
    twitch_client_id: str = ""
    twitch_client_secret: str = ""

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
}


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
                    else:
                        setattr(cfg, key, value)
        unknown = set(data) - set(_SECTIONS)
        if unknown:
            raise ValueError(f"{path}: unknown section(s) {sorted(unknown)}")
    elif not cfg.output_dir.is_absolute():
        cfg.output_dir = base / cfg.output_dir
    if cfg.language == "":
        cfg.language = None
    load_dotenv(base / ".env")
    load_dotenv()  # cwd .env too; never overrides variables already set
    cfg.twitch_client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    cfg.twitch_client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    return cfg
