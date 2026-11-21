"""Client for a self-hosted openshorts API (upload -> process -> poll -> download)."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Iterator, Optional

import httpx

CHUNK = 1024 * 1024


class OpenShortsError(RuntimeError):
    pass


class JobFailed(OpenShortsError):
    """openshorts ran the job and it failed (as opposed to: we could not reach it)."""


def _iter_file(path: Path) -> Iterator[bytes]:
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            yield chunk


class OpenShorts:
    def __init__(self, base_url: str, *, transport: Optional[httpx.BaseTransport] = None,
                 timeout: float = 120.0, sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base, timeout=timeout, transport=transport)
        self._sleep = sleep

    @staticmethod
    def _check(r: httpx.Response, what: str) -> dict:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise OpenShortsError(f"{what} -> {r.status_code}: {str(detail)[:300]}")
        return r.json()

    def healthy(self) -> bool:
        try:
            return self._http.get("/health", timeout=10).status_code == 200
        except httpx.HTTPError:
            return False

    def upload(self, path: Path) -> str:
        slot = self._check(self._http.post("/api/uploads", json={"filename": path.name}),
                           "create upload")
        upload_id = slot["upload_id"]
        # PUT to our own base URL: the server builds upload_url from the Host
        # it saw, which is not always reachable from here.
        self._check(self._http.put(f"/api/uploads/{upload_id}", content=_iter_file(path),
                                   timeout=None), "upload bytes")
        return upload_id

    def process(self, upload_id: str, *, options: dict, twitch_vod_url: Optional[str] = None,
                twitch_vod_offset: Optional[float] = None) -> str:
        # acknowledged: openshorts requires the operator to attest they may
        # process the content; this deployment's operator has chosen to.
        body = {"upload_id": upload_id, "acknowledged": True, **options}
        if twitch_vod_url:
            body["twitch_vod_url"] = twitch_vod_url
            body["twitch_vod_offset"] = round(float(twitch_vod_offset or 0), 3)
        out = self._check(self._http.post("/api/process", json=body), "process")
        if not out.get("job_id"):
            raise OpenShortsError(f"process returned no job_id: {out}")
        return out["job_id"]

    def status(self, job_id: str) -> dict:
        return self._check(self._http.get(f"/api/status/{job_id}"), "status")

    def wait(self, job_id: str, *, poll: float = 10.0, timeout: float = 5400.0,
             on_log: Optional[Callable[[str], None]] = None) -> dict:
        """Poll until the job completes; returns its result. Raises on failure/timeout."""
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            st = self.status(job_id)
            logs = st.get("logs") or []
            if on_log:
                for line in logs[seen:]:
                    on_log(line)
            seen = len(logs)
            state = st.get("status")
            if state == "completed":
                return st.get("result") or {}
            if state == "failed":
                tail = " | ".join(str(l) for l in logs[-5:])
                raise JobFailed(f"job {job_id} failed: {tail}")
            if time.monotonic() > deadline:
                raise OpenShortsError(f"job {job_id} still {state} after {timeout / 60:.0f} min")
            self._sleep(poll)

    def download_clip(self, clip: dict, dest: Path) -> Path:
        url = clip.get("download_url") or clip.get("video_url")
        if not url:
            raise OpenShortsError("clip has no video_url")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self._http.stream("GET", url, timeout=None) as r:
            if r.status_code >= 400:
                raise OpenShortsError(f"download {url} -> {r.status_code}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(CHUNK):
                    f.write(chunk)
        tmp.replace(dest)
        return dest
