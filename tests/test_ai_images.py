"""Tes gambar AI (Cloudflare / Pollinations), filter topik dobel, dan label LLM di Telegram."""
import base64
import io
import json
import re

import pytest
import requests
import responses
from PIL import Image

@pytest.fixture(autouse=True)
def classic_pacing(cfg):
    """Tes ini ditulis untuk tempo lama; kunci nilainya supaya tidak bergantung pada config proyek."""
    cfg.format.target_words = 135
    cfg.format.max_seconds = 58
    cfg.script.max_segments = 8
    cfg.visuals.seconds_per_shot = 3.2
    cfg.visuals.max_shots_per_segment = 3


from ytauto import ai_images, notify, visuals
from ytauto.history import History, overlap

CF = re.compile(r"https://api\.cloudflare\.com/client/v4/accounts/ACC/ai/run/.*")
POLL = re.compile(r"https://image\.pollinations\.ai/prompt/.*")


def jpeg_bytes(w=512, h=896, color=(30, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def cf_env(monkeypatch):
    monkeypatch.setenv("CF_ACCOUNT_ID", "ACC")
    monkeypatch.setenv("CF_API_TOKEN", "TOK")
    monkeypatch.setenv("PIXABAY_API_KEY", "XKEY")
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)


SCRIPT = {"segments": [
    {"text": "a", "scene": "tiny workers beside a giant dam gate", "visual_keywords": ["dam"]},
    {"text": "b", "scene": "", "visual_keywords": ["turbine hall", "water"]},
]}
TIMINGS = {"segments": [{"index": 0, "duration": 6.0}, {"index": 1, "duration": 3.0}]}


@responses.activate
def test_cloudflare_images_per_scene(cfg, cf_env, tmp_path):
    cfg.visuals.mode = "ai_image"
    cfg.visuals.ai_providers = ["cloudflare"]
    sent = []

    def cb(request):
        body = json.loads(request.body)
        sent.append(body["prompt"])
        assert set(body) == {"prompt", "steps"}  # parameter lain ditolak Cloudflare (400)
        assert request.headers["Authorization"] == "Bearer TOK"
        return 200, {}, json.dumps({"success": True, "result": {"image": base64.b64encode(jpeg_bytes()).decode()}})

    responses.add_callback(responses.POST, CF, callback=cb)
    res = visuals.run(cfg, SCRIPT, TIMINGS, tmp_path, used_ids=set(), seed=5)
    shots = [s for seg in res["segments"] for s in seg["shots"]]
    assert len(shots) == 3 and all(s["kind"] == "photo" and s["provider"] == "cloudflare" for s in shots)
    assert res["ai_images"] == 3 and res["clip_ids"] == []
    assert "reference sheet" in sent[0] and cfg.visuals.character in sent[0]  # maskot dibuat dulu
    assert "tiny workers beside a giant dam gate" in sent[1]
    assert cfg.visuals.character in sent[1] and cfg.visuals.background in sent[1]  # maskot & latar seragam
    assert "stylized 3D animation" in sent[1] and "no text" in sent[1]
    assert "turbine hall, water" in sent[3]  # adegan kosong -> pakai kata kunci
    assert res["new_mascot"].endswith("mascot.png")
    assert res["attribution"] == [{"text": "Illustrations generated with AI (cloudflare)", "url": "", "key": "ai:cloudflare"}]
    assert Image.open(shots[0]["path"]).size == (512, 896)


@responses.activate
def test_falls_back_to_pollinations_then_stock(cfg, cf_env, tmp_path, no_sleep):
    cfg.visuals.mode = "ai_image"
    cfg.visuals.ai_providers = ["cloudflare", "pollinations"]
    ref = ai_images.reference_path(cfg)  # maskot sudah ada di repo -> tidak dibuat ulang
    ref.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (512, 896)).save(ref)
    responses.add(responses.POST, CF, status=403, json={"success": False, "errors": [{"message": "quota"}]})
    # Pollinations: shot pertama berhasil (watermark bawah dipotong), lalu terus sibuk
    responses.add(responses.GET, POLL, body=jpeg_bytes(580, 1015), content_type="image/jpeg")
    for _ in range(10):
        responses.add(responses.GET, POLL, status=503, json={"error": "busy"})
    responses.add(responses.GET, visuals.PIXABAY_VIDEO, json={"hits": [
        {"id": i, "pageURL": f"https://pixabay.com/videos/{i}/", "duration": 9, "user": "u", "user_id": 1,
         "videos": {"large": {"url": f"https://cdn.pixabay.com/{i}.mp4", "width": 1080, "height": 1920}}}
        for i in range(1, 6)]})
    responses.add(responses.GET, re.compile(r"https://cdn\.pixabay\.com/.*"), body=b"MP4")
    res = visuals.run(cfg, SCRIPT, TIMINGS, tmp_path, used_ids=set(), seed=1)
    shots = [s for seg in res["segments"] for s in seg["shots"]]
    assert shots[0]["provider"] == "pollinations"
    assert Image.open(shots[0]["path"]).size[1] < 1015  # bagian bawah (watermark) dibuang
    assert {s["provider"] for s in shots[1:]} == {"pixabay"}  # video tetap jadi
    assert sum("cloudflare.com" in c.request.url for c in responses.calls) == 1  # 403 -> tidak dicoba lagi
    assert any(no_sleep)  # menghormati jeda Pollinations


def test_stock_mode_unchanged(cfg):
    assert cfg.visuals.mode == "ai_image"  # default proyek
    cfg.visuals.mode = "stock"
    assert cfg.visuals.mode == "stock"


def test_no_ai_provider_available_uses_stock(cfg, monkeypatch):
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    cfg.visuals.ai_providers = ["cloudflare"]
    assert ai_images.ImageGenerator(cfg).providers == []


def test_build_prompt_varies_angle(cfg):
    p0 = ai_images.build_prompt(cfg, "a dam.", 0)
    p1 = ai_images.build_prompt(cfg, "a dam.", 1)
    assert p0 != p1 and "a dam." in p0 and "9:16" in p0


# ---------------------------------------------------------------- topik dobel
def test_real_duplicate_pair_is_caught(tmp_path):
    a = ("Why Your Phone Boils Water Thousands of Times Every Second How Liquid Cooling Systems Work in Phones "
         "Explains how tiny vapor chambers use phase-change physics to move heat away from mobile processors.")
    b = ("Why Smartphone Liquid Cooling Doesn't Leak Explains how vapor chamber cooling systems inside thin "
         "phones manage thermal spikes without moving parts.")
    assert overlap(a, b) >= 0.45
    h = History(tmp_path / "h.json")
    h.add({"title": "Why Your Phone Boils Water Thousands of Times Every Second",
           "topic": "How Liquid Cooling Systems Work in Phones Explains how tiny vapor chambers use phase-change "
                    "physics to move heat away from mobile processors."})
    assert h.is_duplicate(b)
    assert "(topic:" in h.recent_topics()[0]  # LLM juga melihat sudut bahasan, bukan hanya judul


def test_related_but_different_topics_not_flagged():
    chips = "The Secret Microscopic Graffiti Inside Computer Chips Why do chip designers etch cartoons onto silicon layers?"
    etch = "How Light Etches Billions of Microscopic Transistors photolithography prints transistors on silicon"
    assert overlap(chips, etch) < 0.45


# ---------------------------------------------------------------- label LLM
def test_summary_shows_llm_and_ai_images():
    text = notify.format_summary([{"index": 0, "status": "dry_run", "title": "T", "duration": 40,
                                   "llm": "centai:hy4-flash:free", "ai_images": 7, "shots": 9}], "r", True)
    assert "LLM: centai:hy4-flash:free" in text and "gambar AI 7/9 shot" in text


@responses.activate
def test_chain_records_which_provider_answered(cfg, monkeypatch, no_sleep):
    from ytauto.llm import get_llm, llm_label
    monkeypatch.setenv("LLM_API_KEY", "G")
    monkeypatch.setenv("CENTAI_API_KEY", "C")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    cfg.llm.gemini_rounds = 1
    gem = re.compile(r"https://generativelanguage\.googleapis\.com/.*")
    responses.add(responses.POST, gem, status=503)
    responses.add(responses.POST, "https://api.cent-ai.dev/v1/chat/completions",
                  json={"choices": [{"message": {"content": "{}"}}]})
    llm = get_llm(cfg)
    llm.complete_json("s", "p")
    assert llm_label(llm) == "centai:hy4-flash:free"



# ---------------------------------------------------------------- Gemini Image + maskot
GEM = "https://generativelanguage.googleapis.com/v1beta"


@responses.activate
def test_gemini_image_uses_reference_and_discovers_model(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "GK")
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    cfg.visuals.mode = "ai_image"
    cfg.visuals.ai_providers = ["gemini_image"]
    cfg.visuals.gemini_image_models = ["old-image-model"]
    responses.add(responses.GET, f"{GEM}/models", json={"models": [
        {"name": "models/gemini-9-flash-image", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-9-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/imagen-9", "supportedGenerationMethods": ["predict"]}]})
    bodies = []

    def gen_cb(request):
        bodies.append(json.loads(request.body))
        img = base64.b64encode(jpeg_bytes()).decode()
        return 200, {}, json.dumps({"candidates": [{"content": {"parts": [
            {"text": "here you go"}, {"inlineData": {"mimeType": "image/jpeg", "data": img}}]}}]})

    responses.add_callback(responses.POST, f"{GEM}/models/gemini-9-flash-image:generateContent", callback=gen_cb)
    res = visuals.run(cfg, SCRIPT, TIMINGS, tmp_path, used_ids=set(), seed=2)
    assert res["ai_images"] == 3
    # panggilan 1 = buat maskot (tanpa referensi); berikutnya selalu mengirim gambar maskot
    assert len(bodies[0]["contents"][0]["parts"]) == 1
    for b in bodies[1:]:
        parts = b["contents"][0]["parts"]
        assert "inlineData" in parts[0] and parts[1]["text"].startswith(cfg.visuals.ai_style)
        assert "mascot character from the reference image" in parts[1]["text"]
    assert bodies[1]["generationConfig"]["responseModalities"] == ["IMAGE"]


def test_adopt_reference_copies_once(cfg, tmp_path):
    run = tmp_path / "work" / "r1" / "artifacts" / "result-0"
    run.mkdir(parents=True)
    Image.new("RGB", (300, 300)).save(run / "mascot.png")
    assert ai_images.adopt_reference(cfg, tmp_path / "work" / "r1") == ai_images.reference_path(cfg)
    assert ai_images.reference_path(cfg).exists()
    assert ai_images.adopt_reference(cfg, tmp_path / "work" / "r1") is None  # sudah ada -> tidak ditimpa


@responses.activate
def test_gemini_tries_next_image_model_on_quota(cfg, monkeypatch, tmp_path, no_sleep):
    """Kasus nyata run 45345541557: gemini-2.5-flash-image 429, model gambar lain harus dicoba."""
    monkeypatch.setenv("GEMINI_API_KEY", "GK")
    cfg.visuals.gemini_image_models = ["gemini-2.5-flash-image"]
    responses.add(responses.GET, f"{GEM}/models", json={"models": [
        {"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
        for m in ("gemini-2.5-flash-image", "gemini-3.1-flash-image")]})
    for _ in range(2):
        responses.add(responses.POST, f"{GEM}/models/gemini-2.5-flash-image:generateContent", status=429)
    img = base64.b64encode(jpeg_bytes()).decode()
    responses.add(responses.POST, f"{GEM}/models/gemini-3.1-flash-image:generateContent",
                  json={"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/jpeg", "data": img}}]}}]})
    responses.add(responses.POST, f"{GEM}/models/gemini-3.1-flash-image:generateContent",
                  json={"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/jpeg", "data": img}}]}}]})
    g = ai_images.GeminiImage(cfg)
    g.generate("p", tmp_path / "a.jpg", 1)
    g.generate("p", tmp_path / "b.jpg", 1)
    calls_25 = sum("2.5-flash-image" in c.request.url and c.request.method == "POST" for c in responses.calls)
    assert calls_25 == 2  # model yang kuotanya habis tidak dicoba lagi di gambar kedua
    assert (tmp_path / "b.jpg").exists()


@responses.activate
def test_gemini_all_models_exhausted_fails_fast(cfg, monkeypatch, tmp_path, no_sleep):
    monkeypatch.setenv("GEMINI_API_KEY", "GK")
    cfg.visuals.gemini_image_models = ["m1"]
    responses.add(responses.GET, f"{GEM}/models", json={"models": []})
    for _ in range(2):
        responses.add(responses.POST, f"{GEM}/models/m1:generateContent", status=429)
    g = ai_images.GeminiImage(cfg)
    with pytest.raises(ai_images.ImageGenError):
        g.generate("p", tmp_path / "a.jpg", 1)
    n = len(responses.calls)
    with pytest.raises(ai_images.ImageGenError, match="kuota"):
        g.generate("p", tmp_path / "b.jpg", 1)
    assert len(responses.calls) == n  # tidak membuang waktu memanggil API lagi
