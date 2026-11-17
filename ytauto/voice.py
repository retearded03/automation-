"""Langkah 4: text-to-speech dengan edge-tts + timestamp per kata dan per kalimat untuk subtitle.

Tiap segmen disintesis terpisah lalu digabung, sehingga batas waktu tiap segmen diketahui persis
(untuk mencocokkan footage). edge-tts mengeluarkan MP3 CBR 48 kbps, jadi durasi = byte * 8 / 48000.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import edge_tts

from .config import Config
from .retry import retry

BITRATE = 48_000
TICKS = 10_000_000  # offset edge-tts dalam satuan 100 ns
log = logging.getLogger("ytauto.voice")


class VoiceError(Exception):
    pass


def mp3_duration(data: bytes) -> float:
    return len(data) * 8 / BITRATE


def rate_str(percent: int) -> str:
    return f"{percent:+d}%"


CALL_TIMEOUT = 45  # detik; layanan edge-tts kadang menggantung tanpa error


async def _stream(text: str, voice: str, rate: str) -> tuple[bytes, list[dict]]:
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary",
                                           connect_timeout=10, receive_timeout=20)
    except TypeError:  # versi edge-tts tanpa parameter timeout (atau pengganti di tes)
        communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    audio = bytearray()
    words: list[dict] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            start = chunk["offset"] / TICKS
            words.append({"text": chunk["text"], "start": start, "end": start + chunk["duration"] / TICKS})
    return bytes(audio), words


@retry(attempts=4, base_delay=2.0, retry_on=(Exception,))
def synthesize(text: str, voice: str, rate_percent: int) -> tuple[bytes, list[dict]]:
    audio, words = asyncio.run(asyncio.wait_for(_stream(text, voice, rate_str(rate_percent)), CALL_TIMEOUT))
    if not audio:
        raise VoiceError("edge-tts tidak mengembalikan audio")
    return audio, words


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def sentence_timings(text: str, words: list[dict], seg_start: float, seg_end: float) -> list[dict]:
    """Petakan kalimat ke rentang waktu berdasarkan urutan kata dari WordBoundary."""
    sentences = split_sentences(text)
    if not words:
        # tanpa boundary: bagi rata menurut jumlah kata
        total = sum(len(s.split()) for s in sentences) or 1
        out, t = [], seg_start
        for s in sentences:
            d = (seg_end - seg_start) * len(s.split()) / total
            out.append({"text": s, "start": round(t, 3), "end": round(t + d, 3)})
            t += d
        return out
    out, idx = [], 0
    for n, s in enumerate(sentences):
        count = max(1, len(s.split()))
        chunk = words[idx: idx + count] if n < len(sentences) - 1 else words[idx:]
        if not chunk:
            chunk = [words[-1]]
        out.append({"text": s, "start": round(chunk[0]["start"], 3), "end": round(chunk[-1]["end"], 3)})
        idx += count
    return out


def synthesize_script(config: Config, script: dict, voice: str) -> tuple[bytes, dict]:
    rate = config.voice.base_rate_percent
    while True:
        audio = bytearray()
        segments = []
        t = 0.0
        for i, seg in enumerate(script["segments"]):
            data, words = synthesize(seg["text"], voice, rate)
            dur = mp3_duration(data)
            abs_words = [{"text": w["text"], "start": round(t + w["start"], 3), "end": round(t + w["end"], 3)} for w in words]
            segments.append({
                "index": i,
                "start": round(t, 3),
                "end": round(t + dur, 3),
                "duration": round(dur, 3),
                "words": abs_words,
                "sentences": sentence_timings(seg["text"], abs_words, t, t + dur),
            })
            audio.extend(data)
            t += dur
        total = round(t, 3)
        if total <= config.format.max_seconds:
            break
        if rate >= config.voice.max_rate_percent:
            raise VoiceError(f"narasi {total:.1f} detik melebihi batas {config.format.max_seconds} detik walau sudah dipercepat")
        rate = min(rate + 6, config.voice.max_rate_percent)
        log.info("narasi %.1f detik terlalu panjang, ulang dengan rate %s", total, rate_str(rate))
    if total < config.format.min_seconds:
        raise VoiceError(f"narasi hanya {total:.1f} detik (minimal {config.format.min_seconds})")
    return bytes(audio), {"voice": voice, "rate": rate_str(rate), "total": total, "segments": segments}


def run(config: Config, script: dict, voice: str, out_dir: Path) -> dict:
    audio, timings = synthesize_script(config, script, voice)
    (out_dir / "voice.mp3").write_bytes(audio)
    (out_dir / "timings.json").write_text(json.dumps(timings, indent=1), encoding="utf-8")
    log.info("suara %s: %.1f detik, %d segmen", voice, timings["total"], len(timings["segments"]))
    return timings
