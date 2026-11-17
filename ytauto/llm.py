"""Antarmuka LLM tunggal dengan dua provider: Gemini (default, ada free tier) dan Claude."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from string import Template
from typing import Any, Protocol

from . import http
from .config import Config, env
from .retry import RetryableError

log = logging.getLogger("ytauto.retry")  # ikut handler log pipeline
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class LLMError(Exception):
    pass


class LLM(Protocol):
    def complete_json(self, system: str, prompt: str, temperature: float | None = None) -> Any: ...


def render_prompt(name: str, **values: Any) -> str:
    """Isi template prompts/<name>.md. Placeholder memakai sintaks $nama."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return Template(text).substitute({k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)) for k, v in values.items()})


def extract_json(text: str) -> Any:
    """Ambil JSON dari jawaban LLM, toleran terhadap ```json fences``` dan teks pengantar."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError(f"jawaban LLM bukan JSON yang valid: {text[:200]!r}")


class _BaseClient:
    json_attempts = 3

    def __init__(self, model: str, api_key: str, temperature: float = 0.9, session=None):
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.session = session

    def _call(self, system: str, prompt: str, temperature: float) -> str:  # pragma: no cover - abstrak
        raise NotImplementedError

    def complete_json(self, system: str, prompt: str, temperature: float | None = None) -> Any:
        temp = self.temperature if temperature is None else temperature
        last: Exception | None = None
        p = prompt
        for _ in range(self.json_attempts):
            text = self._call(system, p, temp)
            try:
                return extract_json(text)
            except LLMError as e:
                last = e
                p = prompt + "\n\nIMPORTANT: Your previous reply was not valid JSON. Reply with ONLY valid JSON."
        raise LLMError(str(last))


class GeminiClient(_BaseClient):
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    # Overload (503) & rate limit (429) di free tier bisa berlangsung lama: retry sabar per model,
    # lalu pindah ke model cadangan.
    attempts_per_model = 5
    base_delay = 4.0

    def __init__(self, model: str, api_key: str, temperature: float = 0.9, session=None,
                 fallback_models: list[str] | None = None):
        super().__init__(model, api_key, temperature, session)
        self.fallback_models = [m for m in (fallback_models or []) if m != model]

    def _post(self, payload: dict) -> dict:
        last: Exception | None = None
        for model in [self.model] + self.fallback_models:
            try:
                data = http.post_json(
                    self.URL.format(model=model), payload, headers={"x-goog-api-key": self.api_key},
                    session=self.session, timeout=120, attempts=self.attempts_per_model, base_delay=self.base_delay,
                )
            except RetryableError as e:  # 503/429/timeout terus-menerus
                last = e
            except http.HTTPError as e:
                if e.status != 404:  # 400/401/403 = key/permintaan salah; model lain tidak akan menolong
                    raise
                last = e  # model tidak ada / sudah dipensiunkan
            else:
                if model != self.model:
                    log.warning("Gemini: pindah ke model cadangan %s", model)
                    self.fallback_models = [m for m in self.fallback_models if m != model] + [self.model]
                    self.model = model
                return data
            log.warning("Gemini model %s gagal: %s", model, last)
        raise LLMError(f"semua model Gemini gagal; terakhir: {last}")

    def _call(self, system: str, prompt: str, temperature: float) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        }
        data = self._post(payload)
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "tanpa kandidat")
            raise LLMError(f"Gemini tidak memberi jawaban ({reason})")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise LLMError(f"Gemini menjawab kosong (finishReason={cands[0].get('finishReason')})")
        return text


class ClaudeClient(_BaseClient):
    """Claude lewat SDK resmi `anthropic`.

    Catatan model terbaru: temperature/top_p tidak boleh dikirim (400), thinking selalu adaptif,
    dan kedalaman diatur lewat output_config.effort. Server-side fallback ("default") menjalankan
    ulang permintaan di model lain kalau model utama menolak (stop_reason == "refusal").
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, model: str, api_key: str, effort: str = "medium", client=None):
        super().__init__(model, api_key)
        self.effort = effort
        if client is None:
            import anthropic

            # SDK sendiri sudah retry 408/409/429/5xx + error koneksi dengan backoff
            client = anthropic.Anthropic(api_key=api_key, max_retries=6, timeout=600)
        self.client = client

    def _call(self, system: str, prompt: str, temperature: float) -> str:  # temperature diabaikan
        import anthropic

        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system + "\nReply with valid JSON only, no prose before or after it.",
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort},
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("API key Claude ditolak: cek secret LLM_API_KEY (harus key dari console.anthropic.com)") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"koneksi ke Claude API gagal: {e}") from e
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise LLMError(f"Claude menolak permintaan (kategori: {cat})")
        if resp.stop_reason == "max_tokens":
            raise LLMError("jawaban Claude terpotong (max_tokens)")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if not text:
            raise LLMError(f"Claude menjawab kosong (stop_reason={resp.stop_reason})")
        return text


def get_llm(config: Config) -> LLM:
    key = env("LLM_API_KEY")
    c = config.llm
    if c.provider == "claude":
        return ClaudeClient(c.claude_model, key, effort=c.claude_effort)
    return GeminiClient(c.gemini_model, key, c.temperature, fallback_models=c.gemini_fallback_models)
