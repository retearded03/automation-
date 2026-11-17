"""Antarmuka LLM tunggal dengan dua provider: Gemini (default, ada free tier) dan Claude."""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template
from typing import Any, Protocol

from . import http
from .config import Config, env

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

    def _call(self, system: str, prompt: str, temperature: float) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        }
        data = http.post_json(
            self.URL.format(model=self.model),
            payload,
            headers={"x-goog-api-key": self.api_key},
            session=self.session,
            timeout=120,
        )
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
    URL = "https://api.anthropic.com/v1/messages"

    def _call(self, system: str, prompt: str, temperature: float) -> str:
        payload = {
            "model": self.model,
            "max_tokens": 8000,
            "system": system + "\nAlways reply with valid JSON only, no prose.",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": min(temperature, 1.0),
        }
        data = http.post_json(
            self.URL,
            payload,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            session=self.session,
            timeout=180,
        )
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if not text:
            raise LLMError(f"Claude menjawab kosong (stop_reason={data.get('stop_reason')})")
        return text


def get_llm(config: Config) -> LLM:
    key = env("LLM_API_KEY")
    c = config.llm
    if c.provider == "claude":
        return ClaudeClient(c.claude_model, key, c.temperature)
    return GeminiClient(c.gemini_model, key, c.temperature)
