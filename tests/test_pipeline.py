"""Tes alur pipeline end-to-end dengan semua langkah eksternal dipalsukan."""
import json

import pytest

import pipeline
from ytauto import script_writer

TRENDS = [{"id": "t1", "title": "Mega bridge build", "tags": [], "category_id": "28", "views_per_hour": 100}]


TITLES = ["How Dams Hold Back Lakes", "Why Planes Fly Upside Down", "How Elevators Stop Falling",
          "How Tunnel Boring Machines Steer", "Why Ships Float on Water", "How Wind Turbines Brake"]


def topic(i):
    return {"title": TITLES[i], "angle": f"Explains {TITLES[i].lower()}", "wikipedia_queries": [TITLES[i]],
            "inspired_by": ["t1"], "safety": "safe"}


class Calls:
    def __init__(self):
        self.uploads = []
        self.messages = []


@pytest.fixture
def env(monkeypatch, cfg, fake_llm, tmp_path):
    calls = Calls()
    monkeypatch.setenv("YT_API_KEY", "k")
    monkeypatch.setattr(pipeline.trend_finder, "find_trends", lambda c, k: TRENDS)

    def fake_script(c, llm, t, style, recent):
        if "REJECT" in t["title"]:
            raise script_writer.QualityError("lemah")
        return {"title": f"Video: {t['title']}", "thumbnail_text": "X", "segments": [{"text": "a", "visual_keywords": ["k"]}],
                "topic": t, "quality": {"overall": 8}, "description": "d", "tags": [], "hashtags": [], "sources": []}

    def fake_voice(c, script, v, out):
        if "FAILVOICE" in script["title"]:
            raise RuntimeError("edge-tts mati")
        return {"total": 40.0, "segments": [{"index": 0, "duration": 40.0}]}

    def fake_visuals(c, script, timings, out, used, seed):
        return {"segments": [], "attribution": [], "clip_ids": [f"pexels:{seed}"]}

    def fake_assemble(c, script, timings, vis, style, out):
        (out / "final.mp4").write_bytes(b"mp4")
        (out / "thumbnail.jpg").write_bytes(b"jpg")
        return {"video": str(out / "final.mp4"), "thumbnail": str(out / "thumbnail.jpg"), "duration": 40.2,
                "music": None, "warnings": ["folder musik kosong"]}

    def fake_upload(c, script, asm, attr, publish_at, youtube=None):
        calls.uploads.append(script["title"])
        return {"video_id": f"id{len(calls.uploads)}", "url": "https://youtube.com/shorts/x", "privacy": "private",
                "publish_at": publish_at, "title": script["title"], "warnings": []}

    monkeypatch.setattr(pipeline.script_writer, "produce_script", fake_script)
    monkeypatch.setattr(pipeline.voice, "run", fake_voice)
    monkeypatch.setattr(pipeline.visuals, "run", fake_visuals)
    monkeypatch.setattr(pipeline.assemble, "run", fake_assemble)
    monkeypatch.setattr(pipeline.uploader, "run", fake_upload)
    monkeypatch.setattr(pipeline.notify, "notify_run",
                        lambda c, results, run_id, dry, plan_error=None, expected=None, telegram=None:
                        calls.messages.append({"results": results, "dry": dry, "plan_error": plan_error, "expected": expected}))
    return calls


def run_all(cfg, llm, run_id, dry_run, count=3):
    assert pipeline.cmd_plan(cfg, run_id, dry_run, count, llm=llm) == 0
    codes = [pipeline.cmd_produce(cfg, run_id, i, dry_run, llm=llm) for i in range(count)]
    pipeline.cmd_finalize(cfg, run_id, dry_run)
    return codes


def history(cfg):
    return json.loads(cfg.path("history.json").read_text())["videos"]


def test_dry_run_never_uploads_or_touches_history(cfg, env, fake_llm):
    llm = fake_llm({"topics": [topic(i) for i in range(6)]})
    codes = run_all(cfg, llm, "r1", dry_run=True)
    assert codes == [0, 0, 0]
    assert env.uploads == []
    assert history(cfg) == []
    msg = env.messages[0]
    assert msg["dry"] and msg["expected"] == 3
    assert [r["status"] for r in msg["results"]] == ["dry_run"] * 3
    assert all(r["video_path"].endswith("final.mp4") for r in msg["results"])  # dikirim ke Telegram untuk review


def test_real_run_uploads_and_updates_history(cfg, env, fake_llm):
    llm = fake_llm({"topics": [topic(i) for i in range(6)]})
    run_all(cfg, llm, "r2", dry_run=False)
    assert len(env.uploads) == 3
    h = history(cfg)
    assert len(h) == 3 and h[0]["video_id"] == "id1" and h[0]["clip_ids"]
    assert "seed" not in h[0]["style"]
    assert h[0]["publish_at"]  # jadwal tayang dari plan diteruskan ke uploader


def test_failed_step_stops_that_video_only(cfg, env, fake_llm):
    topics = [topic(i) for i in range(6)]
    topics[1]["title"] = "FAILVOICE machine topic special"
    llm = fake_llm({"topics": topics})
    codes = run_all(cfg, llm, "r3", dry_run=False)
    assert codes == [0, 1, 0]
    assert len(env.uploads) == 2  # video yang gagal tidak pernah diunggah
    failed = env.messages[0]["results"][1]
    assert failed["status"] == "failed" and failed["failed_step"] == "voice"
    assert "edge-tts mati" in failed["error"] and failed["log_tail"]
    assert len(history(cfg)) == 2


def test_rejected_topic_uses_backup(cfg, env, fake_llm):
    topics = [topic(i) for i in range(4)]
    topics[0]["title"] = "REJECT this weak topic please"
    llm = fake_llm({"topics": topics})
    run_all(cfg, llm, "r4", dry_run=False, count=2)
    assert env.uploads[0] == f"Video: {topics[2]['title']}"  # cadangan untuk slot 0


def test_plan_failure_reported(cfg, env, fake_llm, monkeypatch):
    def boom(c, k):
        raise RuntimeError("quotaExceeded")

    monkeypatch.setattr(pipeline.trend_finder, "find_trends", boom)
    assert pipeline.cmd_plan(cfg, "r5", False, 3, llm=fake_llm()) == 1
    pipeline.cmd_finalize(cfg, "r5", False)
    pe = env.messages[0]["plan_error"]
    assert pe["failed_step"] == "trend_finder" and "quotaExceeded" in pe["error"]


def test_finalize_with_nothing(cfg, env):
    pipeline.cmd_finalize(cfg, "empty", False)
    assert env.messages[0]["plan_error"]["failed_step"] == "plan"


def test_github_output(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    pipeline.github_output("indices", "[0, 1]")
    assert out.read_text() == "indices=[0, 1]\n"
