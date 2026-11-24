"""Gambar AI bergaya animasi 3D, satu adegan per shot, sesuai isi narasi.

Urutan penyedia (bisa diatur di config.visuals.ai_providers):
1. cloudflare  - Workers AI (FLUX), kuota gratis harian; perlu CF_ACCOUNT_ID + CF_API_TOKEN
2. pollinations - tanpa key, tapi 1 gambar / ~45 detik per IP dan sering penuh
Kalau semua gagal, visuals.py jatuh ke footage stok (Pixabay/Pexels) supaya video tetap jadi.
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from . import http
from . import retry as retry_mod
from .config import Config, env
from .retry import RetryableError

log = logging.getLogger("ytauto.retry")  # ikut handler log pipeline
CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"


class ImageGenError(Exception):
    pass


def build_prompt(config: Config, scene: str, variant: int = 0) -> str:
    angles = ["wide establishing shot", "medium shot", "close-up detail shot", "low angle dramatic shot"]
    v = config.visuals
    return (f"{v.ai_style}, {scene.strip().rstrip('.')}, {angles[variant % len(angles)]}, "
            f"vertical 9:16 composition, {v.ai_negative}")


def _save_jpeg(data: bytes, dest: Path, crop_bottom: float = 0.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".raw")
    tmp.write_bytes(data)
    try:
        img = Image.open(tmp).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ImageGenError(f"bukan gambar yang valid: {e}") from e
    finally:
        pass
    if crop_bottom:
        w, h = img.size
        img = img.crop((0, 0, w, int(h * (1 - crop_bottom))))  # buang watermark di pojok bawah
    if min(img.size) < 256:
        tmp.unlink(missing_ok=True)
        raise ImageGenError(f"gambar terlalu kecil: {img.size}")
    img.save(dest, "JPEG", quality=92)
    tmp.unlink(missing_ok=True)
    return dest


class Cloudflare:
    name = "cloudflare"

    def __init__(self, config: Config, session=None):
        self.account = env("CF_ACCOUNT_ID", required=False)
        self.token = env("CF_API_TOKEN", required=False)
        self.model = config.visuals.cloudflare_model
        self.session = session

    @property
    def available(self) -> bool:
        return bool(self.account and self.token)

    def generate(self, prompt: str, dest: Path, seed: int) -> Path:
        data = http.post_json(
            CF_URL.format(account=self.account, model=self.model),
            {"prompt": prompt[:2000], "steps": 6, "seed": seed},
            headers={"Authorization": f"Bearer {self.token}"},
            session=self.session, timeout=120, attempts=4, base_delay=3.0,
        )
        img_b64 = (data.get("result") or {}).get("image")
        if not data.get("success", True) or not img_b64:
            raise ImageGenError(f"Cloudflare tidak mengembalikan gambar: {str(data.get('errors'))[:200]}")
        return _save_jpeg(base64.b64decode(img_b64), dest)


class Pollinations:
    name = "pollinations"
    min_interval = 47.0  # batas tanpa key: 1 gambar / 45 detik per IP

    def __init__(self, config: Config, session=None):
        self.session = session
        self.width, self.height = 768, 1344
        self._last = 0.0

    available = True

    def generate(self, prompt: str, dest: Path, seed: int) -> Path:
        url = POLLINATIONS_URL.format(prompt=quote(prompt[:900], safe=""))
        params = {"width": self.width, "height": self.height, "nologo": "true", "seed": seed}
        for attempt in range(1, 6):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                retry_mod.SLEEP(wait)
            self._last = time.monotonic()
            try:
                resp = http.request("GET", url, params=params, session=self.session, attempts=1, timeout=150)
            except (RetryableError, http.HTTPError) as e:  # 402 (limit) / 503 (penuh)
                log.warning("Pollinations gagal (percobaan %d/5): %s", attempt, str(e)[:150])
                continue
            if "image" not in resp.headers.get("Content-Type", "image"):
                log.warning("Pollinations tidak mengirim gambar (percobaan %d/5)", attempt)
                continue
            return _save_jpeg(resp.content, dest, crop_bottom=0.05)
        raise ImageGenError("Pollinations terus sibuk / kena batas")


PROVIDERS = {"cloudflare": Cloudflare, "pollinations": Pollinations}


class ImageGenerator:
    def __init__(self, config: Config, session=None):
        self.config = config
        self.providers = []
        for name in config.visuals.ai_providers:
            p = PROVIDERS[name](config, session)
            if p.available:
                self.providers.append(p)
            else:
                log.warning("penyedia gambar %s dilewati (secret belum diisi)", name)
        self.disabled: set[str] = set()

    def generate(self, prompt: str, dest: Path, seed: int) -> tuple[str, Path]:
        last: Exception | None = None
        for p in self.providers:
            if p.name in self.disabled:
                continue
            try:
                return p.name, p.generate(prompt, dest, seed)
            except (ImageGenError, RetryableError, http.HTTPError) as e:
                last = e
                log.warning("gambar AI via %s gagal: %s", p.name, str(e)[:200])
                if isinstance(e, http.HTTPError) and e.status in (401, 403):
                    self.disabled.add(p.name)  # key salah / kuota habis: jangan dicoba lagi di video ini
        raise ImageGenError(f"semua penyedia gambar AI gagal; terakhir: {last}")
