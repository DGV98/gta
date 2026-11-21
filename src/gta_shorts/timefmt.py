"""Parse and print the time formats people and Twitch use."""
import re

_HMS_UNITS = re.compile(r"(\d+(?:\.\d+)?)([hms])")


def parse_time(value) -> float:
    """Seconds from ``5025``, ``"1:23:45"``, ``"83:45"``, ``"1h23m45s"`` or ``"90m"``."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty time")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
            raise ValueError(f"bad time: {value!r}")
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return total
    if _HMS_UNITS.sub("", text) == "" and _HMS_UNITS.search(text):
        return sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in _HMS_UNITS.findall(text))
    raise ValueError(f"bad time: {value!r}")


def fmt_time(seconds: float) -> str:
    """``3725.4`` -> ``"1:02:05"``."""
    s = int(round(max(0.0, float(seconds))))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
