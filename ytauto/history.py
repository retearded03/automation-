"""history.json: mencegah topik, klip, dan kombinasi gaya berulang."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STOPWORDS = {
    "a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "how", "why", "what", "is", "are",
    "do", "does", "did", "can", "you", "your", "it", "its", "this", "that", "with", "from", "by",
    "at", "as", "be", "into", "really", "actually", "ever", "never", "most", "so", "they", "we",
    "work", "works", "made", "make", "fact", "facts",
    # kata umum di judul/angle yang tidak menunjukkan topik
    "explain", "explains", "explained", "inside", "without", "using", "use", "uses", "tiny", "huge", "giant",
    "secret", "every", "thousand", "thousands", "million", "billion", "time", "times", "second", "seconds",
    "way", "ways", "thing", "things", "doesn", "don", "isn", "just", "like", "about", "their", "them", "there",
    "these", "those", "than", "then", "when", "where", "which", "who", "while", "through", "over", "under",
    "after", "before",
}


SYNONYMS = {
    "smartphone": "phone", "smartphones": "phone", "mobile": "phone", "cellphone": "phone",
    "chip": "chip", "microchip": "chip", "processor": "chip", "cpu": "chip", "semiconductor": "chip",
    "cooling": "cool", "cooler": "cool", "cooled": "cool", "heat": "cool", "thermal": "cool",
    "car": "car", "cars": "car", "vehicle": "car", "vehicles": "car", "automobile": "car",
    "plane": "plane", "airplane": "plane", "aircraft": "plane", "jet": "plane",
}


def tokens(text: str) -> set[str]:
    words = [SYNONYMS.get(w, w) for w in re.findall(r"[a-z0-9]+", text.lower())]
    out = set()
    for w in words:
        if w in STOPWORDS or len(w) < 3:
            continue
        # stemming sangat sederhana: buang akhiran jamak
        if len(w) > 4 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def overlap(a: str, b: str) -> float:
    """Porsi kata kunci teks yang lebih pendek yang juga ada di teks lain (0-1).

    Lebih peka daripada Jaccard untuk topik sama yang ditulis dengan panjang berbeda.
    Kalibrasi dari history asli: topik dobel 0,54; topik berbeda yang paling mirip 0,31.
    """
    ta, tb = tokens(a), tokens(b)
    if len(ta) < 3 or len(tb) < 3:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


OVERLAP_THRESHOLD = 0.45


def style_key(style: dict) -> str:
    return "|".join(str(style.get(k, "")) for k in ("hook_type", "structure", "caption_style", "palette", "thumbnail_layout"))


class History:
    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        self.data = data or {"version": 1, "videos": []}

    @classmethod
    def load(cls, path: Path) -> "History":
        if not path.exists():
            return cls(path)
        raw = path.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else {}
        data.setdefault("version", 1)
        data.setdefault("videos", [])
        return cls(path, data)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @property
    def videos(self) -> list[dict]:
        return self.data["videos"]

    def topic_texts(self) -> list[str]:
        return [f"{v.get('topic', '')} {v.get('title', '')}" for v in self.videos]

    def recent_titles(self, n: int = 150) -> list[str]:
        return [v.get("title") or v.get("topic", "") for v in self.videos[-n:]]

    def recent_topics(self, n: int = 150) -> list[str]:
        """Judul + sudut bahasan, supaya LLM mengenali topik sama yang judulnya berbeda."""
        out = []
        for v in self.videos[-n:]:
            title, topic = v.get("title", ""), v.get("topic", "")
            out.append(f"{title} (topic: {topic})" if topic and topic != title else title or topic)
        return out

    def is_duplicate(self, text: str, threshold: float = 0.5, extra: list[str] | None = None,
                     title: str | None = None, title_threshold: float = 0.6) -> bool:
        for other in self.topic_texts() + (extra or []):
            if similarity(text, other) >= threshold or overlap(text, other) >= OVERLAP_THRESHOLD:
                return True
        if title:
            others = [v.get("title", "") for v in self.videos] + (extra or [])
            if any(similarity(title, o) >= title_threshold for o in others if o):
                return True
        return False

    def used_clip_ids(self, days: int = 60, now: datetime | None = None) -> set[str]:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        used: set[str] = set()
        for v in self.videos:
            try:
                when = datetime.fromisoformat(v.get("created_at", "1970-01-01T00:00:00+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when >= cutoff:
                used.update(v.get("clip_ids", []))
        return used

    def recent_style_keys(self, n: int) -> set[str]:
        return {style_key(v.get("style", {})) for v in self.videos[-n:]} if n > 0 else set()

    def add(self, entry: dict[str, Any]) -> None:
        entry.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.videos.append(entry)
