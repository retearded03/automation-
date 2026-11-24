"""Sumber bibit ide nyeleneh: hal aneh tapi nyata yang bisa dicek faktanya di Wikipedia.

- Wikipedia:Unusual articles (sub-halaman per tema): ribuan artikel aneh + deskripsi singkat yang
  sudah memancing rasa penasaran.
- Template:Did you know: fakta mengejutkan baru dari halaman utama Wikipedia.
Video trending YouTube tetap dipakai topic_picker sebagai sumber tambahan.
"""
from __future__ import annotations

import logging
import random
import re

from . import http
from .config import Config
from .history import History

WIKI_API = "https://en.wikipedia.org/w/api.php"
log = logging.getLogger("ytauto.retry")  # ikut handler log pipeline

ROW_RE = re.compile(r"'''\s*\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]\s*'''[^\n]*\n\|\s*(?:width=\"?\d+%\"?\s*\|)?\s*([^\n]+)")


def clean_wikitext(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*?/>|<ref.*?</ref>", "", text, flags=re.S)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", text)   # [[a|b]] -> b
    text = re.sub(r"\[https?://\S+ ([^\]]+)\]", r"\1", text)         # [url label] -> label
    text = text.replace("'''", "").replace("''", "").replace("&nbsp;", " ")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _wikitext(page: str, session=None) -> str:
    data = http.get_json(WIKI_API, session=session, params={
        "action": "parse", "page": page, "prop": "wikitext", "format": "json", "formatversion": 2,
    })
    return (data.get("parse") or {}).get("wikitext", "")


def parse_unusual(wikitext: str, category: str) -> list[dict]:
    seeds = []
    for title, desc in ROW_RE.findall(wikitext):
        hook = clean_wikitext(desc)
        title = title.strip()
        if title and len(hook) > 15:
            seeds.append({"title": title, "hook": hook[:220], "source": f"wikipedia unusual: {category}"})
    return seeds


def fetch_unusual(categories: list[str], session=None) -> list[dict]:
    seeds: list[dict] = []
    for cat in categories:
        try:
            seeds += parse_unusual(_wikitext(f"Wikipedia:Unusual articles/{cat}", session), cat)
        except Exception as e:  # noqa: BLE001 - satu kategori gagal tidak masalah
            log.warning("Unusual articles/%s gagal diambil: %s", cat, str(e)[:120])
    return seeds


def parse_dyk(wikitext: str) -> list[dict]:
    seeds = []
    for line in wikitext.splitlines():
        if not line.startswith("* ..."):
            continue
        bold = re.search(r"'''\s*\[\[([^\]|#]+)", line)
        if not bold:
            continue
        hook = clean_wikitext(line[1:]).replace("(pictured)", "").strip()
        seeds.append({"title": bold.group(1).strip(), "hook": hook[:220], "source": "wikipedia did you know"})
    return seeds


def fetch_dyk(session=None) -> list[dict]:
    try:
        return parse_dyk(_wikitext("Template:Did you know", session))
    except Exception as e:  # noqa: BLE001
        log.warning("Did you know gagal diambil: %s", str(e)[:120])
        return []


def gather_seeds(config: Config, history: History, rng: random.Random, session=None) -> list[dict]:
    """Ambil bibit ide, buang yang tidak aman atau sudah pernah dibahas, lalu acak."""
    from .topic_picker import is_unsafe  # hindari import melingkar

    ic = config.ideas
    seeds = fetch_unusual(ic.unusual_categories, session) + fetch_dyk(session)
    seen: set[str] = set()
    out = []
    for s in seeds:
        key = s["title"].lower()
        if key in seen or is_unsafe(f"{s['title']} {s['hook']}"):
            continue
        seen.add(key)
        if history.is_duplicate(f"{s['title']} {s['hook']}", threshold=0.45):
            continue
        out.append(s)
    rng.shuffle(out)
    dyk = [s for s in out if "did you know" in s["source"]]
    rest = [s for s in out if "did you know" not in s["source"]]
    picked = (dyk[:5] + rest)[: ic.seeds_for_llm]
    log.info("bibit ide: %d terkumpul, %d dikirim ke LLM", len(out), len(picked))
    return picked


def format_seeds(seeds: list[dict]) -> str:
    return "\n".join(f"- {s['title']} | {s['hook']}" for s in seeds) or "(none)"
