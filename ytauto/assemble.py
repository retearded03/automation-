"""Langkah 6: rakit video dengan ffmpeg (footage + voice-over + subtitle terbakar + musik) dan thumbnail."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import CaptionStyle, Config, Palette

log = logging.getLogger("ytauto.assemble")
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}
FONT_CANDIDATES = [
    "**/Roboto-Black.ttf", "**/Roboto-Bold.ttf", "**/Montserrat-ExtraBold.ttf", "**/DejaVuSans-Bold.ttf",
    "**/LiberationSans-Bold.ttf", "**/arialbd.ttf", "**/impact.ttf",
]
SYSTEM_FONT_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts", "C:/Windows/Fonts", "/Library/Fonts"]
TAIL_SECONDS = 0.35
MAX_THUMB_BYTES = 2 * 1024 * 1024


class AssembleError(Exception):
    pass


# ---------------------------------------------------------------- ffmpeg
def ffmpeg_bin() -> str:
    if os.environ.get("FFMPEG_BIN"):
        return os.environ["FFMPEG_BIN"]
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # dependensi dev, untuk komputer lokal

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        raise AssembleError("ffmpeg tidak ditemukan; pasang ffmpeg atau set FFMPEG_BIN") from e


def run_ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssembleError(f"ffmpeg gagal (kode {proc.returncode}): {proc.stderr[-1500:]}")


def probe(path: Path) -> dict:
    """Info dasar media tanpa ffprobe (parse keluaran `ffmpeg -i`)."""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    err = proc.stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    v = re.search(r"Stream #\S+.*?: Video: .*?(\d{2,5})x(\d{2,5})", err)
    return {"duration": dur, "has_video": bool(v), "has_audio": "Audio:" in err,
            "width": int(v.group(1)) if v else 0, "height": int(v.group(2)) if v else 0}


# ---------------------------------------------------------------- shots
def build_shot(src: Path, kind: str, frames: int, out: Path, config: Config, rng: random.Random,
               src_duration: float = 0.0) -> None:
    f = config.format
    w, h, fps = f.width, f.height, f.fps
    seconds = frames / fps
    enc = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(fps),
           "-frames:v", str(frames), str(out)]
    if kind == "photo":
        bw, bh = w * 3 // 2, h * 3 // 2
        zoom_in = rng.random() < 0.6
        z = "min(zoom+0.0011,1.18)" if zoom_in else "if(eq(on,0),1.18,max(zoom-0.0011,1.0))"
        vf = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps},setsar=1")
        run_ffmpeg(["-loop", "1", "-framerate", str(fps), "-i", str(src), "-vf", vf, *enc])
        return
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=bicubic,crop={w}:{h},fps={fps},setsar=1"
    if src_duration >= seconds + 0.5:
        offset = rng.uniform(0, min(src_duration - seconds - 0.2, src_duration * 0.5))
        run_ffmpeg(["-ss", f"{offset:.2f}", "-i", str(src), "-vf", vf, *enc])
    else:
        run_ffmpeg(["-stream_loop", "-1", "-i", str(src), "-vf", vf, *enc])


def build_video_track(config: Config, visuals: dict, out_dir: Path, rng: random.Random) -> tuple[Path, Path]:
    fps = config.format.fps
    shots_dir = out_dir / "shots"
    shots_dir.mkdir(exist_ok=True)
    entries: list[str] = []
    first_shot: Path | None = None
    all_shots = [(seg, s) for seg in visuals["segments"] for s in seg["shots"]]
    n = 0
    for i, (seg, shot) in enumerate(all_shots):
        seconds = shot["shot_duration"] + (TAIL_SECONDS + 0.3 if i == len(all_shots) - 1 else 0)
        frames = max(1, round(seconds * fps))
        out = shots_dir / f"shot_{n:03d}.mp4"
        build_shot(Path(shot["path"]), shot["kind"], frames, out, config, rng, float(shot.get("duration") or 0))
        if first_shot is None:
            first_shot = out
        entries.append(f"file 'shots/{out.name}'")
        n += 1
    (out_dir / "concat.txt").write_text("\n".join(entries) + "\n", encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "video_raw.mp4"], cwd=out_dir)
    return out_dir / "video_raw.mp4", first_shot  # type: ignore[return-value]


# ---------------------------------------------------------------- subtitle ASS
def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def caption_chunks(timings: dict, words_per_chunk: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    for seg in timings["segments"]:
        words = seg.get("words", [])
        used = 0
        for sent in seg.get("sentences", []):
            sent_words = [w for w in words[used:] if w["start"] <= sent["end"] + 0.01]
            used += len(sent_words)
            for k in range(0, len(sent_words), words_per_chunk):
                chunks.append(sent_words[k: k + words_per_chunk])
        if used < len(words):  # sisa kata yang tidak terpetakan
            rest = words[used:]
            for k in range(0, len(rest), words_per_chunk):
                chunks.append(rest[k: k + words_per_chunk])
    return [c for c in chunks if c]


def build_ass(timings: dict, cap: CaptionStyle, palette: Palette, config: Config, hook_text: str | None) -> str:
    w, h = config.format.width, config.format.height
    k = min(w, h) / 1080  # ukuran di config dirancang untuk sisi pendek 1080 px
    size, hook_size = max(12, int(cap.size * k)), max(12, int(cap.size * 1.05 * k))
    ow, hook_pad, margin = max(1, round(cap.outline_width * k)), max(2, round(18 * k)), max(8, int(60 * k))
    border = 3 if cap.box else 1
    outline_col = ass_color("#000000", 0x60) if cap.box else ass_color(cap.outline)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Caption,{cap.font},{size},{ass_color(cap.primary)},{ass_color(cap.highlight)},{outline_col},"
        f"&H80000000,-1,0,0,0,100,100,1,0,{border},{ow},{max(1, round(2 * k))},5,{margin},{margin},0,1",
        f"Style: Hook,{cap.font},{hook_size},{ass_color(palette.text)},{ass_color(palette.text)},"
        f"{ass_color(palette.accent)},&H00000000,-1,0,0,0,100,100,1,0,3,{hook_pad},0,8,{margin},{margin},0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    x, y = w // 2, int(h * cap.y)
    hl = ass_color(cap.highlight)
    chunks = caption_chunks(timings, cap.words)
    for ci, chunk in enumerate(chunks):
        start = chunk[0]["start"]
        nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else chunk[-1]["end"] + 0.6
        end = max(min(nxt, chunk[-1]["end"] + 0.6), start + 0.15)
        texts = [ass_escape(wd["text"].upper() if cap.uppercase else wd["text"]) for wd in chunk]
        # perkiraan lebar kata terpanjang; kecilkan kalau tidak muat di layar
        char_w = size * (0.66 if cap.uppercase else 0.56)
        longest = max(len(t) for t in texts) * char_w + 2 * ow
        sc = min(100, int(100 * (w - 2 * margin) / longest)) if longest > 0 else 100
        for wi, wd in enumerate(chunk):
            ws = start if wi == 0 else wd["start"]
            we = end if wi == len(chunk) - 1 else chunk[wi + 1]["start"]
            if we <= ws:
                continue
            parts = [f"{{\\c{hl}}}{t}{{\\r}}" if j == wi else t for j, t in enumerate(texts)]
            if cap.pop and wi == 0:
                big = round(sc * 1.14)
                pop = f"\\fscx{big}\\fscy{big}\\t(0,90,\\fscx{sc}\\fscy{sc})"
            else:
                pop = f"\\fscx{sc}\\fscy{sc}" if sc < 100 else ""
            lines.append(f"Dialogue: 1,{ass_time(ws)},{ass_time(we)},Caption,,0,0,0,,{{\\pos({x},{y}){pop}}}{' '.join(parts)}")
    if hook_text:
        first_end = timings["segments"][0]["end"] if timings["segments"] else 2.5
        lines.append(f"Dialogue: 2,{ass_time(0)},{ass_time(min(2.8, first_end))},Hook,,0,0,0,,"
                     f"{{\\pos({x},{int(h * 0.16)})\\fad(120,200)}}{ass_escape(hook_text.upper())}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- musik & render akhir
def pick_music(config: Config, rng: random.Random) -> Path | None:
    folder = config.path(config.music.folder)
    if not folder.exists():
        return None
    tracks = sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT)
    return rng.choice(tracks) if tracks else None


def _rel(path: Path, cwd: Path) -> str:
    try:
        return os.path.relpath(path, cwd).replace("\\", "/")
    except ValueError:  # beda drive di Windows
        return path.as_posix()


def render_final(config: Config, out_dir: Path, total: float, music: Path | None, rng: random.Random) -> Path:
    f = config.format
    T = total + TAIL_SECONDS
    fonts_dir = config.path(config.fonts.folder)
    sub = "subtitles=filename=captions.ass"
    if fonts_dir.exists() and any(fonts_dir.iterdir()):
        sub += f":fontsdir='{_rel(fonts_dir, out_dir)}'"
    inputs = ["-i", "video_raw.mp4", "-i", "voice.mp3"]
    norm = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    if music:
        mdur = probe(music)["duration"]
        offset = rng.uniform(0, max(0.0, mdur - T - 2)) if mdur > T + 4 else 0.0
        inputs += ["-stream_loop", "-1", "-ss", f"{offset:.2f}", "-i", _rel(music, out_dir)]
        audio = (f"[1:a]{fmt},apad=pad_dur=1[vo];[vo]asplit=2[voice][sc];"
                 f"[2:a]{fmt},volume={config.music.volume_db}dB,afade=t=in:st=0:d=1,"
                 f"afade=t=out:st={max(0.0, T - 1.5):.2f}:d=1.5[m];"
                 f"[m][sc]sidechaincompress=threshold=0.04:ratio=6:attack=15:release=350[duck];"
                 f"[voice][duck]amix=inputs=2:duration=first:normalize=0,{norm}[a]")
    else:
        audio = f"[1:a]{fmt},apad=pad_dur=1,{norm}[a]"
    graph = f"[0:v]{sub},format=yuv420p[v];{audio}"
    run_ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", f"{T:.3f}",
                "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-r", str(f.fps), "-g", str(f.fps * 2), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                "-movflags", "+faststart", "final.mp4"], cwd=out_dir)
    return out_dir / "final.mp4"


def validate_output(config: Config, path: Path) -> dict:
    if not path.exists() or path.stat().st_size < 10_000:
        raise AssembleError("final.mp4 tidak ada atau terlalu kecil")
    info = probe(path)
    f = config.format
    if not (info["has_video"] and info["has_audio"]):
        raise AssembleError(f"final.mp4 tidak lengkap: {info}")
    if (info["width"], info["height"]) != (f.width, f.height):
        raise AssembleError(f"resolusi {info['width']}x{info['height']} != {f.width}x{f.height}")
    if not (f.min_seconds - 1 <= info["duration"] <= f.max_seconds + 1.5):
        raise AssembleError(f"durasi {info['duration']:.1f} detik di luar batas")
    return info


# ---------------------------------------------------------------- thumbnail
def find_font(config: Config) -> str | None:
    dirs = [config.path(config.fonts.folder)] + [Path(d) for d in SYSTEM_FONT_DIRS]
    own = config.path(config.fonts.folder)
    if own.exists():
        files = sorted(p for p in own.iterdir() if p.suffix.lower() in (".ttf", ".otf"))
        bold = [p for p in files if re.search(r"black|extrabold|bold|heavy", p.name, re.I)]
        if bold or files:
            return str((bold or files)[0])
    for pattern in FONT_CANDIDATES:
        for d in dirs[1:]:
            if d.exists():
                hits = sorted(d.glob(pattern))
                if hits:
                    return str(hits[0])
    return None


def _font(path: str | None, size: int):
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: str | None, max_w: int, max_lines: int = 3,
             start: int = 170, stroke: int = 8):
    words = text.split()
    for size in range(start, 10, -max(1, start // 28)):
        font = _font(font_path, size)
        lines, cur = [], ""
        for wd in words:
            trial = f"{cur} {wd}".strip()
            if draw.textlength(trial, font=font) + stroke * 2 <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        if len(lines) <= max_lines and all(draw.textlength(l, font=font) + stroke * 2 <= max_w for l in lines):
            return font, lines, size
    font = _font(font_path, 12)
    return font, [text], 12


def make_thumbnail(frame: Path, text: str, layout: str, palette: Palette, out: Path, config: Config) -> Path:
    w, h = config.format.width, config.format.height
    img = ImageOps.fit(Image.open(frame).convert("RGB"), (w, h), Image.Resampling.LANCZOS)
    shade = Image.new("L", (1, 256))
    for y in range(256):
        shade.putpixel((0, y), int(150 * abs(y - 128) / 128))  # gelap di atas & bawah
    dark = Image.new("RGB", (w, h), palette.shade)
    img = Image.composite(dark, img, shade.resize((w, h)))
    draw = ImageDraw.Draw(img)
    font_path = find_font(config)
    text = " ".join(text.upper().split()[:6]) or config.channel.name.upper()
    k = min(w, h) / 1080
    s = lambda v: max(1, int(v * k))  # noqa: E731 - skala ukuran yang dirancang untuk 1080 px
    margin = int(w * 0.07)
    font, lines, size = fit_text(draw, text, font_path, w - 2 * margin - (s(60) if "band" in layout else 0),
                                 start=s(170), stroke=s(8))
    line_h = int(size * 1.08)
    block_h = line_h * len(lines)

    if layout in ("top_band", "bottom_band"):
        top = int(h * 0.12) if layout == "top_band" else int(h * 0.88) - block_h - s(60)
        draw.rectangle([margin // 2, top - s(30), w - margin // 2, top + block_h + s(30)], fill=palette.accent)
        fill, stroke_w, stroke_fill = palette.text, 0, None
    elif layout == "center_outline":
        top = (h - block_h) // 2
        fill, stroke_w, stroke_fill = palette.accent, s(10), "#000000"
    else:  # corner_tag
        top = int(h * 0.14) + s(60)
        tag_font = _font(font_path, s(64))
        tag = "HOW?"
        tw = draw.textlength(tag, font=tag_font)
        draw.rounded_rectangle([margin, top - s(120), margin + tw + s(50), top - s(30)], radius=s(18), fill=palette.accent)
        draw.text((margin + s(25), top - s(114)), tag, font=tag_font, fill=palette.text)
        fill, stroke_w, stroke_fill = "#FFFFFF", s(9), "#000000"

    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=font)
        draw.text(((w - lw) / 2, top + i * line_h), line, font=font, fill=fill,
                  stroke_width=stroke_w, stroke_fill=stroke_fill)

    for quality in (92, 85, 78, 70, 60):
        img.save(out, "JPEG", quality=quality, optimize=True)
        if out.stat().st_size <= MAX_THUMB_BYTES:
            break
    return out


# ---------------------------------------------------------------- orkestrasi
def _by_name(items, name):
    return next((x for x in items if x.name == name), items[0])


def run(config: Config, script: dict, timings: dict, visuals: dict, style: dict, out_dir: Path) -> dict:
    rng = random.Random(style.get("seed", 0))
    cap = _by_name(config.styles.caption_styles, style.get("caption_style"))
    palette = _by_name(config.styles.palettes, style.get("palette"))
    warnings: list[str] = []

    raw, first_shot = build_video_track(config, visuals, out_dir, rng)
    hook = script.get("thumbnail_text") if style.get("hook_overlay") else None
    (out_dir / "captions.ass").write_text(build_ass(timings, cap, palette, config, hook), encoding="utf-8")
    music = pick_music(config, rng)
    if not music:
        warnings.append("folder musik kosong: video dibuat tanpa musik latar")
    final = render_final(config, out_dir, timings["total"], music, rng)
    info = validate_output(config, final)

    frame = out_dir / "frame.jpg"
    first_dur = visuals["segments"][0]["shots"][0]["shot_duration"]
    run_ffmpeg(["-ss", f"{first_dur / 2:.2f}", "-i", str(first_shot), "-frames:v", "1", "-q:v", "2", str(frame)])
    thumb = make_thumbnail(frame, script.get("thumbnail_text") or script["title"], style.get("thumbnail_layout", "top_band"),
                           palette, out_dir / "thumbnail.jpg", config)

    result = {"video": str(final), "thumbnail": str(thumb), "duration": round(info["duration"], 2),
              "music": music.name if music else None, "warnings": warnings}
    (out_dir / "assemble.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    log.info("video %.1f detik selesai%s", info["duration"], f" dengan musik {music.name}" if music else "")
    return result
