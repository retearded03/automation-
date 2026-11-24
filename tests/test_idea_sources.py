import random

import responses

from ytauto import idea_sources as I
from ytauto import topic_picker
from ytauto.history import History

UNUSUAL = """==Science==
{| class="wikitable"
|-
| width="30%" | '''[[Archaeoacoustics]]'''
| width="70%" | Can ancient [[pottery]] be used to play back recorded voices from the distant past?
|-
| {{icon|GA}} '''[[Rjukan|Rjukan sun mirror]]'''
| A town that built giant mirrors to bring sunlight into its valley.
|-
| '''[[Ota Benga]]'''
| The tragic story of a man exhibited in a zoo.
|}"""

DYK = """* ... that '''[[Mari texts|an estimated 20,000 tablets]]''' were unearthed at [[Mari, Syria|Mari]]?
* ... that the '''[[Egg church]]''' ''(pictured)'' looks like an egg?
Some other line"""


def test_parse_unusual_rows():
    seeds = I.parse_unusual(UNUSUAL, "Science")
    assert [s["title"] for s in seeds] == ["Archaeoacoustics", "Rjukan", "Ota Benga"]
    assert seeds[0]["hook"] == "Can ancient pottery be used to play back recorded voices from the distant past?"
    assert seeds[1]["source"] == "wikipedia unusual: Science"


def test_parse_dyk():
    seeds = I.parse_dyk(DYK)
    assert [s["title"] for s in seeds] == ["Mari texts", "Egg church"]
    assert "(pictured)" not in seeds[1]["hook"] and seeds[0]["hook"].startswith("... that an estimated 20,000")


@responses.activate
def test_gather_seeds_filters_unsafe_and_history(cfg, tmp_path):
    def cb(request):
        import json
        page = request.params["page"]
        text = DYK if page == "Template:Did you know" else UNUSUAL
        return 200, {}, json.dumps({"parse": {"wikitext": text}})

    responses.add_callback(responses.GET, I.WIKI_API, callback=cb)
    cfg.ideas.unusual_categories = ["Science"]
    h = History(tmp_path / "h.json")
    h.add({"title": "Can ancient pottery play back recorded voices", "topic": "Archaeoacoustics pottery recorded voices"})
    seeds = I.gather_seeds(cfg, h, random.Random(1))
    titles = {s["title"] for s in seeds}
    assert "Ota Benga" not in titles          # "tragic" -> tidak aman
    assert "Archaeoacoustics" not in titles   # sudah pernah dibahas
    assert {"Rjukan", "Mari texts", "Egg church"} <= titles
    assert I.format_seeds(seeds[:1]).startswith("- ")


def test_topics_ranked_by_score(cfg, fake_llm, tmp_path):
    def topic(title, hook, weird):
        return {"title": title, "angle": f"Reveals {title.lower()}", "wikipedia_queries": [title], "safety": "safe",
                "scores": {"hook": hook, "weird": weird, "explainable": 7}}

    llm = fake_llm({"topics": [topic("How Elevators Work Normally", 3, 2),
                               topic("The Town That Built A Fake Sun", 9, 9),
                               topic("Pottery That Might Record Voices", 8, 9),
                               topic("Why Concrete Gets Stronger Slowly", 5, 4)]})
    seeds = [{"title": "Rjukan", "hook": "giant mirrors", "source": "x"}]
    out = topic_picker.pick_topics(cfg, llm, [], History(tmp_path / "h.json"), 1, "2026-11-24", seeds=seeds)
    assert [t["title"] for t in out] == ["The Town That Built A Fake Sun", "Pottery That Might Record Voices"]
    assert "Rjukan | giant mirrors" in llm.calls[0]["prompt"]
    assert len(llm.calls) == 1  # satu panggilan LLM cukup


def test_unsafe_words_extended():
    assert topic_picker.is_unsafe("The tragic flood disaster")
    assert topic_picker.is_unsafe("Hurricane season facts")
    assert not topic_picker.is_unsafe("The town that built a fake sun")
