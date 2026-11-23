"""An in-memory stand-in for the openshorts REST API (enough for the client)."""
import json
import re

import httpx

_STATUS = re.compile(r"^/api/status/(job\d+)$")


class FakeOpenShorts:
    def __init__(self, clips=None, fail=False, polls_before_done=1, cost=None):
        self.uploads = {}        # upload_id -> bytes
        self.processed = []      # /api/process bodies; job N is processed[N-1]
        self.polls = {}          # job_id -> status calls so far
        self.status_calls = 0
        self.fail = fail
        self.polls_before_done = polls_before_done
        self.cost = cost         # result.cost_analysis.total_cost, when set
        self.down = False        # /health answers 503
        self.lost = set()        # job ids /api/status answers 404 for (server restarted)
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
            return httpx.Response(503 if self.down else 200, json={"ok": not self.down})
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
            return httpx.Response(200, json={"job_id": f"job{len(self.processed)}", "status": "queued"})
        m = _STATUS.match(path)
        if m:
            job = m.group(1)
            if job in self.lost:
                return httpx.Response(404, json={"detail": "Job not found"})
            self.status_calls += 1
            self.polls[job] = self.polls.get(job, 0) + 1
            if self.polls[job] <= self.polls_before_done:
                return httpx.Response(200, json={"status": "processing", "logs": ["Transcribing..."]})
            if self.fail:
                return httpx.Response(200, json={"status": "failed", "logs": ["boom"]})
            result = {"clips": [dict(c, video_url=c["video_url"].replace("job1", job))
                                for c in self.clips]}
            if self.cost is not None:
                result["cost_analysis"] = {"total_cost": self.cost, "model": "fake"}
            return httpx.Response(200, json={"status": "completed",
                                             "logs": ["Transcribing...", "Done"],
                                             "result": result})
        if path.startswith("/videos/"):
            return httpx.Response(200, content=b"MP4:" + path.encode())
        return httpx.Response(404, json={"detail": f"no route {method} {path}"})
