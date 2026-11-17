"""Langkah 8: kirim ringkasan & error ke Telegram. Modul ini tidak pernah melempar exception."""
from __future__ import annotations

import html
import logging
import os
from pathlib import Path

from . import http
from .config import Config, env

log = logging.getLogger("ytauto.notify")
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000
MAX_VIDEO_BYTES = 49 * 1024 * 1024


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def run_url() -> str | None:
    server, repo, rid = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{rid}" if server and repo and rid else None


def _truncate(text: str, limit: int = MAX_LEN) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n…(dipotong)"


def format_summary(results: list[dict], run_id: str, dry_run: bool, plan_error: dict | None = None,
                   link: str | None = None, expected: int | None = None) -> str:
    ok = [r for r in results if r.get("status") in ("uploaded", "dry_run")]
    mode = "🧪 DRY-RUN (tanpa upload)" if dry_run else "🚀 Run harian"
    lines = [f"<b>{mode}</b> · <code>{esc(run_id)}</code>",
             f"Berhasil: <b>{len(ok)}/{expected or len(results) or 0}</b>"]
    if plan_error:
        lines.append(f"\n❌ <b>Gagal di tahap perencanaan</b> ({esc(plan_error.get('failed_step'))})\n"
                     f"<pre>{esc(plan_error.get('error', ''))[:600]}</pre>")
    for r in sorted(results, key=lambda x: x.get("index", 0)):
        n = r.get("index", 0) + 1
        title = esc(r.get("title") or r.get("topic") or "?")
        if r.get("status") == "uploaded":
            when = r.get("publish_at") or "sekarang"
            lines.append(f"\n✅ <b>#{n}</b> {title}\n{esc(r.get('url'))}\n{esc(r.get('privacy'))} · tayang {esc(when)}")
        elif r.get("status") == "dry_run":
            lines.append(f"\n🧪 <b>#{n}</b> {title} · {esc(r.get('duration'))} detik")
        else:
            lines.append(f"\n❌ <b>#{n}</b> {title}\nLangkah: {esc(r.get('failed_step'))} · {esc(str(r.get('error', ''))[:300])}")
        for w in r.get("warnings", []):
            lines.append(f"⚠️ {esc(w)}")
    if expected and len(results) < expected and not plan_error:
        lines.append(f"\n⚠️ {expected - len(results)} video tidak melaporkan hasil (job mungkin timeout/dibatalkan)")
    if link:
        lines.append(f"\n<a href=\"{esc(link)}\">Lihat log GitHub Actions</a>")
    return _truncate("\n".join(lines))


def format_failure(r: dict) -> str:
    return _truncate(
        f"❌ <b>Video #{r.get('index', 0) + 1} gagal</b> di langkah <b>{esc(r.get('failed_step'))}</b>\n"
        f"{esc(r.get('title') or r.get('topic'))}\n\n<b>Error:</b> {esc(str(r.get('error', ''))[:500])}\n\n"
        f"<b>Log terakhir:</b>\n<pre>{esc(r.get('log_tail', ''))[-2800:]}</pre>"
    )


class Telegram:
    def __init__(self, token: str, chat_id: str, session=None):
        self.token, self.chat_id, self.session = token, chat_id, session

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***")

    def send_message(self, text: str) -> bool:
        try:
            http.request("POST", API.format(token=self.token, method="sendMessage"), session=self.session, attempts=4,
                         json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True})
            return True
        except Exception as e:  # noqa: BLE001
            log.error("Telegram sendMessage gagal: %s", self._redact(str(e)))
            return False

    def send_video(self, path: Path, caption: str) -> bool:
        if not path.exists() or path.stat().st_size > MAX_VIDEO_BYTES:
            log.warning("video %s tidak dikirim (tidak ada / > 49 MB)", path)
            return False
        try:
            http.request("POST", API.format(token=self.token, method="sendVideo"), session=self.session, attempts=3,
                         timeout=300, data={"chat_id": self.chat_id, "caption": caption[:1000], "parse_mode": "HTML",
                                            "supports_streaming": "true"},
                         files={"video": (path.name, path.read_bytes(), "video/mp4")})
            return True
        except Exception as e:  # noqa: BLE001
            log.error("Telegram sendVideo gagal: %s", self._redact(str(e)))
            return False


def get_telegram(config: Config) -> Telegram | None:
    if not config.notify.telegram:
        return None
    try:
        return Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"))
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram tidak aktif: %s", e)
        return None


def notify_run(config: Config, results: list[dict], run_id: str, dry_run: bool, plan_error: dict | None = None,
               expected: int | None = None, telegram: Telegram | None = None) -> None:
    try:
        tg = telegram or get_telegram(config)
        if not tg:
            return
        tg.send_message(format_summary(results, run_id, dry_run, plan_error, run_url(), expected))
        for r in results:
            if r.get("status") == "failed":
                tg.send_message(format_failure(r))
        if dry_run and config.notify.send_video_on_dry_run:
            for r in results:
                if r.get("status") == "dry_run" and r.get("video_path"):
                    tg.send_video(Path(r["video_path"]), f"🧪 <b>{esc(r.get('title'))}</b>")
    except Exception as e:  # noqa: BLE001 - notifikasi tidak boleh menggagalkan pipeline
        log.error("notifikasi gagal: %s", e)
