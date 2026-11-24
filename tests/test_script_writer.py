import json
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


from ytauto import script_writer as sw

SOURCE_TEXT = (
    "An offshore drilling rig is a large structure used to drill wells beneath the seabed. "
    "Some rigs operate in water depths of more than 3,000 metres. "
    "Semi-submersible platforms float on pontoons that are ballasted with seawater. "
    "Dynamic positioning uses thrusters to keep a drillship above the well. "
    "A blowout preventer is a large valve that seals the well in an emergency. "
    "The drill string is made of steel pipes screwed together, each about 9 metres long. "
) * 3 + "\n== See also ==\nList of oil rigs"

TOPIC = {"title": "How Oil Is Drilled in the Middle of the Ocean", "angle": "How rigs drill kilometres below waves",
         "core_fact_idea": "3,000 metres of water", "wikipedia_queries": ["offshore drilling rig"]}
STYLE = {"hook_type": "surprising number", "structure": "step-by-step process"}


def wiki_callback(request):
    q = parse_qs(urlparse(request.url).query)
    if q.get("list") == ["search"]:
        return 200, {}, json.dumps({"query": {"search": [{"title": "Offshore drilling"}, {"title": "Oil platform"}]}})
    title = q["titles"][0]
    if title == "Oil platform":
        return 200, {}, json.dumps({"query": {"pages": [{"title": title, "extract": "Oil platform may refer to: x"}]}})
    return 200, {}, json.dumps({"query": {"pages": [{"title": title, "extract": SOURCE_TEXT,
                                                     "fullurl": "https://en.wikipedia.org/wiki/Offshore_drilling"}]}})


def mock_wiki():
    responses.add_callback(responses.GET, sw.WIKI_API, callback=wiki_callback)


def draft(n_words_per_seg=22, bad_claim=False):
    filler = " ".join(["really"] * (n_words_per_seg - 8))
    segs = [
        {"text": f"Some rigs drill in water deeper than three kilometres {filler}", "claims": ["C1"], "visual_keywords": ["oil rig ocean"]},
        {"text": f"Semi submersible platforms float on pontoons full of seawater {filler}", "claims": ["C2"], "visual_keywords": ["offshore platform"]},
        {"text": f"Thrusters hold the drillship right above the well {filler}", "claims": ["C3"], "visual_keywords": ["ship thrusters"]},
        {"text": f"A giant valve can seal the whole well in seconds {filler}", "claims": ["C4"], "visual_keywords": ["valve"]},
        {"text": f"Each steel pipe is about nine metres long and they screw together {filler}", "claims": ["C5"], "visual_keywords": ["steel pipes"]},
        {"text": f"Next time you fill up your car think about that pipe {filler}", "claims": [], "visual_keywords": ["gas station"]},
    ]
    claims = [
        {"id": "C1", "text": "deep water", "source": "S1", "evidence": "Some rigs operate in water depths of more than 3,000 metres."},
        {"id": "C2", "text": "pontoons", "source": "S1", "evidence": "Semi-submersible platforms float on pontoons that are ballasted with seawater."},
        {"id": "C3", "text": "dp", "source": "S1", "evidence": "Dynamic positioning uses thrusters to keep a drillship above the well."},
        {"id": "C4", "text": "bop", "source": "S1",
         "evidence": "The valve was invented in 1922 by a Texan." if bad_claim else "A blowout preventer is a large valve that seals the well in an emergency."},
        {"id": "C5", "text": "pipes", "source": "S1", "evidence": "The drill string is made of steel pipes screwed together, each about 9 metres long."},
    ]
    return {"title": "How Do You Drill Oil 3 km Under the Sea?", "thumbnail_text": "3 KM UNDER WATER", "segments": segs,
            "claims": claims, "description": "How offshore rigs work.", "tags": ["oil rig", "engineering"],
            "hashtags": ["#shorts", "#engineering"]}


def all_supported(system, prompt):
    return {"claims": [{"id": f"C{i}", "supported": True} for i in range(1, 6)],
            "segments": [{"index": i, "uncited_facts": False} for i in range(6)]}


GOOD_SCORE = {"hook": 8, "clarity": 8, "value": 8, "originality": 8, "flow": 8, "overall": 8}


@responses.activate
def test_gather_sources_skips_disambiguation_and_cuts_sections(cfg):
    mock_wiki()
    sources = sw.gather_sources(cfg, TOPIC)
    assert [s["id"] for s in sources] == ["S1"]
    assert "See also" not in sources[0]["text"]
    assert sources[0]["url"].startswith("https://en.wikipedia.org/")


@responses.activate
def test_gather_sources_fails_without_material(cfg):
    responses.add(responses.GET, sw.WIKI_API, json={"query": {"search": []}})
    with pytest.raises(sw.ResearchError):
        sw.gather_sources(cfg, TOPIC)


def test_evidence_found():
    assert sw.evidence_found("Some rigs operate in water depths of more than 3,000 metres.", SOURCE_TEXT)
    assert sw.evidence_found("some rigs operate in water depths of more than 3,000 metres", SOURCE_TEXT)  # beda kapital/tanda baca
    assert not sw.evidence_found("The valve was invented in 1922 by a Texan.", SOURCE_TEXT)
    assert not sw.evidence_found("short", SOURCE_TEXT)


@responses.activate
def test_produce_script_happy_path(cfg, fake_llm):
    mock_wiki()
    llm = fake_llm(draft(), all_supported, GOOD_SCORE)
    s = sw.produce_script(cfg, llm, TOPIC, STYLE, recent_titles=[])
    assert len(s["segments"]) == 6
    assert s["sources"][0]["title"] == "Offshore drilling"
    assert s["quality"]["overall"] == 8
    assert s["fact_check"]["removed_segments"] == []
    # sumber & gaya benar-benar dikirim ke penulis naskah
    assert "[S1] Offshore drilling" in llm.calls[0]["prompt"]
    assert "step-by-step process" in llm.calls[0]["prompt"]
    # tiap panggilan diberi nama langkah supaya bisa diarahkan ke model berbeda
    assert [c["step"] for c in llm.calls] == ["script_writer", "fact_check", "quality_gate"]


@responses.activate
def test_unsupported_claim_segment_removed(cfg, fake_llm):
    mock_wiki()
    llm = fake_llm(draft(bad_claim=True), all_supported, GOOD_SCORE)
    s = sw.produce_script(cfg, llm, TOPIC, STYLE, recent_titles=[])
    texts = " ".join(seg["text"] for seg in s["segments"])
    assert "giant valve" not in texts  # kutipan palsu -> segmen dibuang walau LLM bilang supported
    assert "C4" in s["fact_check"]["unsupported_claims"]
    assert all(c["id"] != "C4" for c in s["claims"])


@responses.activate
def test_llm_flags_uncited_fact(cfg, fake_llm):
    mock_wiki()

    def flag_seg2(system, prompt):
        v = all_supported(system, prompt)
        v["segments"][2]["uncited_facts"] = True
        return v

    llm = fake_llm(draft(), flag_seg2, GOOD_SCORE)
    s = sw.produce_script(cfg, llm, TOPIC, STYLE, recent_titles=[])
    assert len(s["segments"]) == 5
    assert "Thrusters" not in " ".join(x["text"] for x in s["segments"])


@responses.activate
def test_hook_failure_triggers_rewrite_then_succeeds(cfg, fake_llm):
    mock_wiki()

    def reject_c1(system, prompt):
        v = all_supported(system, prompt)
        v["claims"][0]["supported"] = False
        return v

    llm = fake_llm(draft(), reject_c1, draft(), all_supported, GOOD_SCORE)
    s = sw.produce_script(cfg, llm, TOPIC, STYLE, recent_titles=[])
    assert len(s["segments"]) == 6
    assert "failed fact-checking" in llm.calls[2]["prompt"]


@responses.activate
def test_quality_gate_rejects(cfg, fake_llm):
    mock_wiki()
    bad = {"overall": 4, "main_weakness": "generic"}  # di bawah batas bawah (5) -> tetap ditolak
    llm = fake_llm(*[draft(), all_supported, bad] * 3)
    with pytest.raises(sw.QualityError):
        sw.produce_script(cfg, llm, TOPIC, STYLE, recent_titles=["Old title"])
    assert "punchy, surprising hook" in llm.calls[3]["prompt"]  # umpan balik untuk menulis ulang


@responses.activate
def test_best_draft_used_when_close_to_target(cfg, fake_llm):
    """Kasus nyata: semua draf di bawah 7, tapi draf terbaik (6) tetap dipakai dengan peringatan."""
    mock_wiki()
    llm = fake_llm(draft(), all_supported, {"overall": 5, "main_weakness": "slow"},
                   draft(), all_supported, {"overall": 6, "main_weakness": "ok-ish"},
                   draft(), all_supported, {"overall": 3, "main_weakness": "dry"})
    s = sw.produce_script(cfg, llm, TOPIC, STYLE, recent_titles=[])
    assert s["quality"]["overall"] == 6
    assert "6.0" in s["quality_warning"] and s["sources"]


def test_hook_rule_in_prompt():
    from ytauto.llm import render_prompt
    p = render_prompt("script_writer", niche="n", audience="a", language="en", topic="{}", style="{}",
                      target_words="130", min_segments="4", max_segments="8", sources="S1", feedback="")
    assert "under 15 words" in p and "Picture yourself" in p


@responses.activate
def test_length_out_of_range_rewrites(cfg, fake_llm):
    mock_wiki()
    llm = fake_llm(draft(n_words_per_seg=60), draft(), all_supported, GOOD_SCORE)
    s = sw.produce_script(cfg, llm, TOPIC, STYLE, recent_titles=[])
    assert "words" in llm.calls[1]["prompt"]
    assert s["word_count"] < 200


def test_normalize_cleans_narration(cfg):
    raw = {"title": "T", "segments": [{"text": "**Wow** 🚀 this is big", "visual_keywords": []}], "claims": []}
    s = sw.normalize_script(raw, cfg)
    assert s["segments"][0]["text"] == "Wow this is big."
    assert s["segments"][0]["visual_keywords"]  # fallback keyword dari config
