import json

import requests
import responses

from ytauto import notify

TOKEN = "123:SECRET"
BASE = f"https://api.telegram.org/bot{TOKEN}"

RESULTS = [
    {"index": 0, "status": "uploaded", "title": "Bridges <sway> & bend", "url": "https://youtube.com/shorts/a",
     "privacy": "private", "publish_at": "2026-11-17T13:00:00.000Z", "warnings": ["thumbnail gagal"]},
    {"index": 1, "status": "failed", "topic": "Dams", "failed_step": "voice", "error": "edge-tts down",
     "log_tail": "line1\n<tag>"},
]


def test_summary_escapes_and_counts():
    text = notify.format_summary(RESULTS, "run9", False, link="https://github.com/x/y/actions/runs/1", expected=3)
    assert "1/3" in text
    assert "Bridges &lt;sway&gt; &amp; bend" in text
    assert "https://youtube.com/shorts/a" in text
    assert "voice" in text and "edge-tts down" in text
    assert "1 video tidak melaporkan hasil" in text
    assert "⚠️ thumbnail gagal" in text


def test_summary_plan_error_and_truncation():
    text = notify.format_summary([], "r", True, plan_error={"failed_step": "trend_finder", "error": "x" * 9000})
    assert "perencanaan" in text and "trend_finder" in text and len(text) <= notify.MAX_LEN


def test_failure_message_has_log():
    msg = notify.format_failure(RESULTS[1])
    assert "&lt;tag&gt;" in msg and "voice" in msg


@responses.activate
def test_notify_run_sends_summary_failures_and_dry_run_video(cfg, tmp_path):
    vid = tmp_path / "final.mp4"
    vid.write_bytes(b"video")
    responses.post(f"{BASE}/sendMessage", json={"ok": True})
    responses.post(f"{BASE}/sendVideo", json={"ok": True})
    results = RESULTS + [{"index": 2, "status": "dry_run", "title": "T", "video_path": str(vid), "duration": 40}]
    notify.notify_run(cfg, results, "r1", dry_run=True, expected=3, telegram=notify.Telegram(TOKEN, "42"))
    urls = [c.request.url for c in responses.calls]
    assert urls.count(f"{BASE}/sendMessage") == 2  # ringkasan + 1 detail gagal
    assert urls.count(f"{BASE}/sendVideo") == 1
    body = json.loads(responses.calls[0].request.body)
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"


@responses.activate
def test_notify_never_raises_and_redacts_token(cfg, caplog):
    responses.post(f"{BASE}/sendMessage", body=requests.ConnectionError("down"))
    notify.notify_run(cfg, RESULTS, "r1", dry_run=False, telegram=notify.Telegram(TOKEN, "42"))
    assert "SECRET" not in caplog.text


def test_missing_secrets_disables_telegram(cfg, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notify.get_telegram(cfg) is None
    notify.notify_run(cfg, RESULTS, "r", dry_run=False)  # tidak error
