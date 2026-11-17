import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from ytauto import assemble
from ytauto.config import CaptionStyle, Palette


def have_ffmpeg():
    try:
        assemble.ffmpeg_bin()
        return True
    except assemble.AssembleError:
        return False


needs_ffmpeg = pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg tidak tersedia")

CAP = CaptionStyle(name="t", words=2, uppercase=True, highlight="#FFD400", pop=True)
PAL = Palette(name="p", accent="#FFD400", text="#111111", shade="#000000")


def timings_fixture():
    words = [("Some", 0.0, 0.3), ("rigs", 0.3, 0.6), ("drill", 0.6, 0.9), ("deep.", 0.9, 1.3),
             ("Wow", 1.5, 1.8), ("really", 1.8, 2.2)]
    return {"total": 2.4, "segments": [{
        "index": 0, "start": 0, "end": 2.4, "duration": 2.4,
        "words": [{"text": t, "start": s, "end": e} for t, s, e in words],
        "sentences": [{"text": "Some rigs drill deep.", "start": 0.0, "end": 1.3},
                      {"text": "Wow really.", "start": 1.5, "end": 2.2}],
    }]}


def test_ass_color_and_time():
    assert assemble.ass_color("#FFD400") == "&H0000D4FF"
    assert assemble.ass_color("#000000", 0x60) == "&H60000000"
    assert assemble.ass_time(61.234) == "0:01:01.23"


def test_caption_chunks_respect_sentences():
    chunks = assemble.caption_chunks(timings_fixture(), 3)
    assert [[w["text"] for w in c] for c in chunks] == [["Some", "rigs", "drill"], ["deep."], ["Wow", "really"]]


def test_build_ass_highlights_each_word(cfg):
    ass = assemble.build_ass(timings_fixture(), CAP, PAL, cfg, hook_text="3 km down")
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue: 1")]
    assert len(dialogues) == 6  # satu event per kata (karaoke highlight)
    assert "{\\c&H0000D4FF}SOME{\\r} RIGS" in dialogues[0]
    assert "\\fscx114" in dialogues[0] and "\\fscx114" not in dialogues[1]
    assert "Hook" in ass and "3 KM DOWN" in ass
    assert f"PlayResY: {cfg.format.height}" in ass


def test_long_word_is_scaled_down(cfg):
    t = timings_fixture()
    t["segments"][0]["words"][0]["text"] = "computer-controlled-thrusters"
    ass = assemble.build_ass(t, CAP, PAL, cfg, None)
    first = [l for l in ass.splitlines() if l.startswith("Dialogue: 1")][0]
    import re
    sc = int(re.search(r"\\t\(0,90,\\fscx(\d+)", first).group(1))
    assert sc < 100
    # kata pendek tidak diperkecil
    assert "\\fscx114" in [l for l in ass.splitlines() if "WOW" in l][0]


def test_build_ass_escapes_braces(cfg):
    t = timings_fixture()
    t["segments"][0]["words"][0]["text"] = "{bad}\\N"
    ass = assemble.build_ass(t, CAP, PAL, cfg, None)
    assert "{bad}" not in ass and "(BAD)N" in ass


@pytest.mark.parametrize("layout", ["top_band", "bottom_band", "center_outline", "corner_tag"])
def test_thumbnail_layouts(cfg, tmp_path, layout):
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (640, 360), (40, 90, 160)).save(frame)
    out = assemble.make_thumbnail(frame, "How rigs drill 3 km under the sea", layout, PAL, tmp_path / "t.jpg", cfg)
    img = Image.open(out)
    assert img.size == (cfg.format.width, cfg.format.height)
    assert out.stat().st_size <= assemble.MAX_THUMB_BYTES


def test_pick_music(cfg):
    import random
    assert assemble.pick_music(cfg, random.Random(0)) is None
    (cfg.path("assets/music") / "a.mp3").write_bytes(b"x")
    (cfg.path("assets/music") / "LICENSES.md").write_text("x")
    assert assemble.pick_music(cfg, random.Random(0)).name == "a.mp3"


# ---------------------------------------------------------------- integrasi ffmpeg
def ff(*args):
    subprocess.run([assemble.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@needs_ffmpeg
@pytest.mark.parametrize("with_music", [True, False])
def test_full_render(cfg, tmp_path, with_music):
    # resolusi kecil supaya tes cepat
    cfg.format.width, cfg.format.height, cfg.format.fps = 270, 480, 24
    cfg.format.min_seconds, cfg.format.max_seconds = 2, 20
    out = tmp_path / "video_0"
    out.mkdir()
    clips = tmp_path / "clips"
    clips.mkdir()
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=6", "-pix_fmt", "yuv420p", str(clips / "a.mp4"))
    ff("-f", "lavfi", "-i", "testsrc=size=360x640:rate=30:duration=1", "-pix_fmt", "yuv420p", str(clips / "b.mp4"))
    Image.new("RGB", (800, 1200), (200, 120, 30)).save(clips / "c.jpg")
    # voice: 4 detik nada sebagai pengganti TTS, MP3 seperti keluaran edge-tts
    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-ar", "24000", "-ac", "1", "-b:a", "48k", str(out / "voice.mp3"))
    if with_music:
        ff("-f", "lavfi", "-i", "sine=frequency=220:duration=30", str(cfg.path("assets/music") / "bed.wav"))

    timings = {"total": 4.0, "segments": [
        {"index": 0, "start": 0, "end": 2.5, "duration": 2.5,
         "words": [{"text": "Hello", "start": 0.1, "end": 0.6}, {"text": "world", "start": 0.7, "end": 1.2}],
         "sentences": [{"text": "Hello world.", "start": 0.1, "end": 1.2}]},
        {"index": 1, "start": 2.5, "end": 4.0, "duration": 1.5,
         "words": [{"text": "Bye", "start": 2.6, "end": 3.0}], "sentences": [{"text": "Bye.", "start": 2.6, "end": 3.0}]},
    ]}
    visuals = {"segments": [
        {"segment": 0, "duration": 2.5, "shots": [
            {"path": str(clips / "a.mp4"), "kind": "video", "duration": 6, "shot_duration": 1.25},
            {"path": str(clips / "b.mp4"), "kind": "video", "duration": 1, "shot_duration": 1.25}]},  # di-loop
        {"segment": 1, "duration": 1.5, "shots": [
            {"path": str(clips / "c.jpg"), "kind": "photo", "duration": 0, "shot_duration": 1.5}]},
    ]}
    script = {"title": "Test", "thumbnail_text": "Big Test"}
    style = {"seed": 3, "caption_style": "punch_yellow", "palette": "electric", "thumbnail_layout": "corner_tag",
             "hook_overlay": True}
    res = assemble.run(cfg, script, timings, visuals, style, out)

    info = assemble.probe(Path(res["video"]))
    assert info["has_audio"] and info["has_video"]
    assert (info["width"], info["height"]) == (270, 480)
    assert 4.0 <= info["duration"] <= 4.8
    assert Path(res["thumbnail"]).exists()
    assert (res["music"] == "bed.wav") == with_music
    assert bool(res["warnings"]) != with_music
    assert json.loads((out / "assemble.json").read_text())["video"] == res["video"]


@needs_ffmpeg
def test_validate_rejects_missing(cfg, tmp_path):
    with pytest.raises(assemble.AssembleError):
        assemble.validate_output(cfg, tmp_path / "nope.mp4")
