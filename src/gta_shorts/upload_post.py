"""Client for Upload-Post (https://upload-post.com), which posts our clips to TikTok.

TikTok only lets audited apps post publicly, and its guidelines rule out
auditing "a utility tool to help upload contents to the account(s) you or
your team manages". Upload-Post's app is audited: we send it the file and it
publishes on the TikTok account connected to one of its profiles.

Upload: ``POST /api/upload`` (multipart) with our own ``request_id`` and an
``Idempotency-Key``, so a retry after a crash or timeout replays the request
instead of posting twice. Then ``GET /api/uploadposts/status`` until TikTok
has published it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

API_URL = "https://api.upload-post.com"

# HTTP statuses about the account (key, plan, limits), not about the clip:
# the poster pauses instead of counting a failed attempt against the clip.
ACCOUNT_STATUSES = (401, 402, 403, 429)
ACCOUNT_CODES = {"profile_not_found", "PROFILE_NOT_FOUND", "tiktok_reconnect_required",
                 "reauth_required", "reached_active_user_cap", "SCHEDULED_POSTS_LIMIT",
                 "tiktok_privacy_unavailable", "spam_risk_too_many_posts",
                 "spam_risk_too_many_pending_share", "spam_risk_user_banned_from_posting"}
FINAL_CODES = {"tiktok_media_rejected"}       # this file will never post: don't retry
PENDING_STEPS = ("queued", "pending", "processing", "in_progress", "retryable",
                 "processing_upload", "processing_download")


class UploadPostError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: Optional[str] = None):
        super().__init__(message)
        self.status, self.code = status, code

    @property
    def account(self) -> bool:
        """The account needs attention (or time), the clip is fine."""
        return self.status in ACCOUNT_STATUSES or self.code in ACCOUNT_CODES

    @property
    def transient(self) -> bool:
        """Worth sending the same request again (server error, duplicate in flight)."""
        return (self.status or 0) >= 500 or self.code == "IDEMPOTENCY_KEY_IN_USE"


@dataclass
class Outcome:
    """Where one TikTok post stands."""
    state: str                    # pending | posted | inbox | failed
    url: Optional[str] = None
    error: Optional[str] = None
    code: Optional[str] = None

    @property
    def final(self) -> bool:      # a failure no retry will fix
        return self.code in FINAL_CODES

    @property
    def account(self) -> bool:
        return self.code in ACCOUNT_CODES


def _message(body: dict) -> str:
    text = body.get("message") or body.get("error") or ""
    for v in body.get("violations") or []:
        text += f" | {v.get('message') or v}"
    return str(text or body)[:300]


def tiktok_outcome(body: dict) -> Outcome:
    """Read TikTok's result from an upload or status response (both shapes)."""
    results = body.get("results")
    item = None
    if isinstance(results, dict):                       # sync upload answer
        item = results.get("tiktok")
    elif isinstance(results, list):                     # status answer
        item = next((r for r in results if r.get("platform") == "tiktok"), None)
    overall = str(body.get("status") or "").lower()     # the request as a whole
    if not item:
        if overall == "failed":
            return Outcome("failed", error=_message(body))
        return Outcome("pending")
    if item.get("skipped"):
        return Outcome("failed", error=f"skipped: {item.get('skip_reason') or 'no reason given'}",
                       code="profile_not_found")
    step = str(item.get("status") or "").lower()
    # "retryable" is Upload-Post retrying by itself: posting again could post twice.
    if step in PENDING_STEPS and overall != "failed":
        return Outcome("pending")
    if item.get("success") is False or step in ("failed", "retryable"):
        return Outcome("failed", error=_message(item), code=item.get("error_code"))
    if item.get("success") or step in ("completed", "publish_success", "publish_complete"):
        url = item.get("post_url")
        if item.get("fallback_to_inbox") or step == "send_to_user_inbox":
            return Outcome("inbox", error="sent to your TikTok inbox: publish it from the app")
        if url and not str(url).startswith("http"):     # "Video sent to Inbox (No Public URL)"
            return Outcome("inbox", error=str(url))
        if not url and overall not in ("completed", ""):
            return Outcome("pending")
        return Outcome("posted", url=url)
    if overall == "failed":
        return Outcome("failed", error=_message(body))
    return Outcome("pending")


class UploadPost:
    def __init__(self, api_key: str, *, base_url: str = API_URL,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = 60.0):
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                  transport=transport,
                                  headers={"Authorization": f"Apikey {api_key}"})

    @staticmethod
    def _check(r: httpx.Response, what: str) -> dict:
        try:
            body = r.json()
        except ValueError:
            body = {"message": r.text[:300]}
        if not isinstance(body, dict):
            body = {"message": str(body)[:300]}
        if r.status_code >= 400 or body.get("success") is False:
            raise UploadPostError(f"{what} -> {r.status_code}: {_message(body)}",
                                  status=r.status_code, code=body.get("error_code"))
        return body

    def me(self) -> dict:
        """The account behind the key (``plan`` among others). Raises on a bad key."""
        return self._check(self._http.get("/api/uploadposts/me"), "check API key")

    def tiktok_account(self, user: str) -> Optional[dict]:
        """The TikTok account connected to profile ``user`` (None if none is)."""
        body = self._check(self._http.get(f"/api/uploadposts/users/{user}"), f"profile {user!r}")
        accounts = (body.get("profile") or {}).get("social_accounts") or {}
        return accounts.get("tiktok") or None

    def upload_video(self, path: Path, *, user: str, caption: str, request_id: str,
                     privacy: Optional[str] = None, external_id: Optional[str] = None) -> dict:
        data = {"user": user, "platform[]": "tiktok", "title": caption,
                "request_id": request_id, "async_upload": "true",
                # Over TikTok's app-wide user cap, fail (we retry later) instead
                # of dropping the video into the TikTok inbox as a draft.
                "disable_inbox_fallback": "true"}
        if privacy:
            data["privacy_level"] = privacy
        if external_id:
            data["external_id"] = external_id[:255]
        with open(path, "rb") as f:
            r = self._http.post("/api/upload", data=data,
                                files={"video": (path.name, f, "video/mp4")},
                                headers={"Idempotency-Key": request_id}, timeout=600)
        return self._check(r, "upload")

    def status(self, request_id: str) -> Optional[dict]:
        """The request's progress, or None when Upload-Post never received it."""
        r = self._http.get("/api/uploadposts/status", params={"request_id": request_id})
        if r.status_code == 404:
            return None
        body = self._check(r, "upload status")
        if str(body.get("status") or "").lower() == "not_found":
            return None
        return body
