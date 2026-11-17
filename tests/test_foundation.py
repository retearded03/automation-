"""Tes modul fondasi: config, retry, http, history, llm."""
import json

import pytest
import requests
import responses

from ytauto import http, retry
from ytauto.config import ConfigError, env, load_config
from ytauto.history import History, similarity, style_key
from ytauto.llm import ClaudeClient, GeminiClient, LLMError, extract_json, render_prompt


# ---------- config ----------
def test_load_project_config(cfg):
    assert cfg.channel.name
    assert cfg.schedule.videos_per_day == 5
    assert len(cfg.schedule.publish_slots) >= cfg.schedule.videos_per_day
    assert cfg.format.vertical
    assert cfg.upload.made_for_kids is False


def test_invalid_slot_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("schedule:\n  publish_slots: ['25:00']\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_defaults_when_sections_missing(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("channel:\n  name: X\n", encoding="utf-8")
    c = load_config(p)
    assert c.upload.privacy_status == "private"  # default spesifikasi
    assert c.base_dir == tmp_path


def test_env_missing(monkeypatch):
    monkeypatch.delenv("NOPE_SECRET", raising=False)
    with pytest.raises(ConfigError, match="NOPE_SECRET"):
        env("NOPE_SECRET")
    assert env("NOPE_SECRET", required=False) is None


# ---------- retry ----------
def test_retry_then_success(no_sleep):
    calls = {"n": 0}

    @retry.retry(attempts=4)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise retry.RetryableError("sementara")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3
    assert len(no_sleep) == 2
    assert no_sleep[1] >= no_sleep[0] * 0.5  # backoff naik (dengan jitter)


def test_retry_gives_up():
    @retry.retry(attempts=3)
    def always():
        raise retry.RetryableError("x")

    with pytest.raises(retry.RetryableError):
        always()


def test_retry_does_not_catch_other_errors():
    calls = {"n": 0}

    @retry.retry(attempts=3)
    def boom():
        calls["n"] += 1
        raise ValueError("bukan sementara")

    with pytest.raises(ValueError):
        boom()
    assert calls["n"] == 1


# ---------- http ----------
@responses.activate
def test_http_retries_429_with_retry_after(no_sleep):
    url = "https://api.example.com/x?key=SECRET"
    responses.get(url, status=429, headers={"Retry-After": "7"})
    responses.get(url, json={"ok": True})
    assert http.get_json(url) == {"ok": True}
    assert no_sleep == [7.0]


@responses.activate
def test_http_4xx_not_retried_and_key_redacted():
    url = "https://api.example.com/x?key=SECRET"
    responses.get(url, status=403, body="forbidden")
    with pytest.raises(http.HTTPError) as ei:
        http.get_json(url)
    assert "SECRET" not in str(ei.value)
    assert len(responses.calls) == 1


@responses.activate
def test_http_connection_error_retried():
    url = "https://api.example.com/y"
    responses.get(url, body=requests.ConnectionError("down"))
    responses.get(url, json=[1])
    assert http.get_json(url) == [1]


@responses.activate
def test_download_limit(tmp_path):
    responses.get("https://cdn.example.com/a.mp4", body=b"x" * 1000)
    with pytest.raises(http.HTTPError):
        http.download("https://cdn.example.com/a.mp4", tmp_path / "a.mp4", max_bytes=100)
    assert not (tmp_path / "a.mp4").exists()
    responses.get("https://cdn.example.com/b.mp4", body=b"y" * 50)
    out = http.download("https://cdn.example.com/b.mp4", tmp_path / "b.mp4", max_bytes=100)
    assert out.read_bytes() == b"y" * 50


# ---------- history ----------
def test_history_duplicate_and_clips(tmp_path):
    h = History(tmp_path / "h.json")
    h.add({"topic": "How do offshore oil rigs drill under the sea", "title": "Drilling Oil Under the Ocean",
           "clip_ids": ["pexels:1"], "style": {"hook_type": "a", "structure": "b"}})
    h.save()
    h2 = History.load(tmp_path / "h.json")
    assert h2.is_duplicate("How offshore oil rigs drill oil under the sea")
    assert not h2.is_duplicate("Why airplane wings are curved")
    assert "pexels:1" in h2.used_clip_ids()
    assert style_key({"hook_type": "a", "structure": "b"}) in h2.recent_style_keys(5)


def test_similarity_bounds():
    assert similarity("", "abc") == 0
    assert similarity("bridges cables steel", "steel cables bridges") == 1


# ---------- llm ----------
def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": [1, 2]} hope it helps') == {"a": [1, 2]}
    with pytest.raises(LLMError):
        extract_json("no json here")


@responses.activate
def test_gemini_client():
    responses.post(
        "https://generativelanguage.googleapis.com/v1beta/models/m1:generateContent",
        json={"candidates": [{"content": {"parts": [{"text": '{"x": 1}'}]}}]},
    )
    c = GeminiClient("m1", "KEY")
    assert c.complete_json("sys", "prompt") == {"x": 1}
    req = responses.calls[0].request
    assert req.headers["x-goog-api-key"] == "KEY"
    body = json.loads(req.body)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"


@responses.activate
def test_gemini_blocked():
    responses.post(
        "https://generativelanguage.googleapis.com/v1beta/models/m1:generateContent",
        json={"promptFeedback": {"blockReason": "SAFETY"}},
    )
    with pytest.raises(LLMError, match="SAFETY"):
        GeminiClient("m1", "KEY").complete_json("s", "p")


@responses.activate
def test_claude_client_retries_invalid_json():
    url = "https://api.anthropic.com/v1/messages"
    responses.post(url, json={"content": [{"type": "text", "text": "oops not json"}]})
    responses.post(url, json={"content": [{"type": "text", "text": '[{"ok": true}]'}]})
    c = ClaudeClient("claude-x", "KEY")
    assert c.complete_json("s", "p") == [{"ok": True}]
    assert responses.calls[0].request.headers["anthropic-version"] == "2023-06-01"
    assert "not valid JSON" in json.loads(responses.calls[1].request.body)["messages"][0]["content"]


def test_all_prompts_render():
    common = dict(niche="n", audience="a", language="en")
    render_prompt("topic_picker", **common, count=3, trends="[]", history="[]", today="2026-01-01")
    render_prompt("script_writer", **common, topic="{}", style="{}", target_words=130, min_segments=4,
                  max_segments=8, sources="S1", feedback="")
    render_prompt("fact_check", script="{}", sources="S1")
    render_prompt("quality_gate", niche="n", script="{}", recent_titles="[]")
