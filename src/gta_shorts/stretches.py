"""Choose which stretches of a long VOD to download and clip.

A GTA stream VOD is 4-10 hours; the moments worth posting are a few minutes of
it, and the clips viewers cut while watching say where. Downloading and
transcribing only fixed-length stretches around the densest viewer-clip
activity cuts download, disk and GPU time by an order of magnitude.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class Stretch:
    start: float
    end: float
    clips: int          # viewer clips wholly inside
    views: int          # their summed views

    @property
    def length(self) -> float:
        return self.end - self.start


def _weight(clip) -> int:
    return int(clip.views) + 1          # +1: a clip with no views still counts


def _inside(clip, start: float, end: float) -> bool:
    return start <= clip.start and clip.start + clip.duration <= end


def muted_seconds(start: float, end: float, muted: Sequence[Tuple[float, float]]) -> float:
    """How much of [start, end) falls in Twitch's muted (offset, duration) spans."""
    return sum(max(0.0, min(end, off + dur) - max(start, off)) for off, dur in muted)


def plan_stretches(clips: Sequence, vod_duration: float, length: float, count: int,
                   lead: float = 60.0, min_clips: int = 1,
                   muted: Sequence[Tuple[float, float]] = (),
                   max_muted_fraction: float = 1.0) -> List[Stretch]:
    """Up to ``count`` non-overlapping ``length``-second stretches, hottest first.

    ``clips`` have ``.start`` (VOD seconds), ``.duration`` and ``.views``. Each
    candidate opens ``lead`` seconds before some clip so its moment keeps its
    setup; a candidate's heat is the views (+1 each) of the clips wholly
    inside it. Greedy: take the hottest, drop candidates that overlap it,
    repeat. A stretch with fewer than ``min_clips`` clips is not worth a job.
    A VOD shorter than ``length`` is one stretch covering all of it.

    ``muted`` spans (copyrighted music Twitch silenced) have no audio, so no
    transcript and no reaction: a clip mostly inside one does not count, and
    a stretch more than ``max_muted_fraction`` muted is not taken.
    """
    total = max(0.0, float(vod_duration))
    if count <= 0 or length <= 0 or total <= 0:
        return []
    usable = [c for c in clips if 0 <= c.start < total
              and muted_seconds(c.start, c.start + c.duration, muted) <= c.duration / 2]

    def too_muted(start: float, end: float) -> bool:
        return muted_seconds(start, end, muted) > max_muted_fraction * (end - start)

    if total <= length:
        heat = [c for c in usable if _inside(c, 0.0, total)]
        if len(heat) < min_clips or too_muted(0.0, total):
            return []
        return [Stretch(0.0, round(total, 3), len(heat), sum(c.views for c in heat))]

    candidates = []
    for anchor in usable:
        start = min(max(0.0, anchor.start - lead), total - length)
        end = start + length
        inside = [c for c in usable if _inside(c, start, end)]
        candidates.append((sum(_weight(c) for c in inside), start, end, inside))
    # Hottest first; on a tie the earlier stretch (stable, predictable).
    candidates.sort(key=lambda t: (-t[0], t[1]))

    chosen: List[Stretch] = []
    for _, start, end, inside in candidates:
        if len(chosen) >= count:
            break
        if len(inside) < min_clips:
            continue
        if any(start < s.end and s.start < end for s in chosen):
            continue
        if too_muted(start, end):
            continue
        chosen.append(Stretch(round(start, 3), round(end, 3), len(inside),
                              sum(c.views for c in inside)))
    return chosen


def manual_stretch(start: float, end: float, clips: Iterable = ()) -> Stretch:
    if end <= start:
        raise ValueError("end must be after start")
    inside = [c for c in clips if _inside(c, start, end)]
    return Stretch(round(start, 3), round(end, 3), len(inside), sum(c.views for c in inside))
