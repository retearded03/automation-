import re
from datetime import datetime, timezone

import responses

from conftest import fixture_json
from ytauto import trend_finder

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
URL_RE = re.compile(r"https://www\.googleapis\.com/youtube/v3/videos.*")


def test_parse_item_views_per_hour():
    item = fixture_json("trending_us.json")["items"][0]
    v = trend_finder.parse_item(item, "US", NOW)
    assert v["hours_since_publish"] == 24.0
    assert v["views_per_hour"] == 10000.0
    assert v["category_id"] == "2"
    assert v["regions"] == ["US"]


def test_parse_item_min_one_hour():
    item = {"id": "x", "snippet": {"publishedAt": "2026-11-17T11:59:00Z"}, "statistics": {"viewCount": "100"}}
    assert trend_finder.parse_item(item, "US", NOW)["views_per_hour"] == 100.0


@responses.activate
def test_find_trends_filters_merges_and_sorts(cfg):
    cfg.trends.regions = ["US", "GB"]
    cfg.trends.category_ids = ["28"]
    data = fixture_json("trending_us.json")

    def handler(request):
        import json
        if "videoCategoryId=28" in request.url and "regionCode=GB" in request.url:
            return 404, {}, json.dumps({"error": {"errors": [{"reason": "videoChartNotFound"}]}})
        return 200, {}, json.dumps(data)

    responses.add_callback(responses.GET, URL_RE, callback=handler)
    trends = trend_finder.find_trends(cfg, "APIKEY", now=NOW)

    # 2 region x (umum + kategori 28) = 4 panggilan, tidak memakai search.list
    assert len(responses.calls) == 4
    assert all("chart=mostPopular" in c.request.url for c in responses.calls)
    assert all("/search" not in c.request.url for c in responses.calls)
    ids = [t["id"] for t in trends]
    assert "vidMusic" not in ids  # kategori Musik diblokir
    assert set(ids) == {"vidCar1", "vidBridge"}
    car = next(t for t in trends if t["id"] == "vidCar1")
    assert sorted(car["regions"]) == ["GB", "US"]
    assert ids[0] == "vidCar1"  # views/jam tertinggi


@responses.activate
def test_other_errors_propagate(cfg):
    cfg.trends.regions = ["US"]
    cfg.trends.category_ids = []
    responses.add(responses.GET, URL_RE, status=403, body='{"error": {"errors": [{"reason": "quotaExceeded"}]}}')
    try:
        trend_finder.find_trends(cfg, "APIKEY", now=NOW)
    except Exception as e:  # noqa: BLE001
        assert "403" in str(e)
        assert "APIKEY" not in str(e)
    else:
        raise AssertionError("seharusnya gagal")
