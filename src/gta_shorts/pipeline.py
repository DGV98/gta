"""Clip a VOD: plan stretches -> download each -> openshorts -> collect clips.

State lives in ``<run_dir>/manifest.json`` and is written after every step, so
re-running the same command resumes: finished stretches are skipped and a
stretch whose job was already submitted is polled again instead of being
downloaded and processed twice.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .config import Config
from .download import download_stretch, media_duration
from .openshorts_client import JobFailed, OpenShorts
from .stretches import Stretch, plan_stretches
from .twitch import Vod

Log = Callable[[str], None]


def run_dir_for(cfg: Config, vod: Vod) -> Path:
    day = vod.created_at[:10].replace("-", "")
    return cfg.output_dir / f"{day}_{vod.user_login}_{vod.id}"


def vod_link(vod: Vod, seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{vod.url}?t={s // 3600}h{s % 3600 // 60}m{s % 60}s"


def load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def clip_entry(clip: dict, vod: Vod, stretch: Stretch, credit_template: str, filename: str) -> dict:
    """What the posting stage needs for one clip, credit included."""
    credit = credit_template.format(login=vod.user_login, name=vod.user_name)
    start = float(clip.get("start") or 0)
    end = float(clip.get("end") or 0)

    def with_credit(text) -> str:
        text = str(text or "").strip()
        return f"{text}\n\n{credit}" if text else credit

    return {
        "file": filename,
        "duration": round(end - start, 1),
        "vod_start": round(stretch.start + start, 1),
        "vod_end": round(stretch.start + end, 1),
        "vod_link": vod_link(vod, stretch.start + start),
        "score": clip.get("predicted_score"),
        "hook": clip.get("viral_hook_text"),
        "why": clip.get("why"),
        "youtube_title": clip.get("video_title_for_youtube_short"),
        "captions": {
            "tiktok": with_credit(clip.get("video_description_for_tiktok")),
            "instagram": with_credit(clip.get("video_description_for_instagram")),
            "youtube": credit,
        },
        "credit": credit,
        "openshorts": clip,
    }


def plan_vod(cfg: Config, vod: Vod, clips: Sequence, *, count: Optional[int] = None,
             minutes: Optional[float] = None) -> List[Stretch]:
    """The config's stretch plan for a VOD, skipping parts Twitch muted."""
    return plan_stretches(
        clips, vod.duration,
        length=(minutes or cfg.stretch_minutes) * 60,
        count=count or cfg.stretches_per_vod,
        lead=cfg.lead_seconds, min_clips=cfg.min_viewer_clips,
        muted=vod.muted, max_muted_fraction=cfg.max_muted_fraction)


def clip_vod(cfg: Config, vod: Vod, stretches: List[Stretch], api: OpenShorts, *,
             log: Log = print, job_log: Optional[Log] = None,
             downloader=download_stretch, probe=media_duration,
             on_submit: Optional[Callable[[dict], None]] = None) -> dict:
    """Clip ``stretches`` of ``vod``; ``on_submit(entry)`` runs after each job starts."""
    run_dir = run_dir_for(cfg, vod)
    mpath = run_dir / "manifest.json"
    manifest = load_manifest(mpath) or {
        "vod": asdict(vod),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stretches": [],
    }
    known = {(s["start"], s["end"]): s for s in manifest["stretches"]}

    for n, st in enumerate(stretches, 1):
        entry = known.get((st.start, st.end))
        if entry is None:
            entry = {**asdict(st), "dir": f"stretch_{int(st.start):06d}", "status": "planned",
                     "clips": []}
            manifest["stretches"].append(entry)
            known[(st.start, st.end)] = entry
        label = f"[{n}/{len(stretches)}] {vod_link(vod, st.start)} (+{st.length / 60:.0f} min)"
        if entry["status"] == "done":
            log(f"{label}: already done, {len(entry['clips'])} clip(s)")
            continue
        sdir = run_dir / entry["dir"]
        try:
            if not entry.get("job_id"):
                src = sdir / entry["source"] if entry.get("source") else None
                secs = entry.get("source_seconds")
                if src is not None and src.exists() and secs is not None and secs < st.length / 2:
                    # e.g. the empty files an older downloader left: fetch it again
                    log(f"{label}: {src.name} has only {secs:.0f} s of {st.length:.0f}; downloading again")
                    src.unlink()
                if src is None or not src.exists():
                    log(f"{label}: downloading ({st.clips} viewer clip(s) inside)...")
                    src = downloader(vod.url, st.start, st.end, sdir, max_height=cfg.max_height)
                    entry["source"] = src.name
                    entry["source_seconds"] = round(probe(src), 1)
                    save_manifest(mpath, manifest)
                else:
                    log(f"{label}: reusing downloaded {src.name}")
                log(f"{label}: uploading {src.stat().st_size / 1e6:.0f} MB to openshorts")
                upload_id = api.upload(src)
                entry["job_id"] = api.process(upload_id, options=cfg.process_options(),
                                              twitch_vod_url=vod.url, twitch_vod_offset=st.start)
                entry["status"] = "processing"
                save_manifest(mpath, manifest)
                log(f"{label}: job {entry['job_id']} queued")
                if on_submit:
                    on_submit(entry)
            else:
                log(f"{label}: resuming job {entry['job_id']}")
            result = api.wait(entry["job_id"], poll=cfg.poll_seconds,
                              timeout=cfg.timeout_minutes * 60, on_log=job_log)
            cost = (result.get("cost_analysis") or {}).get("total_cost")
            if cost is not None:
                entry["cost_usd"] = round(float(cost), 6)
            clips = sorted(result.get("clips") or [],
                           key=lambda c: -(c.get("predicted_score") or 0))
            entry["clips"] = []
            for k, clip in enumerate(clips, 1):
                name = f"clip{k:02d}.mp4"
                api.download_clip(clip, sdir / name)
                entry["clips"].append(clip_entry(clip, vod, st, cfg.credit, f"{entry['dir']}/{name}"))
            entry["status"] = "done"
            entry.pop("error", None)
            if not cfg.keep_sources and entry.get("source"):
                (sdir / entry["source"]).unlink(missing_ok=True)
            save_manifest(mpath, manifest)
            log(f"{label}: {len(clips)} clip(s) saved to {sdir}")
        except JobFailed as e:
            # openshorts gave up on this job: the next run submits it again
            # (from the kept source when it is still on disk).
            entry["failed_job_id"] = entry.pop("job_id", None)
            entry["status"] = "failed"
            entry["error"] = str(e)
            save_manifest(mpath, manifest)
            log(f"{label}: FAILED in openshorts: {e}")
        except Exception as e:
            # Download/network/timeout: a submitted job stays "processing" and
            # the next run polls it again; an unsubmitted stretch is retried.
            if not entry.get("job_id"):
                entry["status"] = "failed"
            entry["error"] = f"{type(e).__name__}: {e}"
            save_manifest(mpath, manifest)
            log(f"{label}: FAILED: {entry['error']}")
    manifest["path"] = str(mpath)
    return manifest
