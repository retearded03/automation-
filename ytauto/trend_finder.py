"""Langkah 1: ambil video trending via videos.list chart=mostPopular (1 unit kuota per panggilan).

Sengaja TIDAK memakai search.list. Video trending hanya dipakai sebagai sinyal topik.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import http
from .config import Config

API_URL = "https://www.googleapis.com/youtube/v3/videos"
log = logging.getLogger("ytauto.trend_finder")


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_item(item: dict, region: str, now: datetime) -> dict:
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    published = sn.get("publishedAt")
    try:
        pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else now
    except ValueError:
        pub_dt = now
    hours = max((now - pub_dt).total_seconds() / 3600, 1.0)  # minimal 1 jam supaya tidak meledak
    views = _int(st.get("viewCount"))
    return {
        "id": item.get("id"),
        "title": sn.get("title", ""),
        "description": (sn.get("description") or "")[:400],
        "tags": (sn.get("tags") or [])[:15],
        "category_id": str(sn.get("categoryId", "")),
        "channel_title": sn.get("channelTitle", ""),
        "published_at": published,
        "views": views,
        "likes": _int(st.get("likeCount")),
        "comments": _int(st.get("commentCount")),
        "hours_since_publish": round(hours, 1),
        "views_per_hour": round(views / hours, 1),
        "regions": [region],
    }


def fetch_chart(api_key: str, region: str, category_id: str | None, max_results: int, now: datetime,
                session=None) -> list[dict]:
    params = {
        "part": "snippet,statistics",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": max_results,
        "key": api_key,
    }
    if category_id:
        params["videoCategoryId"] = category_id
    try:
        data = http.get_json(API_URL, params=params, session=session)
    except http.HTTPError as e:
        # Tidak semua kategori punya chart di semua region -> lewati saja.
        if e.status in (400, 404) and ("videoChartNotFound" in e.body or "notFound" in e.body
                                       or "invalidCategory" in e.body.lower() or "not found" in e.body.lower()):
            log.warning("chart %s/%s tidak tersedia, dilewati", region, category_id or "umum")
            return []
        raise
    return [parse_item(it, region, now) for it in data.get("items", [])]


def find_trends(config: Config, api_key: str, now: datetime | None = None, session=None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    t = config.trends
    categories: list[str | None] = list(t.category_ids)
    if t.include_general_chart:
        categories.insert(0, None)

    merged: dict[str, dict] = {}
    calls = 0
    for region in t.regions:
        for cat in categories:
            calls += 1
            for v in fetch_chart(api_key, region, cat, t.max_results_per_chart, now, session=session):
                if v["id"] in merged:
                    m = merged[v["id"]]
                    if region not in m["regions"]:
                        m["regions"].append(region)
                    m["views_per_hour"] = max(m["views_per_hour"], v["views_per_hour"])
                else:
                    merged[v["id"]] = v
    log.info("trend: %d panggilan videos.list (~%d unit kuota), %d video unik", calls, calls, len(merged))

    blocked = set(t.blocked_category_ids)
    trends = [v for v in merged.values() if v["category_id"] not in blocked]
    # Video yang trending di lebih banyak region sedikit diprioritaskan
    trends.sort(key=lambda v: v["views_per_hour"] * (1 + 0.25 * (len(v["regions"]) - 1)), reverse=True)
    return trends
