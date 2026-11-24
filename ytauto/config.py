"""Memuat dan memvalidasi config.yaml, serta membaca secret dari environment."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


class ChannelCfg(BaseModel):
    name: str = "My Channel"
    niche: str = "How things work"
    language: str = "en"
    audience: str = "English-speaking viewers"
    category_id: str = "27"
    fallback_visual_keywords: list[str] = Field(default_factory=lambda: ["technology"])


class TrendsCfg(BaseModel):
    regions: list[str] = Field(default_factory=lambda: ["US"])
    category_ids: list[str] = Field(default_factory=list)
    include_general_chart: bool = True
    max_results_per_chart: int = Field(50, ge=1, le=50)
    blocked_category_ids: list[str] = Field(default_factory=list)
    max_trends_for_llm: int = 60


class ScheduleCfg(BaseModel):
    videos_per_day: int = Field(1, ge=1, le=20)
    timezone: str = "America/New_York"
    publish_slots: list[str] = Field(default_factory=lambda: ["18:00"])
    min_lead_minutes: int = 20

    @field_validator("publish_slots")
    @classmethod
    def _slots(cls, v: list[str]) -> list[str]:
        for s in v:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s):
                raise ValueError(f"slot jam tidak valid: {s!r} (format HH:MM)")
        return v


class UploadCfg(BaseModel):
    privacy_status: Literal["scheduled", "private", "unlisted", "public"] = "private"
    contains_synthetic_media: bool = False
    made_for_kids: bool = False
    set_thumbnail: bool = True
    notify_subscribers: bool = True


class FormatCfg(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    max_seconds: float = 58
    min_seconds: float = 15
    target_words: int = 135

    @property
    def vertical(self) -> bool:
        return self.height > self.width


Effort = Literal["low", "medium", "high", "xhigh", "max"]
LLM_STEPS = ("topic_picker", "script_writer", "fact_check", "quality_gate")


class StepModel(BaseModel):
    model: str
    effort: Effort = "medium"


class FallbackProvider(BaseModel):
    """Penyedia cadangan berformat OpenAI (/chat/completions): Cent AI, Groq, OpenRouter, DeepSeek, ..."""
    name: str
    base_url: str
    model: str
    key_env: str
    temperature: float = 0.8


class LLMCfg(BaseModel):
    provider: Literal["gemini", "claude"] = "gemini"
    gemini_model: str = "gemini-flash-latest"
    gemini_fallback_models: list[str] = Field(default_factory=lambda: ["gemini-flash-lite-latest", "gemini-3.8-flash"])
    gemini_rounds: int = Field(3, ge=1, le=6)
    gemini_round_wait: float = Field(90, ge=0)
    # model Claude bawaan untuk langkah yang tidak diatur di claude_steps
    claude_model: str = "claude-sonnet-5-5"
    claude_effort: Effort = "medium"
    claude_steps: dict[str, StepModel] = Field(default_factory=dict)
    # dicoba berurutan kalau provider utama gagal; yang secret-nya kosong dilewati
    fallback_providers: list[FallbackProvider] = Field(default_factory=list)

    @field_validator("claude_steps")
    @classmethod
    def _steps(cls, v: dict) -> dict:
        unknown = set(v) - set(LLM_STEPS)
        if unknown:
            raise ValueError(f"langkah tidak dikenal di llm.claude_steps: {sorted(unknown)}; pilihan: {list(LLM_STEPS)}")
        return v
    temperature: float = 0.9


class ScriptCfg(BaseModel):
    min_quality_score: float = 7
    min_segments: int = 4
    max_segments: int = 8
    sources_per_video: int = 3
    max_source_chars: int = 6000
    max_attempts: int = 2


class VoiceCfg(BaseModel):
    voices: list[str] = Field(default_factory=lambda: ["en-US-AndrewNeural"])
    base_rate_percent: int = 0
    max_rate_percent: int = 25


class VisualsCfg(BaseModel):
    # ai_image = gambar AI bergaya 3D per adegan (stok jadi cadangan); stock = footage Pixabay/Pexels saja
    mode: Literal["ai_image", "stock"] = "stock"
    ai_providers: list[Literal["cloudflare", "pollinations"]] = Field(default_factory=lambda: ["cloudflare", "pollinations"])
    cloudflare_model: str = "@cf/black-forest-labs/flux-1-schnell"
    ai_style: str = ("stylized 3D animation render, colorful animated explainer video style, soft global illumination, "
                     "clean simple shapes, expressive cartoon characters, cinematic lighting")
    ai_negative: str = "no text, no letters, no logos, no watermark"
    providers: list[Literal["pexels", "pixabay"]] = Field(default_factory=lambda: ["pexels", "pixabay"])
    seconds_per_shot: float = 3.2
    max_shots_per_segment: int = 3
    avoid_reuse_days: int = 60
    max_download_mb: int = 80


class MusicCfg(BaseModel):
    folder: str = "assets/music"
    volume_db: float = -16


class FontsCfg(BaseModel):
    folder: str = "assets/fonts"


class CaptionStyle(BaseModel):
    name: str
    font: str = "Roboto"
    size: int = 86
    primary: str = "#FFFFFF"
    highlight: str = "#FFD400"
    outline: str = "#000000"
    outline_width: int = 6
    words: int = Field(2, ge=1, le=5)
    uppercase: bool = True
    y: float = Field(0.62, ge=0.1, le=0.95)
    pop: bool = True
    box: bool = False


class Palette(BaseModel):
    name: str
    accent: str
    text: str
    shade: str


class StylesCfg(BaseModel):
    avoid_recent_combinations: int = 15
    hook_types: list[str] = Field(default_factory=lambda: ["direct question"])
    structures: list[str] = Field(default_factory=lambda: ["step-by-step process"])
    caption_styles: list[CaptionStyle] = Field(default_factory=lambda: [CaptionStyle(name="default")])
    palettes: list[Palette] = Field(
        default_factory=lambda: [Palette(name="default", accent="#FFD400", text="#111111", shade="#000000")]
    )
    thumbnail_layouts: list[str] = Field(default_factory=lambda: ["top_band"])
    hook_overlay: list[bool] = Field(default_factory=lambda: [True])


class NotifyCfg(BaseModel):
    telegram: bool = True
    send_video_on_dry_run: bool = True


class PathsCfg(BaseModel):
    work_dir: str = "work"
    history_file: str = "history.json"


class Config(BaseModel):
    channel: ChannelCfg = Field(default_factory=ChannelCfg)
    trends: TrendsCfg = Field(default_factory=TrendsCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    upload: UploadCfg = Field(default_factory=UploadCfg)
    format: FormatCfg = Field(default_factory=FormatCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    script: ScriptCfg = Field(default_factory=ScriptCfg)
    voice: VoiceCfg = Field(default_factory=VoiceCfg)
    visuals: VisualsCfg = Field(default_factory=VisualsCfg)
    music: MusicCfg = Field(default_factory=MusicCfg)
    fonts: FontsCfg = Field(default_factory=FontsCfg)
    styles: StylesCfg = Field(default_factory=StylesCfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)

    # Folder dasar untuk path relatif (folder tempat config.yaml berada)
    base_dir: Path = Field(default=PROJECT_ROOT, exclude=True)

    @model_validator(mode="after")
    def _check(self) -> "Config":
        if len(self.schedule.publish_slots) < 1:
            raise ValueError("schedule.publish_slots tidak boleh kosong")
        if self.script.min_segments > self.script.max_segments:
            raise ValueError("script.min_segments > script.max_segments")
        return self

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.base_dir / p


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if not path.exists():
        raise ConfigError(f"config tidak ditemukan: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = Config(**data)
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml tidak valid: {e}") from e
    except ValueError as e:  # pydantic ValidationError turunan ValueError
        raise ConfigError(f"config.yaml tidak valid:\n{e}") from e
    cfg.base_dir = path.resolve().parent
    return cfg


def env(name: str, required: bool = True) -> str | None:
    """Ambil secret dari environment. Pesan error tidak pernah memuat nilainya."""
    val = os.environ.get(name, "").strip()
    if not val:
        if required:
            raise ConfigError(f"secret/environment variable {name} belum diisi")
        return None
    return val
