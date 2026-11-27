#!/usr/bin/env python
"""Unggah satu video ke YouTube dari komputer ini (alur manual).

Kredensial dibaca dari .env (dibuat oleh `python get_refresh_token.py --client-secret client_secret.json --save`)
dan tidak pernah ikut ter-commit.

Contoh:
  python upload_video.py video.mp4 --title "Judul" --description "Deskripsi" --tags "fakta,unik"
  python upload_video.py video.mp4 --title "Judul" --privacy public
  python upload_video.py video.mp4 --title "Judul" --publish-at "2026-11-28 18:00" --tz America/New_York
  python upload_video.py video.mp4 --title "Judul" --check      # hanya cek metadata, tanpa upload
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ytauto.uploader import UploadError, build_tags, sanitize_title, set_thumbnail, upload_video  # noqa: E402
from ytauto.scheduling import rfc3339  # noqa: E402

LOG_FILE = ROOT / "uploads.json"


def load_env(path: Path) -> None:
    """Muat KEY=VALUE dari .env ke environment (tanpa menimpa yang sudah ada)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def load_client_secret(path: Path) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    info = data.get("installed") or data.get("web") or {}
    if info.get("client_id"):
        os.environ.setdefault("YT_CLIENT_ID", info["client_id"])
    if info.get("client_secret"):
        os.environ.setdefault("YT_CLIENT_SECRET", info["client_secret"])


def parse_publish_at(text: str | None, tz: str) -> datetime | None:
    if not text:
        return None
    dt = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(tz))
    if dt <= datetime.now(timezone.utc):
        raise SystemExit(f"--publish-at {text} ({tz}) sudah lewat")
    return dt


def build_body(args, publish_at: datetime | None) -> dict:
    title = sanitize_title(args.title)
    description = (Path(args.description_file).read_text(encoding="utf-8") if args.description_file
                   else (args.description or "")).replace("<", "").replace(">", "")
    tags = build_tags([t for t in (args.tags or "").split(",") if t.strip()])
    status: dict = {
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": args.synthetic,
        "embeddable": True,
        "license": "youtube",
    }
    if publish_at:
        status["privacyStatus"] = "private"
        status["publishAt"] = rfc3339(publish_at)
    else:
        status["privacyStatus"] = args.privacy
    snippet = {"title": title, "description": description[:4900], "tags": tags, "categoryId": args.category}
    if args.language:
        snippet["defaultLanguage"] = args.language
        snippet["defaultAudioLanguage"] = args.language
    return {"snippet": snippet, "status": status}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video")
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--description-file")
    p.add_argument("--tags", default="", help="dipisah koma")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    p.add_argument("--publish-at", help='jadwal tayang "YYYY-MM-DD HH:MM" (otomatis private sampai waktunya)')
    p.add_argument("--tz", default="Asia/Jakarta", help="zona waktu untuk --publish-at (default WIB)")
    p.add_argument("--thumbnail")
    p.add_argument("--category", default="27", help="27 Education, 28 Science & Technology, 24 Entertainment, 22 People & Blogs")
    p.add_argument("--language", default="en")
    p.add_argument("--synthetic", action="store_true",
                   help="tandai berisi konten realistis buatan/ubahan AI (wajib bila videonya bisa dikira nyata)")
    p.add_argument("--no-notify", action="store_true", help="jangan beri notifikasi ke subscriber")
    p.add_argument("--check", action="store_true", help="tampilkan metadata saja, tanpa upload")
    args = p.parse_args(argv)

    video = Path(args.video)
    if not video.is_file():
        raise SystemExit(f"file video tidak ditemukan: {video}")
    if args.thumbnail and not Path(args.thumbnail).is_file():
        raise SystemExit(f"file thumbnail tidak ditemukan: {args.thumbnail}")
    body = build_body(args, parse_publish_at(args.publish_at, args.tz))
    print(json.dumps(body, indent=2, ensure_ascii=False))
    print(f"file: {video} ({video.stat().st_size / 1e6:.1f} MB)")
    if args.check:
        return 0

    load_env(ROOT / ".env")
    load_client_secret(ROOT / "client_secret.json")
    missing = [k for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"kredensial belum lengkap: {', '.join(missing)}. Jalankan dulu: "
                         "python get_refresh_token.py --client-secret client_secret.json --save")

    from ytauto.uploader import build_service

    youtube = build_service()
    try:
        resp = upload_video(youtube, video, body, notify_subscribers=not args.no_notify)
    except UploadError as e:
        raise SystemExit(f"upload gagal: {e}") from e
    vid = resp["id"]
    url = f"https://youtube.com/shorts/{vid}"
    warnings = []
    if args.thumbnail:
        try:
            set_thumbnail(youtube, vid, Path(args.thumbnail))
        except Exception as e:  # noqa: BLE001 - video sudah terunggah
            warnings.append(f"thumbnail gagal dipasang: {str(e)[:200]}")
    entry = {"video_id": vid, "url": url, "title": body["snippet"]["title"], "file": video.name,
             "privacy": body["status"]["privacyStatus"], "publish_at": body["status"].get("publishAt"),
             "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    log = json.loads(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else []
    log.append(entry)
    LOG_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nBERHASIL: {url}  ({entry['privacy']}{', tayang ' + entry['publish_at'] if entry['publish_at'] else ''})")
    for w in warnings:
        print(f"PERINGATAN: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
