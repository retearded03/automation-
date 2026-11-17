import json

import pytest

from ytauto import voice


class FakeCommunicate:
    """Pengganti edge_tts.Communicate: 1 detik audio per 6000 byte (48 kbps), 0.4 detik per kata."""

    instances = []
    fail_first = 0

    def __init__(self, text, voice_name, rate="+0%", boundary="SentenceBoundary"):
        self.text, self.voice, self.rate, self.boundary = text, voice_name, rate, boundary
        FakeCommunicate.instances.append(self)

    async def stream(self):
        if FakeCommunicate.fail_first > 0:
            FakeCommunicate.fail_first -= 1
            raise ConnectionError("edge down")
        words = self.text.split()
        speed = 1 + int(self.rate.strip("%+")) / 100
        per_word = 0.4 / speed
        for i, w in enumerate(words):
            yield {"type": "WordBoundary", "offset": int(i * per_word * 1e7), "duration": int(per_word * 0.9 * 1e7),
                   "text": w.strip(".,!?")}
        yield {"type": "audio", "data": b"\x00" * int(6000 * per_word * len(words))}


@pytest.fixture(autouse=True)
def fake_tts(monkeypatch):
    FakeCommunicate.instances = []
    FakeCommunicate.fail_first = 0
    monkeypatch.setattr(voice.edge_tts, "Communicate", FakeCommunicate)


SCRIPT = {"segments": [
    {"text": "Some rigs drill in water three kilometres deep. That is wild."},
    {"text": "They float on giant pontoons filled with seawater."},
    {"text": "Thrusters keep the ship exactly above the well."},
]}


def test_timings_and_audio(cfg, tmp_path):
    cfg.voice.base_rate_percent = 0
    cfg.format.min_seconds = 1
    t = voice.run(cfg, SCRIPT, "en-US-AndrewNeural", tmp_path)
    assert (tmp_path / "voice.mp3").stat().st_size > 0
    saved = json.loads((tmp_path / "timings.json").read_text())
    assert saved["total"] == t["total"]
    segs = t["segments"]
    assert segs[0]["start"] == 0 and segs[1]["start"] == segs[0]["end"]
    assert abs(segs[0]["duration"] - 11 * 0.4) < 0.01
    # kata pada segmen kedua digeser oleh durasi segmen pertama
    assert segs[1]["words"][0]["start"] == pytest.approx(segs[0]["end"], abs=0.01)
    # dua kalimat pada segmen pertama
    assert [s["text"] for s in segs[0]["sentences"]] == ["Some rigs drill in water three kilometres deep.", "That is wild."]
    assert segs[0]["sentences"][1]["start"] > segs[0]["sentences"][0]["end"] - 0.01
    assert FakeCommunicate.instances[0].boundary == "WordBoundary"


def test_speeds_up_when_too_long(cfg, tmp_path):
    cfg.voice.base_rate_percent = 0
    cfg.format.max_seconds = 11.5  # 28 kata x 0.4 = 11.2 detik -> muat
    cfg.format.min_seconds = 1
    long_script = {"segments": SCRIPT["segments"] + [{"text": "One more line here."}]}  # 32 kata = 12.8 detik
    t = voice.run(cfg, long_script, "v", tmp_path)
    assert t["total"] <= 11.5
    assert t["rate"] == "+12%"


def test_too_long_even_at_max_rate(cfg, tmp_path):
    cfg.voice.base_rate_percent = 0
    cfg.voice.max_rate_percent = 6
    cfg.format.max_seconds = 5
    with pytest.raises(voice.VoiceError, match="melebihi"):
        voice.run(cfg, SCRIPT, "v", tmp_path)


def test_retry_on_network_error(cfg, tmp_path):
    cfg.format.min_seconds = 1
    FakeCommunicate.fail_first = 2
    t = voice.run(cfg, SCRIPT, "v", tmp_path)
    assert t["total"] > 0


def test_sentence_timings_without_words():
    out = voice.sentence_timings("One two. Three four five six.", [], 10.0, 16.0)
    assert out[0] == {"text": "One two.", "start": 10.0, "end": 12.0}
    assert out[1]["end"] == 16.0
