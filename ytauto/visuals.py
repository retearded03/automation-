"""Langkah 5: ambil stock footage berlisensi bebas (Pexels, cadangan Pixabay) per segmen + atribusi."""
from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path

from . import http
from .config import Config, env
from .retry import RetryableError

log = logging.getLogger("ytauto.visuals")

PEXELS_VIDEO = "https://api.pexels.com/videos/search"
PEXELS_PHOTO = "https://api.pexels.com/v1/search"
PIXABAY_VIDEO = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE = "https://pixabay.com/api/"


class VisualsError(Exception):
    pass


@dataclass
class Candidate:
    provider: str
    id: str
    kind: str  # "video" | "photo"
    url: str
    width: int
    height: int
    duration: float
    page_url: str
    author: str
    author_url: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"


# ---------------------------------------------------------------- provider
def pexels_videos(query: str, api_key: str, vertical: bool, min_height: int, session=None) -> list[Candidate]:
    data = http.get_json(PEXELS_VIDEO, session=session, headers={"Authorization": api_key}, params={
        "query": query, "orientation": "portrait" if vertical else "landscape", "size": "medium", "per_page": 15,
    })
    out = []
    for v in data.get("videos", []):
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")
                 and (f.get("height") or 0) >= min_height * 0.66]
        if not files:
            continue
        # file terkecil yang masih >= resolusi target; kalau tidak ada, yang terbesar
        files.sort(key=lambda f: (f.get("height") or 0))
        good = [f for f in files if (f.get("height") or 0) >= min_height]
        f = good[0] if good else files[-1]
        user = v.get("user") or {}
        out.append(Candidate("pexels", str(v["id"]), "video", f["link"], f.get("width") or 0, f.get("height") or 0,
                             float(v.get("duration") or 0), v.get("url", ""), user.get("name", "Pexels"), user.get("url", "")))
    return out


def pexels_photos(query: str, api_key: str, vertical: bool, session=None) -> list[Candidate]:
    data = http.get_json(PEXELS_PHOTO, session=session, headers={"Authorization": api_key}, params={
        "query": query, "orientation": "portrait" if vertical else "landscape", "per_page": 15,
    })
    out = []
    for p in data.get("photos", []):
        src = p.get("src") or {}
        url = src.get("large2x") or src.get("original")
        if url:
            out.append(Candidate("pexels", f"photo{p['id']}", "photo", url, p.get("width") or 0, p.get("height") or 0, 0.0,
                                 p.get("url", ""), p.get("photographer", "Pexels"), p.get("photographer_url", "")))
    return out


def pixabay_videos(query: str, api_key: str, vertical: bool, min_height: int, session=None) -> list[Candidate]:
    data = http.get_json(PIXABAY_VIDEO, session=session, params={
        "key": api_key, "q": query[:100], "per_page": 20, "safesearch": "true",
    })
    out = []
    for h in data.get("hits", []):
        vids = h.get("videos") or {}
        pick = None
        for size in ("medium", "large", "small"):
            f = vids.get(size) or {}
            if f.get("url") and max(f.get("width") or 0, f.get("height") or 0) >= min_height * 0.66:
                pick = f
                break
        if not pick:
            continue
        user = h.get("user", "Pixabay")
        out.append(Candidate("pixabay", str(h["id"]), "video", pick["url"], pick.get("width") or 0, pick.get("height") or 0,
                             float(h.get("duration") or 0), h.get("pageURL", ""), user,
                             f"https://pixabay.com/users/{user}-{h.get('user_id', '')}/"))
    # orientasi sesuai format diprioritaskan (video lain akan di-crop)
    out.sort(key=lambda c: (c.height > c.width) != vertical)
    return out


def pixabay_photos(query: str, api_key: str, vertical: bool, session=None) -> list[Candidate]:
    data = http.get_json(PIXABAY_IMAGE, session=session, params={
        "key": api_key, "q": query[:100], "image_type": "photo", "safesearch": "true", "per_page": 20,
        "orientation": "vertical" if vertical else "horizontal",
    })
    out = []
    for h in data.get("hits", []):
        url = h.get("largeImageURL") or h.get("webformatURL")
        if not url:
            continue
        user = h.get("user", "Pixabay")
        out.append(Candidate("pixabay", f"photo{h['id']}", "photo", url, h.get("imageWidth") or 0,
                             h.get("imageHeight") or 0, 0.0, h.get("pageURL", ""), user,
                             f"https://pixabay.com/users/{user}-{h.get('user_id', '')}/"))
    return out


# ---------------------------------------------------------------- pemilihan
def choose(cands: list[Candidate], used: set[str], min_duration: float, rng: random.Random) -> Candidate | None:
    fresh = [c for c in cands if c.key not in used]
    if not fresh:
        return None
    long_enough = [c for c in fresh if c.kind == "photo" or c.duration >= min_duration]
    pool = (long_enough or fresh)[:6]  # acak di antara hasil teratas supaya visual bervariasi
    return rng.choice(pool)


def shots_for(duration: float, config: Config) -> int:
    v = config.visuals
    return max(1, min(v.max_shots_per_segment, math.ceil(duration / v.seconds_per_shot)))


class Finder:
    def __init__(self, config: Config, used: set[str], rng: random.Random, session=None):
        self.config = config
        self.used = set(used)
        self.rng = rng
        self.session = session
        # Cukup salah satu key. Pexels sempat menutup pendaftaran key baru, jadi keduanya opsional.
        providers = config.visuals.providers
        self.pexels_key = env("PEXELS_API_KEY", required=False) if "pexels" in providers else None
        self.pixabay_key = env("PIXABAY_API_KEY", required=False) if "pixabay" in providers else None
        if not (self.pexels_key or self.pixabay_key):
            raise VisualsError("isi minimal satu secret: PIXABAY_API_KEY atau PEXELS_API_KEY")
        self.vertical = config.format.vertical
        self.min_h = config.format.height if self.vertical else config.format.width
        self.min_h = min(self.min_h, 1920)

    def _search(self, query: str) -> list[Candidate]:
        cands: list[Candidate] = []
        for provider in self.config.visuals.providers:
            try:
                if provider == "pexels" and self.pexels_key:
                    cands += pexels_videos(query, self.pexels_key, self.vertical, self.min_h, self.session)
                elif provider == "pixabay" and self.pixabay_key:
                    cands += pixabay_videos(query, self.pixabay_key, self.vertical, self.min_h, self.session)
            except (http.HTTPError, RetryableError) as e:
                log.warning("%s gagal untuk %r: %s", provider, query, e)
            if [c for c in cands if c.key not in self.used]:
                break  # provider pertama sudah cukup
        return cands

    def find(self, keywords: list[str], min_duration: float) -> Candidate:
        queries = list(dict.fromkeys(keywords + [" ".join(k.split()[:1]) for k in keywords]
                                     + list(self.config.channel.fallback_visual_keywords)))
        for q in queries:
            c = choose(self._search(q), self.used, min_duration, self.rng)
            if c:
                self.used.add(c.key)
                return c
        # pilihan terakhir: foto (diberi efek zoom saat perakitan)
        for q in queries:
            photos: list[Candidate] = []
            try:
                if self.pexels_key:
                    photos += pexels_photos(q, self.pexels_key, self.vertical, self.session)
                if self.pixabay_key and not [p for p in photos if p.key not in self.used]:
                    photos += pixabay_photos(q, self.pixabay_key, self.vertical, self.session)
            except (http.HTTPError, RetryableError) as e:
                log.warning("pencarian foto gagal untuk %r: %s", q, e)
            c = choose(photos, self.used, 0, self.rng)
            if c:
                self.used.add(c.key)
                return c
        raise VisualsError(f"tidak ada footage untuk kata kunci {keywords}")


def run(config: Config, script: dict, timings: dict, out_dir: Path, used_ids: set[str], seed: int,
        session=None) -> dict:
    rng = random.Random(seed)
    finder = Finder(config, used_ids, rng, session)
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = config.visuals.max_download_mb * 1024 * 1024
    plan = []
    for seg, tm in zip(script["segments"], timings["segments"]):
        n = shots_for(tm["duration"], config)
        shot_dur = tm["duration"] / n
        kws = list(seg["visual_keywords"])
        shots = []
        for k in range(n):
            rotated = kws[k % len(kws):] + kws[: k % len(kws)]  # tiap shot mulai dari kata kunci berbeda
            c = finder.find(rotated, shot_dur)
            ext = ".mp4" if c.kind == "video" else ".jpg"
            path = clips_dir / f"seg{tm['index']:02d}_{k}_{c.provider}_{c.id}{ext}"
            http.download(c.url, path, max_bytes=max_bytes, session=session)
            shots.append({**asdict(c), "key": c.key, "path": str(path), "shot_duration": round(shot_dur, 3)})
        plan.append({"segment": tm["index"], "duration": tm["duration"], "shots": shots})

    attribution = []
    seen = set()
    for seg in plan:
        for s in seg["shots"]:
            if s["key"] in seen:
                continue
            seen.add(s["key"])
            site = "Pexels" if s["provider"] == "pexels" else "Pixabay"
            attribution.append({"text": f"{'Photo' if s['kind'] == 'photo' else 'Video'} by {s['author']} on {site}",
                                "url": s["page_url"], "key": s["key"]})
    result = {"segments": plan, "attribution": attribution, "clip_ids": sorted(seen)}
    (out_dir / "visuals.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    log.info("visual: %d shot dari %d klip unik", sum(len(s["shots"]) for s in plan), len(seen))
    return result
