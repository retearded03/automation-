"""Langkah 3: riset singkat dari Wikipedia, tulis naskah orisinal, cek fakta, lalu quality gate.

Cek fakta dua lapis:
1. Programatik: setiap klaim harus mengutip kalimat yang benar-benar ada di teks sumber.
2. LLM terpisah menilai apakah kutipan itu memang mendukung klaim, dan menandai segmen yang memuat
   fakta tanpa klaim. Segmen yang bermasalah dibuang; kalau hook ikut terbuang atau sisa segmen
   terlalu sedikit, naskah ditulis ulang dengan umpan balik.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

from . import http
from .config import Config
from .llm import LLM, render_prompt

WIKI_API = "https://en.wikipedia.org/w/api.php"
CUT_SECTIONS = re.compile(r"\n=+\s*(See also|References|External links|Notes|Further reading|Bibliography|Citations)\s*=+.*", re.S | re.I)
log = logging.getLogger("ytauto.script_writer")


class ScriptRejected(Exception):
    """Topik/naskah ditolak; pipeline boleh mencoba topik cadangan."""


class ResearchError(ScriptRejected):
    pass


class FactCheckError(ScriptRejected):
    pass


class QualityError(ScriptRejected):
    pass


# ---------------------------------------------------------------- riset
def wiki_search(query: str, limit: int = 2, session=None) -> list[str]:
    data = http.get_json(WIKI_API, session=session, params={
        "action": "query", "list": "search", "srsearch": query, "srlimit": limit,
        "format": "json", "formatversion": 2,
    })
    return [r["title"] for r in data.get("query", {}).get("search", [])]


def clean_extract(text: str, max_chars: int) -> str:
    text = CUT_SECTIONS.sub("", text)
    text = re.sub(r"\n=+\s*([^=\n]+?)\s*=+", r"\n\1:", text)  # judul bagian jadi teks biasa
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > max_chars * 0.6 else cut


def wiki_extract(title: str, max_chars: int, session=None) -> dict | None:
    data = http.get_json(WIKI_API, session=session, params={
        "action": "query", "prop": "extracts|info", "inprop": "url", "explaintext": 1, "redirects": 1,
        "titles": title, "format": "json", "formatversion": 2,
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    p = pages[0]
    text = clean_extract(p.get("extract", ""), max_chars)
    if not text or "may refer to" in text[:300]:
        return None  # halaman disambiguasi
    url = p.get("fullurl") or f"https://en.wikipedia.org/wiki/{quote(p['title'].replace(' ', '_'))}"
    return {"title": p["title"], "url": url, "text": text}


def gather_sources(config: Config, topic: dict, session=None) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    for q in topic.get("wikipedia_queries", []):
        for title in wiki_search(q, session=session):
            if title in seen or len(sources) >= config.script.sources_per_video:
                continue
            seen.add(title)
            src = wiki_extract(title, config.script.max_source_chars, session=session)
            if src:
                src["id"] = f"S{len(sources) + 1}"
                sources.append(src)
    if not sources or sum(len(s["text"]) for s in sources) < 800:
        raise ResearchError(f"sumber Wikipedia tidak cukup untuk topik {topic.get('title')!r}")
    return sources


def format_sources(sources: list[dict]) -> str:
    return "\n\n".join(f"[{s['id']}] {s['title']}\n{s['text']}" for s in sources)


# ---------------------------------------------------------------- naskah
def _clean_narration(text: str) -> str:
    text = re.sub(r"[*_#`>\[\]]", "", text or "")
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)  # emoji
    text = re.sub(r"\s+", " ", text).strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def normalize_script(raw: dict, config: Config) -> dict:
    if not isinstance(raw, dict) or not raw.get("segments"):
        raise FactCheckError("naskah dari LLM tidak memiliki segmen")
    segs = []
    for s in raw["segments"]:
        text = _clean_narration(s.get("text", ""))
        if not text:
            continue
        kws = [k.strip() for k in (s.get("visual_keywords") or []) if isinstance(k, str) and k.strip()]
        scene = s.get("scene") if isinstance(s.get("scene"), str) else ""
        segs.append({"text": text, "claims": [str(c) for c in (s.get("claims") or [])], "scene": scene.strip(),
                     "visual_keywords": kws[:3] or list(config.channel.fallback_visual_keywords[:2])})
    claims = [c for c in (raw.get("claims") or []) if isinstance(c, dict) and c.get("id")]
    return {
        "title": (raw.get("title") or "").strip()[:100],
        "thumbnail_text": (raw.get("thumbnail_text") or raw.get("title") or "").strip(),
        "segments": segs,
        "claims": claims,
        "description": (raw.get("description") or "").strip(),
        "tags": [t.strip() for t in (raw.get("tags") or []) if isinstance(t, str) and t.strip()],
        "hashtags": [h.strip() for h in (raw.get("hashtags") or []) if isinstance(h, str) and h.strip()],
    }


def word_count(script: dict) -> int:
    return sum(len(s["text"].split()) for s in script["segments"])


def write_script(config: Config, llm: LLM, topic: dict, style: dict, sources: list[dict], feedback: str = "") -> dict:
    prompt = render_prompt(
        "script_writer",
        niche=config.channel.niche,
        audience=config.channel.audience,
        language=config.channel.language,
        topic=json.dumps({k: topic.get(k) for k in ("title", "angle", "core_fact_idea")}, ensure_ascii=False),
        style=f"- Hook type: {style['hook_type']}\n- Structure: {style['structure']}",
        target_words=str(config.format.target_words),
        min_segments=str(config.script.min_segments),
        max_segments=str(config.script.max_segments),
        sources=format_sources(sources),
        feedback=f"\nFEEDBACK ON THE PREVIOUS DRAFT (fix all of it):\n{feedback}\n" if feedback else "",
    )
    raw = llm.complete_json("You are a precise science and engineering writer. Output JSON only.", prompt,
                            step="script_writer")
    return normalize_script(raw, config)


# ---------------------------------------------------------------- cek fakta
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def evidence_found(evidence: str, source_text: str, min_overlap: float = 0.85) -> bool:
    ev = _norm(evidence)
    if len(ev) < 15:
        return False
    src = _norm(source_text)
    if ev in src:
        return True
    ev_tokens = [t for t in ev.split() if len(t) > 2]
    if not ev_tokens:
        return False
    for sentence in re.split(r"(?<=[.!?])\s+|\n", source_text):
        st = set(_norm(sentence).split())
        if st and sum(t in st for t in ev_tokens) / len(ev_tokens) >= min_overlap:
            return True
    return False


def fact_check(config: Config, llm: LLM, script: dict, sources: list[dict]) -> tuple[dict, dict]:
    by_id = {s["id"]: s for s in sources}
    unsupported: dict[str, str] = {}
    for c in script["claims"]:
        src = by_id.get(str(c.get("source", "")).strip("[] "))
        if not src:
            unsupported[c["id"]] = "sumber tidak dikenal"
        elif not evidence_found(c.get("evidence", ""), src["text"]):
            unsupported[c["id"]] = "kutipan tidak ditemukan di sumber"

    verdict = llm.complete_json(
        "You are a strict fact-checker. Output JSON only.",
        render_prompt("fact_check", sources=format_sources(sources), script=json.dumps(
            {"segments": [{"index": i, "text": s["text"], "claims": s["claims"]} for i, s in enumerate(script["segments"])],
             "claims": script["claims"]}, ensure_ascii=False, indent=1)),
        temperature=0.0,
        step="fact_check",
    )
    llm_claims = {str(c.get("id")): c for c in (verdict.get("claims") or [])}
    for c in script["claims"]:
        v = llm_claims.get(c["id"])
        if not v or not v.get("supported"):
            unsupported.setdefault(c["id"], (v or {}).get("reason", "tidak diverifikasi oleh pemeriksa"))
    flagged = {int(s.get("index", -1)) for s in (verdict.get("segments") or []) if s.get("uncited_facts")}

    known_claims = {c["id"] for c in script["claims"]}
    kept, removed = [], []
    for i, seg in enumerate(script["segments"]):
        bad_claims = [c for c in seg["claims"] if c in unsupported or c not in known_claims]
        if bad_claims or i in flagged:
            removed.append({"index": i, "text": seg["text"], "claims": bad_claims, "uncited_facts": i in flagged})
        else:
            kept.append(seg)
    report = {"unsupported_claims": unsupported, "removed_segments": removed}

    if removed and removed[0]["index"] == 0:
        raise FactCheckError(f"hook tidak lolos cek fakta: {removed[0]['text'][:120]}")
    if len(kept) < config.script.min_segments:
        raise FactCheckError(f"hanya {len(kept)} segmen lolos cek fakta (minimal {config.script.min_segments})")

    out = dict(script)
    out["segments"] = kept
    out["claims"] = [c for c in script["claims"] if c["id"] not in unsupported]
    log.info("cek fakta: %d klaim dibuang, %d segmen dibuang", len(unsupported), len(removed))
    return out, report


# ---------------------------------------------------------------- quality gate
def quality_gate(config: Config, llm: LLM, script: dict, recent_titles: list[str]) -> dict:
    score = llm.complete_json(
        "You are a demanding editor. Output JSON only.",
        render_prompt("quality_gate", niche=config.channel.niche,
                      recent_titles="\n".join(f"- {t}" for t in recent_titles[-30:]) or "(none)",
                      script=json.dumps({k: script[k] for k in ("title", "segments")}, ensure_ascii=False, indent=1)),
        temperature=0.2,
        step="quality_gate",
    )
    try:
        overall = float(score.get("overall", 0))
    except (TypeError, ValueError):
        overall = 0.0
    score["overall"] = overall
    if overall < config.script.min_quality_score:
        raise QualityError(f"skor kualitas {overall:.1f} < {config.script.min_quality_score}: {score.get('main_weakness', '')}")
    return score


# ---------------------------------------------------------------- orkestrasi
def produce_script(config: Config, llm: LLM, topic: dict, style: dict, recent_titles: list[str],
                   session=None) -> dict:
    sources = gather_sources(config, topic, session=session)
    feedback = ""
    last_error: ScriptRejected | None = None
    target = config.format.target_words
    for attempt in range(1, config.script.max_attempts + 1):
        log.info("menulis naskah (percobaan %d)", attempt)
        script = write_script(config, llm, topic, style, sources, feedback)
        words = word_count(script)
        if words > target * 1.35 or words < target * 0.55:
            feedback = f"The narration has {words} words; it must be close to {target} words."
            last_error = FactCheckError(f"panjang naskah {words} kata, target {target}")
            continue
        try:
            checked, report = fact_check(config, llm, script, sources)
            quality = quality_gate(config, llm, checked, recent_titles)
        except FactCheckError as e:
            feedback = f"The draft failed fact-checking: {e}. Use only facts stated in the sources and quote evidence verbatim."
            last_error = e
            continue
        except QualityError as e:
            feedback = f"An editor rejected the draft: {e}. Make the hook sharper and the angle more original."
            last_error = e
            continue
        checked.update({
            "sources": [{k: s[k] for k in ("id", "title", "url")} for s in sources],
            "fact_check": report,
            "quality": quality,
            "word_count": word_count(checked),
            "topic": topic,
            "style": style,
        })
        return checked
    raise last_error or ScriptRejected("naskah gagal dibuat")
