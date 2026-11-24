import json
import random
import re
from urllib.parse import parse_qs, urlparse

import pytest
import responses

@pytest.fixture(autouse=True)
def classic_pacing(cfg):
    """Tes ini ditulis untuk tempo lama; kunci nilainya supaya tidak bergantung pada config proyek."""
    cfg.format.target_words = 135
    cfg.format.max_seconds = 58
    cfg.script.max_segments = 8
    cfg.visuals.seconds_per_shot = 3.2
    cfg.visuals.max_shots_per_segment = 3


from ytauto import visuals


def pexels_video(i, h=1920, w=1080, dur=10):
    return {"id": i, "duration": dur, "url": f"https://www.pexels.com/video/{i}/",
            "user": {"name": f"Author{i}", "url": f"https://www.pexels.com/@a{i}"},
            "video_files": [
                {"file_type": "video/mp4", "width": w // 2, "height": h // 2, "link": f"https://cdn.pexels.com/{i}_sd.mp4"},
                {"file_type": "video/mp4", "width": w, "height": h, "link": f"https://cdn.pexels.com/{i}_hd.mp4"},
                {"file_type": "video/mp4", "width": w * 2, "height": h * 2, "link": f"https://cdn.pexels.com/{i}_4k.mp4"},
            ]}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "PKEY")
    monkeypatch.setenv("PIXABAY_API_KEY", "XKEY")


def test_pexels_picks_smallest_file_at_target_resolution():
    with responses.RequestsMock() as rs:
        rs.get(visuals.PEXELS_VIDEO, json={"videos": [pexels_video(1)]})
        c = visuals.pexels_videos("oil rig", "PKEY", True, 1920)[0]
        assert c.url.endswith("1_hd.mp4")
        assert rs.calls[0].request.headers["Authorization"] == "PKEY"
        assert "orientation=portrait" in rs.calls[0].request.url


def test_choose_avoids_used_and_prefers_long_enough():
    rng = random.Random(0)
    mk = lambda i, d: visuals.Candidate("pexels", str(i), "video", "u", 1080, 1920, d, "p", "a", "au")
    cands = [mk(1, 10), mk(2, 1), mk(3, 8)]
    for _ in range(20):
        c = visuals.choose(cands, {"pexels:1"}, 3.0, rng)
        assert c.id == "3"
    assert visuals.choose(cands, {"pexels:1", "pexels:2", "pexels:3"}, 3.0, rng) is None


@responses.activate
def test_run_downloads_and_attributes(cfg, keys, tmp_path):
    counter = {"n": 100}

    def pexels_cb(request):
        q = parse_qs(urlparse(request.url).query)["query"][0]
        if q == "nothing here":
            return 200, {}, json.dumps({"videos": []})
        counter["n"] += 10
        base = counter["n"]
        return 200, {}, json.dumps({"videos": [pexels_video(base + k) for k in range(4)]})

    responses.add_callback(responses.GET, visuals.PEXELS_VIDEO, callback=pexels_cb)
    responses.add(responses.GET, visuals.PIXABAY_VIDEO, body=__import__("requests").ConnectionError("down"))
    responses.add(responses.GET, re.compile(r"https://cdn\.pexels\.com/.*"), body=b"MP4DATA")

    script = {"segments": [
        {"text": "a", "visual_keywords": ["nothing here", "oil rig"]},
        {"text": "b", "visual_keywords": ["ship"]},
    ]}
    timings = {"segments": [{"index": 0, "duration": 7.0}, {"index": 1, "duration": 2.0}]}
    res = visuals.run(cfg, script, timings, tmp_path, used_ids={"pexels:111"}, seed=1)

    shots0 = res["segments"][0]["shots"]
    assert len(shots0) == 3  # 7 detik / 3.2 -> 3 shot
    assert len(res["segments"][1]["shots"]) == 1
    keys_used = [s["key"] for seg in res["segments"] for s in seg["shots"]]
    assert len(keys_used) == len(set(keys_used))  # tidak ada klip dobel dalam satu video
    assert "pexels:111" not in keys_used           # klip dari history tidak dipakai lagi
    assert all(tmp_path.joinpath("clips").glob("*.mp4"))
    assert res["attribution"][0]["text"].startswith("Video by Author")
    assert res["attribution"][0]["url"].startswith("https://www.pexels.com/video/")
    assert (tmp_path / "visuals.json").exists()


@responses.activate
def test_fallback_to_pixabay_then_photo(cfg, keys, tmp_path):
    responses.add(responses.GET, visuals.PEXELS_VIDEO, json={"videos": []})
    responses.add(responses.GET, visuals.PIXABAY_VIDEO, json={"hits": [{
        "id": 77, "pageURL": "https://pixabay.com/videos/id-77/", "duration": 12, "user": "bob", "user_id": 5,
        "videos": {"medium": {"url": "https://cdn.pixabay.com/77.mp4", "width": 1920, "height": 1080}}}]})
    f = visuals.Finder(cfg, set(), random.Random(0))
    c = f.find(["dam"], 3)
    assert c.provider == "pixabay" and c.author_url.endswith("bob-5/")
    # klip pixabay sudah terpakai -> jatuh ke foto pexels
    responses.add(responses.GET, visuals.PEXELS_PHOTO, json={"photos": [{
        "id": 9, "url": "https://www.pexels.com/photo/9/", "photographer": "Ann", "photographer_url": "x",
        "src": {"large2x": "https://images.pexels.com/9.jpg"}}]})
    c2 = f.find(["dam"], 3)
    assert c2.kind == "photo" and c2.key == "pexels:photo9"


@responses.activate
def test_no_footage_raises(cfg, keys):
    responses.add(responses.GET, visuals.PEXELS_VIDEO, json={"videos": []})
    responses.add(responses.GET, visuals.PIXABAY_VIDEO, json={"hits": []})
    responses.add(responses.GET, visuals.PEXELS_PHOTO, json={"photos": []})
    responses.add(responses.GET, visuals.PIXABAY_IMAGE, json={"hits": []})
    with pytest.raises(visuals.VisualsError):
        visuals.Finder(cfg, set(), random.Random(0)).find(["zzz"], 3)


@pytest.fixture
def pixabay_only(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.setenv("PIXABAY_API_KEY", "XKEY")


@responses.activate
def test_pixabay_only_videos_then_photos(cfg, pixabay_only):
    responses.add(responses.GET, visuals.PIXABAY_VIDEO, json={"hits": [{
        "id": 5, "pageURL": "https://pixabay.com/videos/id-5/", "duration": 9, "user": "eve", "user_id": 2,
        "videos": {"large": {"url": "https://cdn.pixabay.com/5.mp4", "width": 1920, "height": 1080}}}]})
    responses.add(responses.GET, visuals.PIXABAY_IMAGE, json={"hits": [{
        "id": 8, "pageURL": "https://pixabay.com/photos/id-8/", "user": "max", "user_id": 3,
        "largeImageURL": "https://cdn.pixabay.com/8.jpg", "imageWidth": 1080, "imageHeight": 1920}]})
    f = visuals.Finder(cfg, set(), random.Random(0))
    assert f.find(["crane"], 3).key == "pixabay:5"
    photo = f.find(["crane"], 3)  # video satu-satunya sudah terpakai -> foto Pixabay
    assert photo.kind == "photo" and photo.key == "pixabay:photo8"
    assert all("api.pexels.com" not in c.request.url for c in responses.calls)
    img_call = [c for c in responses.calls if c.request.url.startswith(visuals.PIXABAY_IMAGE + "?")][0]
    assert "orientation=vertical" in img_call.request.url


def test_requires_at_least_one_key(cfg, monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    with pytest.raises(visuals.VisualsError, match="PIXABAY_API_KEY"):
        visuals.Finder(cfg, set(), random.Random(0))
