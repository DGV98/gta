"""An in-memory stand-in for the openshorts REST API (enough for the client)."""
import json

import httpx


class FakeOpenShorts:
    def __init__(self, clips=None, fail=False, polls_before_done=1):
        self.uploads = {}        # upload_id -> bytes
        self.processed = []      # /api/process bodies
        self.status_calls = 0
        self.fail = fail
        self.polls_before_done = polls_before_done
        self.clips = clips if clips is not None else [
            {"start": 10.0, "end": 40.0, "predicted_score": 71, "viral_hook_text": "He flew 💀",
             "video_description_for_tiktok": "Bro got launched #GTA6",
             "video_description_for_instagram": "Launched #GTA6",
             "video_title_for_youtube_short": "Launched by a truck", "why": "physics",
             "video_url": "/videos/job1/clip_a.mp4"},
            {"start": 100.0, "end": 130.0, "predicted_score": 88, "viral_hook_text": "Cops gave up 😭",
             "video_description_for_tiktok": "", "video_description_for_instagram": "Chase",
             "video_title_for_youtube_short": "Chase", "why": "chase",
             "video_url": "/videos/job1/clip_b.mp4"},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/uploads" and method == "POST":
            uid = f"up{len(self.uploads) + 1}"
            self.uploads[uid] = b""
            return httpx.Response(200, json={"upload_id": uid,
                                             "upload_url": f"http://elsewhere:8000/api/uploads/{uid}"})
        if path.startswith("/api/uploads/") and method == "PUT":
            uid = path.rsplit("/", 1)[1]
            body = request.read()
            if uid not in self.uploads or not body:
                return httpx.Response(400, json={"detail": "bad upload"})
            self.uploads[uid] = body
            return httpx.Response(200, json={"upload_id": uid, "bytes": len(body)})
        if path == "/api/process":
            body = json.loads(request.content)
            if not body.get("acknowledged"):
                return httpx.Response(400, json={"detail": "You must confirm"})
            if body.get("upload_id") not in self.uploads:
                return httpx.Response(404, json={"detail": "unknown upload"})
            self.processed.append(body)
            return httpx.Response(200, json={"job_id": "job1", "status": "queued"})
        if path == "/api/status/job1":
            self.status_calls += 1
            if self.status_calls <= self.polls_before_done:
                return httpx.Response(200, json={"status": "processing", "logs": ["Transcribing..."]})
            if self.fail:
                return httpx.Response(200, json={"status": "failed", "logs": ["boom"]})
            return httpx.Response(200, json={"status": "completed",
                                             "logs": ["Transcribing...", "Done"],
                                             "result": {"clips": self.clips}})
        if path.startswith("/videos/job1/"):
            return httpx.Response(200, content=b"MP4:" + path.encode())
        return httpx.Response(404, json={"detail": f"no route {method} {path}"})
