"""Gambar AI bergaya animasi 3D, satu adegan per shot, dengan MASKOT yang sama di setiap adegan.

Kunci tampilan "animasi 3D" ala channel viral: satu karakter yang konsisten + latar polos seragam.
- Maskot dideskripsikan di config.visuals.character dan muncul di setiap prompt.
- Gambar referensi maskot disimpan di assets/character/mascot.png. Kalau belum ada, dibuat sekali
  secara otomatis lalu di-commit oleh job finalize, sehingga semua video berikutnya memakai maskot yang sama.

Urutan penyedia (config.visuals.ai_providers):
1. gemini_image - model gambar Gemini ("Nano Banana"), bisa memakai gambar referensi -> karakter paling
   konsisten. Memakai GEMINI_API_KEY, atau LLM_API_KEY kalau provider LLM = gemini. Model dipilih
   otomatis dari daftar model yang tersedia untuk key tersebut.
2. cloudflare   - Workers AI (FLUX), tanpa referensi; konsistensi lewat deskripsi teks yang identik.
3. pollinations - tanpa key, lambat, kualitas rendah.
Kalau semua gagal, visuals.py jatuh ke footage stok supaya video tetap jadi.
"""
from __future__ import annotations

import base64
import logging
import shutil
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
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
REFERENCE_REL = "assets/character/mascot.png"
ANGLES = ["wide establishing shot", "medium shot", "close-up detail shot", "low angle dramatic shot"]


class ImageGenError(Exception):
    pass


# ---------------------------------------------------------------- prompt
def build_prompt(config: Config, scene: str, variant: int = 0, with_reference: bool = False) -> str:
    v = config.visuals
    who = ("The mascot character from the reference image" if with_reference
           else f"The channel mascot ({v.character})")
    return (f"{v.ai_style}. {who}, {scene.strip().rstrip('.')}. {v.background}. "
            f"{ANGLES[variant % len(ANGLES)]}, vertical 9:16 composition, {v.ai_negative}")


def reference_prompt(config: Config) -> str:
    v = config.visuals
    return (f"{v.ai_style}. Character design reference sheet of ONE original mascot: {v.character}. "
            f"Full body, centered, neutral friendly pose, looking at the camera. {v.background}. "
            f"Vertical 9:16, {v.ai_negative}")


def reference_path(config: Config) -> Path:
    return config.path(REFERENCE_REL)


# ---------------------------------------------------------------- util
def _save_jpeg(data: bytes, dest: Path, crop_bottom: float = 0.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".raw")
    tmp.write_bytes(data)
    try:
        img = Image.open(tmp).convert("RGB")
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise ImageGenError(f"bukan gambar yang valid: {e}") from e
    if crop_bottom:
        w, h = img.size
        img = img.crop((0, 0, w, int(h * (1 - crop_bottom))))  # buang watermark di pojok bawah
    tmp.unlink(missing_ok=True)
    if min(img.size) < 256:
        raise ImageGenError(f"gambar terlalu kecil: {img.size}")
    if dest.suffix.lower() == ".png":
        img.save(dest, "PNG")
    else:
        img.save(dest, "JPEG", quality=92)
    return dest


# ---------------------------------------------------------------- penyedia
class GeminiImage:
    name = "gemini_image"
    supports_reference = True

    def __init__(self, config: Config, session=None):
        self.key = env("GEMINI_API_KEY", required=False) or (
            env("LLM_API_KEY", required=False) if config.llm.provider == "gemini" else None)
        self.preferred = list(config.visuals.gemini_image_models)
        self.session = session
        self._models: list[str] | None = None

    @property
    def available(self) -> bool:
        return bool(self.key)

    def models(self) -> list[str]:
        """Model gambar yang benar-benar tersedia untuk key ini (nama model sering berubah)."""
        if self._models is not None:
            return self._models
        found: list[str] = []
        try:
            data = http.get_json(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": self.key},
                                 params={"pageSize": 200}, session=self.session, attempts=3)
            for m in data.get("models", []):
                name = m.get("name", "").removeprefix("models/")
                if "image" in name and "generateContent" in (m.get("supportedGenerationMethods") or []) \
                        and "imagen" not in name:
                    found.append(name)
        except Exception as e:  # noqa: BLE001
            log.warning("daftar model Gemini gagal diambil: %s", str(e)[:150])
        ordered = [m for m in self.preferred if m in found] + sorted(
            (m for m in found if m not in self.preferred), key=lambda n: ("flash" not in n, "preview" in n, n))
        self._models = ordered or list(self.preferred)
        log.info("model gambar Gemini: %s", ", ".join(self._models[:3]) or "(tidak ada)")
        return self._models

    def generate(self, prompt: str, dest: Path, seed: int, reference: Path | None = None) -> Path:
        parts: list[dict] = []
        if reference and reference.exists():
            mime = "image/png" if reference.suffix.lower() == ".png" else "image/jpeg"
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(reference.read_bytes()).decode()}})
        parts.append({"text": prompt})
        payload = {"contents": [{"role": "user", "parts": parts}],
                   "generationConfig": {"responseModalities": ["IMAGE"]}}
        last: Exception | None = None
        for model in list(self.models()):
            try:
                data = http.post_json(f"{GEMINI_BASE}/models/{model}:generateContent", payload,
                                      headers={"x-goog-api-key": self.key}, session=self.session,
                                      timeout=180, attempts=3, base_delay=5.0)
            except http.HTTPError as e:
                last = e
                if e.status in (400, 404):  # model tidak cocok / tidak ada: coba model berikutnya
                    self._models = [m for m in self._models if m != model]
                    continue
                raise
            for cand in data.get("candidates") or []:
                for part in (cand.get("content") or {}).get("parts") or []:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        return _save_jpeg(base64.b64decode(inline["data"]), dest)
            last = ImageGenError(f"{model} tidak mengembalikan gambar")
        raise ImageGenError(f"Gemini Image gagal: {last}")


class Cloudflare:
    name = "cloudflare"
    supports_reference = False

    def __init__(self, config: Config, session=None):
        self.account = env("CF_ACCOUNT_ID", required=False)
        self.token = env("CF_API_TOKEN", required=False)
        self.model = config.visuals.cloudflare_model
        self.session = session

    @property
    def available(self) -> bool:
        return bool(self.account and self.token)

    def generate(self, prompt: str, dest: Path, seed: int, reference: Path | None = None) -> Path:
        data = http.post_json(
            CF_URL.format(account=self.account, model=self.model),
            {"prompt": prompt[:2000], "steps": 6},  # flux-1-schnell menolak parameter lain (400)
            headers={"Authorization": f"Bearer {self.token}"},
            session=self.session, timeout=120, attempts=4, base_delay=3.0,
        )
        img_b64 = (data.get("result") or {}).get("image")
        if not data.get("success", True) or not img_b64:
            raise ImageGenError(f"Cloudflare tidak mengembalikan gambar: {str(data.get('errors'))[:200]}")
        return _save_jpeg(base64.b64decode(img_b64), dest)


class Pollinations:
    name = "pollinations"
    supports_reference = False
    min_interval = 47.0  # batas tanpa key: 1 gambar / 45 detik per IP
    available = True

    def __init__(self, config: Config, session=None):
        self.session = session
        self.width, self.height = 768, 1344
        self._last = 0.0

    def generate(self, prompt: str, dest: Path, seed: int, reference: Path | None = None) -> Path:
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


PROVIDERS = {"gemini_image": GeminiImage, "cloudflare": Cloudflare, "pollinations": Pollinations}


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
        self.reference: Path | None = None
        self.new_reference: Path | None = None

    def ensure_reference(self, work_dir: Path) -> Path | None:
        """Pakai maskot dari repo; kalau belum ada, buat sekali (disalin ke repo oleh finalize)."""
        repo_ref = reference_path(self.config)
        if repo_ref.exists():
            self.reference = repo_ref
            return repo_ref
        dest = work_dir / "mascot.png"
        try:
            self.generate(reference_prompt(self.config), dest, seed=1, scene_prompt_for_reference=None)
        except ImageGenError as e:
            log.warning("gambar referensi maskot gagal dibuat: %s", str(e)[:150])
            return None
        log.info("maskot baru dibuat: %s", dest)
        self.reference = self.new_reference = dest
        return dest

    def generate(self, prompt: str, dest: Path, seed: int,
                 scene_prompt_for_reference: str | None = None) -> tuple[str, Path]:
        """`scene_prompt_for_reference` = versi prompt untuk penyedia yang memakai gambar referensi."""
        last: Exception | None = None
        for p in self.providers:
            if p.name in self.disabled:
                continue
            use_ref = bool(p.supports_reference and self.reference and scene_prompt_for_reference)
            try:
                return p.name, p.generate(scene_prompt_for_reference if use_ref else prompt, dest, seed,
                                          reference=self.reference if use_ref else None)
            except (ImageGenError, RetryableError, http.HTTPError) as e:
                last = e
                log.warning("gambar AI via %s gagal: %s", p.name, str(e)[:200])
                if isinstance(e, http.HTTPError) and e.status in (401, 403, 429):
                    self.disabled.add(p.name)  # key salah / kuota habis: jangan dicoba lagi di video ini
        raise ImageGenError(f"semua penyedia gambar AI gagal; terakhir: {last}")


def adopt_reference(config: Config, run_root: Path) -> Path | None:
    """Dipanggil finalize: salin maskot baru dari hasil run ke assets/ kalau repo belum punya."""
    dest = reference_path(config)
    if dest.exists():
        return None
    for cand in sorted(run_root.rglob("mascot.png")):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cand, dest)
        log.info("maskot disimpan ke %s", dest)
        return dest
    return None
