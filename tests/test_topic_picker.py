from datetime import datetime, timezone

import pytest

from ytauto import scheduling, topic_picker
from ytauto.history import History

NOW = datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)  # 02:00 di New York


def topic(title, angle=None, safety="safe"):
    return {
        "title": title,
        "angle": angle or f"Explains {title.lower()} with a surprising number",
        "core_fact_idea": "x",
        "inspired_by": ["vidCar1"],
        "wikipedia_queries": [title],
        "visual_theme": "machines",
        "safety": safety,
    }


TRENDS = [
    {"id": "vidCar1", "title": "EV pickup crash test", "tags": ["ev"], "category_id": "2", "views_per_hour": 10000},
    {"id": "vidWar", "title": "War footage from the front", "tags": [], "category_id": "22", "views_per_hour": 9000},
]


def test_prefilter_removes_unsafe():
    out = topic_picker.prefilter_trends(TRENDS, 10)
    assert [t["id"] for t in out] == ["vidCar1"]


def test_validate_topics_filters_duplicates_unsafe_incomplete(tmp_path):
    h = History(tmp_path / "h.json")
    h.add({"title": "How Airbags Inflate So Fast", "topic": "how airbags inflate in milliseconds"})
    raw = [
        topic("How Airbags Inflate So Fast"),                       # duplikat history
        topic("Why Bridges Sway in the Wind"),
        topic("Why Bridges Sway in Wind"),                          # duplikat dalam batch
        topic("The election machine explained"),                    # politik
        topic("Concrete that heals itself", safety="unsafe"),       # ditandai LLM
        {"title": "No queries", "angle": "x", "wikipedia_queries": []},
        topic("How Escalators Never Run Out of Steps"),
    ]
    out = topic_picker.validate_topics(raw, h)
    assert [t["title"] for t in out] == ["Why Bridges Sway in the Wind", "How Escalators Never Run Out of Steps"]


def test_build_plan_assigns_backups_styles_and_slots(cfg, fake_llm, tmp_path):
    titles = ["How Dams Hold Back Lakes", "Why Planes Fly Upside Down", "How Elevators Stop Falling",
              "How Tunnel Boring Machines Steer", "Why Ships Float on Water", "How Wind Turbines Brake",
              "How Escalators Recycle Steps", "Why Rails Have Gaps", "How Cranes Build Themselves",
              "How Fiber Optics Carry Light"]
    llm = fake_llm({"topics": [topic(t) for t in titles]})
    h = History(tmp_path / "h.json")
    plan = topic_picker.build_plan(cfg, llm, TRENDS, h, "run1", count=5, now=NOW)

    assert plan["count"] == 5
    vids = plan["videos"]
    assert [v["topic"]["title"] for v in vids] == titles[:5]
    assert [v["backup"]["title"] for v in vids] == titles[5:]
    assert len({v["style"]["voice"] for v in vids}) == 5  # suara berbeda tiap video
    times = [datetime.fromisoformat(v["publish_at"]) for v in vids]
    assert times == sorted(times) and len(set(times)) == 5
    assert all(t > NOW for t in times)
    assert vids[0]["topic"]["inspired_by_titles"] == ["EV pickup crash test"]
    # prompt memuat tren yang aman saja
    assert "vidWar" not in llm.calls[0]["prompt"]


def test_pick_topics_asks_again_then_fails(cfg, fake_llm, tmp_path):
    llm = fake_llm({"topics": [topic("Only One Topic Here")]}, {"topics": []}, {"topics": []})
    with pytest.raises(topic_picker.TopicError):
        topic_picker.pick_topics(cfg, llm, TRENDS, History(tmp_path / "h.json"), 2, "2026-11-17")
    assert len(llm.calls) == 3


def test_styles_avoid_recent_combinations(cfg, tmp_path):
    import random
    h = History(tmp_path / "h.json")
    styles = topic_picker.choose_styles(cfg, h, 5, random.Random(1))
    from ytauto.history import style_key
    assert len({style_key(s) for s in styles}) == 5


# ---------- scheduling ----------
def test_slots_respect_dst(cfg):
    from datetime import date
    winter = scheduling.slot_times(cfg, date(2026, 12, 1))
    summer = scheduling.slot_times(cfg, date(2026, 7, 1))
    assert winter[3].hour == 23  # 18:00 EST = 23:00 UTC
    assert summer[3].hour == 22  # 18:00 EDT = 22:00 UTC


def test_plan_publish_times_skips_past_slots(cfg):
    late = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)  # 16:00 NY: slot 08,12,15 sudah lewat
    times = scheduling.plan_publish_times(cfg, 5, late)
    assert times[0] == datetime(2026, 11, 17, 23, 0, tzinfo=timezone.utc)  # 18:00 NY
    assert times[2].date().isoformat() == "2026-11-18"
    assert len(set(times)) == 5


def test_resolve_publish_at():
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    assert scheduling.resolve_publish_at("2026-11-17T13:00:00+00:00", now) is not None
    assert scheduling.resolve_publish_at("2026-11-17T12:05:00+00:00", now) is None
    assert scheduling.resolve_publish_at(None, now) is None
    assert scheduling.rfc3339(now) == "2026-11-17T12:00:00.000Z"
