"""Langkah 2: pilih topik (via LLM) yang sesuai niche, aman, dan belum pernah dibahas.

Juga menetapkan kombinasi gaya dan waktu tayang untuk tiap video hari ini.
"""
from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone

from .config import Config
from .history import History, style_key
from .llm import LLM, render_prompt
from .scheduling import plan_publish_times

log = logging.getLogger("ytauto.topic_picker")


class TopicError(Exception):
    pass


# Saringan berbasis aturan, dijalankan sebelum dan sesudah LLM.
UNSAFE_PATTERNS = [
    r"\bkill(ed|ing|s)?\b", r"\bdead\b", r"\bdeath\b", r"\bdies?\b", r"\bdied\b", r"\bmurder", r"\bshooting\b",
    r"\bcrash victims?\b", r"\btragedy\b", r"\btragic", r"\bdisaster", r"\bhurricane", r"\bearthquake",
    r"\btsunami", r"\bgenocide", r"\bslave", r"\babuse", r"\bexecut", r"\bsex", r"\bwar\b", r"\bterror", r"\bbomb", r"\bhostage", r"\bmassacre",
    r"\belection", r"\bpresident\b", r"\bcongress\b", r"\bparliament\b", r"\bsenat", r"\bpolitic", r"\btrump\b",
    r"\bbiden\b", r"\bpolice\b", r"\bsuicide\b", r"\babortion\b", r"\breligio",
    r"\bofficial (music )?video\b", r"\bofficial trailer\b", r"\blyrics?\b", r"\bft\.\s", r"\bfeat\.\s",
    r"\bepisode\b", r"\bseason \d", r"\bhighlights\b", r"\bnfl\b", r"\bnba\b", r"\bpremier league\b", r"\bufc\b",
]
_UNSAFE_RE = re.compile("|".join(UNSAFE_PATTERNS), re.I)


def is_unsafe(text: str) -> bool:
    return bool(_UNSAFE_RE.search(text or ""))


def prefilter_trends(trends: list[dict], limit: int) -> list[dict]:
    out = [t for t in trends if not is_unsafe(f"{t['title']} {' '.join(t.get('tags', []))}")]
    return out[:limit]


def compact_trends(trends: list[dict]) -> str:
    lines = []
    for t in trends:
        tags = ", ".join(t.get("tags", [])[:6])
        lines.append(f"{t['id']} | {t['title']} | {tags} | cat {t['category_id']} | {t['views_per_hour']:.0f}/h")
    return "\n".join(lines) or "(no relevant trending videos today - use strong evergreen topics)"


def topic_score(t: dict) -> float:
    s = t.get("scores") or {}
    try:
        return float(s.get("hook", 0)) * 1.5 + float(s.get("weird", 0)) + float(s.get("explainable", 0)) * 0.8
    except (TypeError, ValueError):
        return 0.0


def validate_topics(raw: list[dict], history: History, threshold: float = 0.45) -> list[dict]:
    accepted: list[dict] = []
    for t in raw:
        title = (t.get("title") or "").strip()
        angle = (t.get("angle") or "").strip()
        queries = [q for q in (t.get("wikipedia_queries") or []) if isinstance(q, str) and q.strip()]
        if not title or not angle or not queries:
            log.info("topik dibuang (tidak lengkap): %r", title)
            continue
        text = f"{title} {angle}"
        if str(t.get("safety", "safe")).lower() != "safe" or is_unsafe(text):
            log.info("topik dibuang (tidak aman): %r", title)
            continue
        extra = [f"{a['title']} {a['angle']}" for a in accepted] + [a["title"] for a in accepted]
        if history.is_duplicate(text, threshold, extra=extra, title=title):
            log.info("topik dibuang (duplikat): %r", title)
            continue
        t["title"], t["angle"], t["wikipedia_queries"] = title, angle, queries[:3]
        accepted.append(t)
    return accepted


def choose_styles(config: Config, history: History, count: int, rng: random.Random) -> list[dict]:
    s = config.styles
    recent = history.recent_style_keys(s.avoid_recent_combinations)
    voices = list(config.voice.voices)
    rng.shuffle(voices)
    styles: list[dict] = []
    used_keys: set[str] = set()
    for i in range(count):
        style: dict = {}
        for _ in range(50):
            style = {
                "hook_type": rng.choice(s.hook_types),
                "structure": rng.choice(s.structures),
                "caption_style": rng.choice(s.caption_styles).name,
                "palette": rng.choice(s.palettes).name,
                "thumbnail_layout": rng.choice(s.thumbnail_layouts),
                "hook_overlay": rng.choice(s.hook_overlay),
            }
            key = style_key(style)
            if key not in recent and key not in used_keys:
                break
        used_keys.add(style_key(style))
        style["voice"] = voices[i % len(voices)]  # suara berbeda tiap video dalam satu hari
        style["seed"] = rng.randrange(1 << 30)
        styles.append(style)
    return styles


def pick_topics(config: Config, llm: LLM, trends: list[dict], history: History, count: int,
                today: str, seeds: list[dict] | None = None) -> list[dict]:
    """Minta banyak kandidat, saring, lalu ambil yang skornya tertinggi (utama + cadangan)."""
    from .idea_sources import format_seeds

    candidates = prefilter_trends(trends, config.trends.max_trends_for_llm)
    want = count * 2
    ask = max(want, count * config.ideas.candidates_per_video)
    accepted: list[dict] = []
    for attempt in range(3):
        prompt = render_prompt(
            "topic_picker",
            niche=config.channel.niche,
            audience=config.channel.audience,
            language=config.channel.language,
            today=today,
            trends=compact_trends(candidates),
            seeds=format_seeds(seeds or []),
            history="\n".join(f"- {t}" for t in history.recent_topics(150) + [a["title"] for a in accepted]) or "(none yet)",
            count=str(max(ask - len(accepted), count + 2)),
        )
        data = llm.complete_json("You are a careful, safety-minded editor. Output JSON only.", prompt,
                                 step="topic_picker")
        raw = data.get("topics", []) if isinstance(data, dict) else data
        new = validate_topics(raw or [], History(history.path, {"videos": history.videos + [
            {"topic": a["angle"], "title": a["title"]} for a in accepted]}))
        accepted.extend(new)
        if len(accepted) >= want:  # satu panggilan biasanya cukup; kandidat dipilih lewat skor
            break
        log.info("baru %d/%d topik layak, minta lagi (percobaan %d)", len(accepted), want, attempt + 1)
    if len(accepted) < count:
        raise TopicError(f"hanya {len(accepted)} topik layak, butuh {count}")
    accepted.sort(key=topic_score, reverse=True)
    for t in accepted[:want]:
        log.info("topik terpilih (skor %.1f): %s", topic_score(t), t["title"])
    return accepted[:want]


def build_plan(config: Config, llm: LLM, trends: list[dict], history: History, run_id: str,
               count: int | None = None, now: datetime | None = None, seeds: list[dict] | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    count = count or config.schedule.videos_per_day
    topics = pick_topics(config, llm, trends, history, count, now.date().isoformat(), seeds=seeds)
    primaries, backups = topics[:count], topics[count:]
    rng = random.Random(f"{run_id}-styles")
    styles = choose_styles(config, history, count, rng)
    times = plan_publish_times(config, count, now)
    trend_by_id = {t["id"]: t for t in trends}
    items = []
    for i in range(count):
        topic = primaries[i]
        topic["inspired_by_titles"] = [trend_by_id[x]["title"] for x in topic.get("inspired_by", []) if x in trend_by_id]
        items.append({
            "index": i,
            "topic": topic,
            "backup": backups[i] if i < len(backups) else None,
            "style": styles[i],
            "publish_at": times[i].isoformat(),
        })
    return {"run_id": run_id, "created_at": now.isoformat(timespec="seconds"), "count": count, "videos": items}
