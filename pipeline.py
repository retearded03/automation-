#!/usr/bin/env python
"""Orkestrator yt-autopilot.

Subperintah (dipakai GitHub Actions sebagai 3 job):
  plan      langkah 1-2: tren -> pilih N topik + gaya + jadwal tayang (work/<run>/plan/plan.json)
  produce   langkah 3-7 untuk satu video (--index i): naskah -> suara -> visual -> rakit -> upload
  finalize  kumpulkan hasil, perbarui history.json, kirim notifikasi Telegram (langkah 8)
  all       jalankan semuanya berurutan di satu mesin (untuk lokal)

--dry-run menjalankan semua langkah kecuali upload, dan tidak mengubah history.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from ytauto import assemble, notify, script_writer, trend_finder, uploader, visuals, voice
from ytauto.config import Config, env, load_config
from ytauto.context import RunContext, StepError, read_tail
from ytauto.history import History
from ytauto.llm import get_llm, llm_label
from ytauto.topic_picker import build_plan


def default_run_id() -> str:
    if os.environ.get("GITHUB_RUN_ID"):
        return f"{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return "local-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def run_root(cfg: Config, run_id: str) -> Path:
    return cfg.path(cfg.paths.work_dir) / run_id


# ---------------------------------------------------------------- plan
def cmd_plan(cfg: Config, run_id: str, dry_run: bool, count: int | None = None, llm=None) -> int:
    ctx = RunContext(run_id, cfg, dry_run, scope="plan")
    history = History.load(cfg.path(cfg.paths.history_file))
    try:
        with ctx.step("trend_finder"):
            trends = trend_finder.find_trends(cfg, env("YT_API_KEY"))
            ctx.write_json("trends.json", trends)
        with ctx.step("topic_picker"):
            plan = build_plan(cfg, llm or get_llm(cfg), trends, history, run_id, count)
            ctx.write_json("plan.json", plan)
    except StepError as e:
        ctx.write_json("plan_error.json", {"status": "failed", "failed_step": e.step, "error": str(e.cause),
                                           "log_tail": read_tail(ctx.log_file, 40)})
        github_output("indices", "[]")
        return 1
    github_output("indices", json.dumps(list(range(plan["count"]))))
    for v in plan["videos"]:
        ctx.logger.info("#%d %s | tayang %s | %s", v["index"] + 1, v["topic"]["title"], v["publish_at"], v["style"]["voice"])
    return 0


# ---------------------------------------------------------------- produce
def cmd_produce(cfg: Config, run_id: str, index: int, dry_run: bool, llm=None, youtube=None) -> int:
    ctx = RunContext(run_id, cfg, dry_run, scope=f"video_{index}")
    out = ctx.dir()
    report = ctx.dir("report")
    result: dict = {"index": index, "status": "failed", "warnings": [], "run_id": run_id}
    try:
        with ctx.step("load_plan"):
            plan = json.loads((ctx.run_root / "plan" / "plan.json").read_text(encoding="utf-8"))
            item = plan["videos"][index]
            style = item["style"]
            result.update(topic=item["topic"]["title"], style=style, planned_publish_at=item["publish_at"])
            history = History.load(cfg.path(cfg.paths.history_file))
            llm = llm or get_llm(cfg)

        with ctx.step("script_writer"):
            script, last_err = None, None
            for topic in [item["topic"]] + ([item["backup"]] if item.get("backup") else []):
                try:
                    script = script_writer.produce_script(cfg, llm, topic, style, history.recent_titles(30))
                    break
                except script_writer.ScriptRejected as e:
                    last_err = e
                    ctx.logger.warning("topik %r ditolak: %s", topic["title"], e)
            if script is None:
                raise last_err or script_writer.ScriptRejected("tidak ada topik")
            ctx.write_json("script.json", script)
            result.update(topic=script["topic"]["title"], topic_angle=script["topic"].get("angle", ""),
                          title=script["title"], quality=script["quality"].get("overall"), llm=llm_label(llm))
            if script.get("quality_warning"):
                result["warnings"].append(script["quality_warning"])

        with ctx.step("voice"):
            timings = voice.run(cfg, script, style["voice"], out)

        with ctx.step("visuals"):
            vis = visuals.run(cfg, script, timings, out, history.used_clip_ids(cfg.visuals.avoid_reuse_days),
                              style["seed"])
            result.update(clip_ids=vis["clip_ids"], ai_images=vis.get("ai_images", 0),
                          shots=sum(len(s["shots"]) for s in vis["segments"]))

        with ctx.step("assemble"):
            asm = assemble.run(cfg, script, timings, vis, style, out)
            result.update(duration=asm["duration"], music=asm["music"])
            result["warnings"] += asm["warnings"]

        if dry_run:
            ctx.logger.info("dry-run: upload dilewati")
            result["status"] = "dry_run"
            for name in ("final.mp4", "thumbnail.jpg", "script.json"):
                if (out / name).exists():
                    shutil.copy2(out / name, report / name)
        else:
            with ctx.step("uploader"):
                up = uploader.run(cfg, script, asm, vis["attribution"], item["publish_at"], youtube=youtube)
                result["warnings"] += up.pop("warnings", [])
                result.update(up)
                result["status"] = "uploaded"
    except StepError as e:
        result.update(failed_step=e.step, error=f"{type(e.cause).__name__}: {e.cause}")
    finally:
        result["log_tail"] = read_tail(ctx.log_file, 40)
        (report / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if result["status"] != "failed" else 1


# ---------------------------------------------------------------- finalize
def collect(root: Path) -> tuple[list[dict], dict | None, int | None]:
    by_index: dict[int, dict] = {}
    for p in sorted(root.rglob("result.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        video = p.parent / "final.mp4"
        if video.exists():
            r["video_path"] = str(video)
        by_index[r.get("index", len(by_index))] = r
    plan_error = next((json.loads(p.read_text(encoding="utf-8")) for p in root.rglob("plan_error.json")), None)
    plan = next((json.loads(p.read_text(encoding="utf-8")) for p in root.rglob("plan.json")), None)
    return [by_index[k] for k in sorted(by_index)], plan_error, (plan or {}).get("count")


def cmd_finalize(cfg: Config, run_id: str, dry_run: bool, telegram=None) -> int:
    ctx = RunContext(run_id, cfg, dry_run, scope="finalize")
    results, plan_error, expected = collect(ctx.run_root)
    if not results and not plan_error:
        plan_error = {"failed_step": "plan", "error": "tidak ada hasil sama sekali (job plan gagal sebelum menulis laporan?)"}
    uploaded = [r for r in results if r.get("status") == "uploaded"]
    if uploaded and not dry_run:
        history = History.load(cfg.path(cfg.paths.history_file))
        for r in uploaded:
            style = {k: v for k, v in (r.get("style") or {}).items() if k != "seed"}
            history.add({"run_id": run_id, "video_id": r.get("video_id"), "url": r.get("url"), "title": r.get("title"),
                         "topic": f"{r.get('topic', '')} {r.get('topic_angle', '')}".strip(), "style": style,
                         "clip_ids": r.get("clip_ids", []), "publish_at": r.get("publish_at")})
        history.save()
        ctx.logger.info("history.json: +%d video", len(uploaded))
    ctx.write_json("summary.json", {"results": [{k: v for k, v in r.items() if k != "log_tail"} for r in results],
                                    "plan_error": plan_error, "expected": expected})
    notify.notify_run(cfg, results, run_id, dry_run, plan_error, expected, telegram=telegram)
    ok = sum(r.get("status") in ("uploaded", "dry_run") for r in results)
    ctx.logger.info("selesai: %d/%s video berhasil", ok, expected or len(results))
    return 0


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["plan", "produce", "finalize", "all"])
    p.add_argument("--config", default=None, help="path config.yaml (default: config.yaml di folder proyek)")
    p.add_argument("--run-id", default=None)
    p.add_argument("--index", type=int, default=0, help="nomor video untuk 'produce' (mulai 0)")
    p.add_argument("--count", type=int, default=None, help="jumlah video (default: schedule.videos_per_day)")
    p.add_argument("--dry-run", action="store_true", help="jalankan semua langkah kecuali upload")
    a = p.parse_args(argv)

    cfg = load_config(a.config)
    run_id = a.run_id or default_run_id()
    if a.command == "plan":
        return cmd_plan(cfg, run_id, a.dry_run, a.count)
    if a.command == "produce":
        return cmd_produce(cfg, run_id, a.index, a.dry_run)
    if a.command == "finalize":
        return cmd_finalize(cfg, run_id, a.dry_run)
    # all
    if cmd_plan(cfg, run_id, a.dry_run, a.count) == 0:
        plan = json.loads((run_root(cfg, run_id) / "plan" / "plan.json").read_text(encoding="utf-8"))
        for i in range(plan["count"]):
            cmd_produce(cfg, run_id, i, a.dry_run)
    return cmd_finalize(cfg, run_id, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
