"""Langkah 7: unggah via videos.insert (resumable upload, OAuth refresh token) lalu set thumbnail.

Hanya dipanggil setelah final.mp4 dan thumbnail tervalidasi, jadi tidak pernah mengunggah video setengah jadi.
"""
from __future__ import annotations

import http.client
import logging
import socket
from datetime import datetime
from pathlib import Path

from .config import Config, env
from .retry import backoff_delay
from . import retry as retry_mod
from .scheduling import resolve_publish_at, rfc3339

log = logging.getLogger("ytauto.uploader")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
RETRIABLE_STATUS = {500, 502, 503, 504}
RETRIABLE_EXC = (http.client.HTTPException, ConnectionError, socket.timeout, TimeoutError, OSError)


class UploadError(Exception):
    pass


# ---------------------------------------------------------------- metadata
def _strip(text: str) -> str:
    return (text or "").replace("<", "").replace(">", "").strip()


def sanitize_title(title: str) -> str:
    t = " ".join(_strip(title).split())
    return t[:100] if t else "Untitled"


def build_tags(tags: list[str], max_total: int = 480) -> list[str]:
    out, total, seen = [], 0, set()
    for t in tags:
        t = _strip(t).replace(",", " ").lstrip("#")
        t = " ".join(t.split())[:60]
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > max_total:
            break
        out.append(t)
        seen.add(t.lower())
        total += cost
    return out


def build_description(script: dict, attribution: list[dict], config: Config) -> str:
    hashtags = []
    for h in ["#shorts"] + script.get("hashtags", []):
        h = "#" + "".join(ch for ch in h.lstrip("#") if ch.isalnum())
        if len(h) > 1 and h.lower() not in [x.lower() for x in hashtags]:
            hashtags.append(h)
    parts = [_strip(script.get("description", "")), " ".join(hashtags[:5])]
    if script.get("sources"):
        parts.append("Sources:\n" + "\n".join(f"- {_strip(s['title'])} (Wikipedia): {s['url']}" for s in script["sources"]))
    if attribution:
        parts.append("Footage:\n" + "\n".join(f"- {_strip(a['text'])}: {a['url']}" for a in attribution))
    parts.append("Narration uses a synthetic text-to-speech voice. Facts were checked against the sources above.")
    desc = "\n\n".join(p for p in parts if p)
    while len(desc.encode("utf-8")) > 4900:  # batas YouTube 5000 byte
        desc = desc[: int(len(desc) * 0.95)]
    return desc


def build_body(config: Config, script: dict, attribution: list[dict], publish_at: datetime | None) -> dict:
    up = config.upload
    status: dict = {
        "selfDeclaredMadeForKids": up.made_for_kids,
        "containsSyntheticMedia": up.contains_synthetic_media,
        "embeddable": True,
        "license": "youtube",
    }
    if up.privacy_status == "scheduled":
        if publish_at:
            status["privacyStatus"] = "private"
            status["publishAt"] = rfc3339(publish_at)
        else:
            status["privacyStatus"] = "public"  # slot sudah lewat -> tayang sekarang
    else:
        status["privacyStatus"] = up.privacy_status
    return {
        "snippet": {
            "title": sanitize_title(script["title"]),
            "description": build_description(script, attribution, config),
            "tags": build_tags(script.get("tags", [])),
            "categoryId": config.channel.category_id,
            "defaultLanguage": config.channel.language,
            "defaultAudioLanguage": config.channel.language,
        },
        "status": status,
    }


# ---------------------------------------------------------------- API
def build_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=env("YT_REFRESH_TOKEN"),
        client_id=env("YT_CLIENT_ID"),
        client_secret=env("YT_CLIENT_SECRET"),
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _status_of(err) -> int | None:
    resp = getattr(err, "resp", None)
    try:
        return int(getattr(resp, "status", None))
    except (TypeError, ValueError):
        return None


def upload_video(youtube, path: Path, body: dict, notify_subscribers: bool = True,
                 chunk_mb: int = 8, max_retries: int = 8) -> dict:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=chunk_mb * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media,
                                      notifySubscribers=notify_subscribers)
    response = None
    errors = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info("upload %d%%", int(status.progress() * 100))
        except HttpError as e:
            code = _status_of(e)
            if code not in RETRIABLE_STATUS:
                raise UploadError(f"upload ditolak (HTTP {code}): {getattr(e, 'reason', '') or e}") from e
            errors += 1
            if errors > max_retries:
                raise UploadError(f"upload gagal setelah {max_retries} percobaan (HTTP {code})") from e
            retry_mod.SLEEP(backoff_delay(errors, 2.0, 64.0))
        except RETRIABLE_EXC as e:
            errors += 1
            if errors > max_retries:
                raise UploadError(f"upload gagal setelah {max_retries} percobaan: {e}") from e
            retry_mod.SLEEP(backoff_delay(errors, 2.0, 64.0))
    if not response or "id" not in response:
        raise UploadError(f"respons upload tidak berisi id video: {response}")
    return response


def set_thumbnail(youtube, video_id: str, thumb: Path, attempts: int = 4) -> None:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    for attempt in range(1, attempts + 1):
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
            return
        except HttpError as e:
            if _status_of(e) not in RETRIABLE_STATUS or attempt == attempts:
                raise
            retry_mod.SLEEP(backoff_delay(attempt, 2.0, 30.0))


def run(config: Config, script: dict, assembled: dict, attribution: list[dict], planned_publish_at: str | None,
        youtube=None, now: datetime | None = None) -> dict:
    video = Path(assembled["video"])
    thumb = Path(assembled["thumbnail"])
    if not video.exists():
        raise UploadError("final.mp4 tidak ada; upload dibatalkan")
    publish_at = resolve_publish_at(planned_publish_at, now, config.schedule.min_lead_minutes)
    body = build_body(config, script, attribution, publish_at)
    youtube = youtube or build_service()
    resp = upload_video(youtube, video, body, config.upload.notify_subscribers)
    vid = resp["id"]
    warnings: list[str] = []
    if planned_publish_at and config.upload.privacy_status == "scheduled" and not publish_at:
        warnings.append("slot tayang sudah lewat, video langsung dipublikasikan")
    if config.upload.set_thumbnail and thumb.exists():
        try:
            set_thumbnail(youtube, vid, thumb)
        except Exception as e:  # noqa: BLE001 - video sudah utuh; thumbnail gagal hanya peringatan
            warnings.append(f"thumbnail gagal dipasang ({type(e).__name__}: {str(e)[:200]}). "
                            "Pastikan channel sudah verifikasi nomor HP.")
    result = {
        "video_id": vid,
        "url": f"https://youtube.com/shorts/{vid}",
        "privacy": body["status"]["privacyStatus"],
        "publish_at": body["status"].get("publishAt"),
        "title": body["snippet"]["title"],
        "warnings": warnings,
    }
    log.info("terunggah: %s (%s, tayang %s)", result["url"], result["privacy"], result["publish_at"] or "sekarang")
    return result
