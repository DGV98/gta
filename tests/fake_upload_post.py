"""An in-memory stand-in for the Upload-Post API (upload, status, account checks)."""
import re

import httpx


def _form(request: httpx.Request) -> dict:
    """Multipart fields -> bytes (enough for what our client sends)."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    fields = {}
    for part in request.read().split(b"--" + boundary):
        head, sep, body = part.partition(b"\r\n\r\n")
        m = re.search(rb'name="([^"]+)"', head)
        if sep and m:
            fields[m.group(1).decode()] = body[:-2]
    return fields


class FakeUploadPost:
    def __init__(self):
        self.uploads = []            # every /api/upload call: (form fields, headers)
        self.published = []          # request ids that became a post (no double posts!)
        self.requests = {}           # request_id -> {"polls": n}
        self.keys = {}               # Idempotency-Key -> request_id
        self.polls_before_done = 1
        self.upload_error = None     # (status, body) answered to /api/upload
        self.fail_with = None        # per-platform result dict for a failed post
        self.inbox = False           # TikTok's user cap hit: video lands in the inbox
        self.drop = False            # accept the upload but forget it (never received)
        self.step = "processing"     # per-platform status while in progress
        self.plan = "basic"
        self.tiktok = {"handle": "gta6clips", "reauth_required": False}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if request.headers.get("authorization") != "Apikey k":
            return httpx.Response(401, json={"success": False, "message": "Invalid API key"})
        if path == "/api/uploadposts/me":
            return httpx.Response(200, json={"success": True, "message": "Token is valid",
                                             "plan": self.plan})
        if path.startswith("/api/uploadposts/users/"):
            user = path.rsplit("/", 1)[1]
            if user != "me":
                return httpx.Response(404, json={"success": False, "message": "Profile not found"})
            return httpx.Response(200, json={"success": True, "profile": {
                "username": "me", "social_accounts": {"tiktok": self.tiktok}}})
        if path == "/api/upload" and method == "POST":
            form = _form(request)
            self.uploads.append((form, dict(request.headers)))
            if self.upload_error:
                status, body = self.upload_error
                return httpx.Response(status, json=body)
            key = request.headers.get("idempotency-key")
            rid = form["request_id"].decode()
            if key in self.keys:                         # replay: the original request
                return httpx.Response(200, json={"success": True, "request_id": self.keys[key]})
            if not self.drop:
                self.keys[key] = rid
                self.requests[rid] = {"polls": 0}
            return httpx.Response(200, json={"success": True, "request_id": rid,
                                             "message": "Upload initiated successfully in background."})
        if path == "/api/uploadposts/status":
            rid = request.url.params.get("request_id")
            req = self.requests.get(rid)
            if req is None:
                return httpx.Response(404, json={"request_id": rid, "status": "not_found",
                                                 "message": "No upload request found with this ID"})
            req["polls"] += 1
            if req["polls"] <= self.polls_before_done:
                return httpx.Response(200, json={"request_id": rid, "status": "in_progress",
                                                 "completed": 0, "total": 1, "results": [
                    {"platform": "tiktok", "success": True, "message": "Queued",
                     "status": self.step}]})
            if self.fail_with:
                return httpx.Response(200, json={"request_id": rid, "status": "failed",
                                                 "completed": 1, "total": 1, "results": [
                    {"platform": "tiktok", "status": "failed", **self.fail_with}]})
            if rid not in self.published:
                self.published.append(rid)
            n = self.published.index(rid) + 1
            result = {"platform": "tiktok", "success": True, "status": "completed",
                      "post_url": f"https://www.tiktok.com/@gta6clips/video/{n}",
                      "platform_post_id": str(n)}
            if self.inbox:
                result.update(post_url=None, fallback_to_inbox=True)
            return httpx.Response(200, json={"request_id": rid, "status": "completed",
                                             "completed": 1, "total": 1, "results": [result]})
        return httpx.Response(404, json={"success": False, "message": f"no route {method} {path}"})
