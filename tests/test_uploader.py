from datetime import datetime, timezone

import pytest
from googleapiclient.errors import HttpError

from ytauto import uploader

NOW = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
SCRIPT = {
    "title": "How Do You Drill Oil <3 km> Under the Sea?",
    "description": "Offshore rigs are floating factories.",
    "tags": ["oil rig", "Oil Rig", "engineering", "#shorts", "a,b"] + [f"tag number {i}" for i in range(60)],
    "hashtags": ["#shorts", "#engineering", "#how-it-works"],
    "sources": [{"id": "S1", "title": "Offshore drilling", "url": "https://en.wikipedia.org/wiki/Offshore_drilling"}],
}
ATTR = [{"text": "Video by Ann on Pexels", "url": "https://www.pexels.com/video/1/"}]


class Resp(dict):
    def __init__(self, status):
        super().__init__()
        self.status = status
        self.reason = "err"


def http_error(status, body=b'{"error": {"message": "x"}}'):
    return HttpError(Resp(status), body)


class FakeRequest:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def next_chunk(self):
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


class FakeYouTube:
    def __init__(self, outcomes, thumb_errors=()):
        self.outcomes = outcomes
        self.thumb_errors = list(thumb_errors)
        self.insert_kwargs = None
        self.thumb_calls = 0

    def videos(self):
        yt = self

        class V:
            def insert(self, **kw):
                yt.insert_kwargs = kw
                return FakeRequest(yt.outcomes)
        return V()

    def thumbnails(self):
        yt = self

        class T:
            def set(self, **kw):
                class Exec:
                    def execute(self_inner):
                        yt.thumb_calls += 1
                        if yt.thumb_errors:
                            raise yt.thumb_errors.pop(0)
                        return {}
                return Exec()
        return T()


@pytest.fixture
def files(tmp_path):
    v = tmp_path / "final.mp4"
    v.write_bytes(b"\x00" * 2048)
    t = tmp_path / "thumbnail.jpg"
    t.write_bytes(b"\xff\xd8jpg")
    return {"video": str(v), "thumbnail": str(t)}


def test_metadata_rules(cfg):
    body = uploader.build_body(cfg, SCRIPT, ATTR, NOW.replace(hour=13))
    sn, st = body["snippet"], body["status"]
    assert "<" not in sn["title"] and len(sn["title"]) <= 100
    assert sn["defaultAudioLanguage"] == "en" and sn["categoryId"] == "27"
    tags = sn["tags"]
    assert len({t.lower() for t in tags}) == len(tags)
    assert sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1 <= 480
    assert "shorts" in [t.lower() for t in tags]
    d = sn["description"]
    assert d.count("#shorts") == 1 and "#howitworks" in d
    assert "https://en.wikipedia.org/wiki/Offshore_drilling" in d and "Video by Ann on Pexels" in d
    assert st == {"selfDeclaredMadeForKids": False, "containsSyntheticMedia": False, "embeddable": True,
                  "license": "youtube", "privacyStatus": "private", "publishAt": "2026-11-17T13:00:00.000Z"}


@pytest.mark.parametrize("privacy", ["private", "unlisted", "public"])
def test_fixed_privacy_has_no_publish_at(cfg, privacy):
    cfg.upload.privacy_status = privacy
    st = uploader.build_body(cfg, SCRIPT, ATTR, NOW)["status"]
    assert st["privacyStatus"] == privacy and "publishAt" not in st


def test_synthetic_flag_from_config(cfg):
    cfg.upload.contains_synthetic_media = True
    assert uploader.build_body(cfg, SCRIPT, ATTR, None)["status"]["containsSyntheticMedia"] is True


def test_run_scheduled_upload_with_retries(cfg, files):
    yt = FakeYouTube([http_error(503), ConnectionResetError("reset"), (None, {"id": "abc123"})])
    res = uploader.run(cfg, SCRIPT, files, ATTR, "2026-11-17T13:00:00+00:00", youtube=yt, now=NOW)
    assert res["video_id"] == "abc123"
    assert res["url"] == "https://youtube.com/shorts/abc123"
    assert res["privacy"] == "private" and res["publish_at"] == "2026-11-17T13:00:00.000Z"
    assert yt.insert_kwargs["part"] == "snippet,status"
    assert yt.insert_kwargs["media_body"].resumable()
    assert yt.thumb_calls == 1 and res["warnings"] == []


def test_slot_passed_publishes_now(cfg, files):
    yt = FakeYouTube([(None, {"id": "x"})])
    res = uploader.run(cfg, SCRIPT, files, ATTR, "2026-11-17T08:05:00+00:00", youtube=yt, now=NOW)
    assert res["privacy"] == "public" and res["publish_at"] is None
    assert "lewat" in res["warnings"][0]


def test_quota_error_not_retried(cfg, files):
    yt = FakeYouTube([http_error(403, b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')])
    with pytest.raises(uploader.UploadError, match="403"):
        uploader.run(cfg, SCRIPT, files, ATTR, None, youtube=yt, now=NOW)


def test_thumbnail_failure_is_warning(cfg, files):
    yt = FakeYouTube([(None, {"id": "v1"})], thumb_errors=[http_error(403)])
    res = uploader.run(cfg, SCRIPT, files, ATTR, None, youtube=yt, now=NOW)
    assert res["video_id"] == "v1"
    assert "thumbnail gagal" in res["warnings"][0]


def test_thumbnail_retries_5xx(cfg, files):
    yt = FakeYouTube([(None, {"id": "v1"})], thumb_errors=[http_error(500)])
    res = uploader.run(cfg, SCRIPT, files, ATTR, None, youtube=yt, now=NOW)
    assert yt.thumb_calls == 2 and res["warnings"] == []


def test_missing_video_aborts(cfg, tmp_path):
    with pytest.raises(uploader.UploadError):
        uploader.run(cfg, SCRIPT, {"video": str(tmp_path / "nope.mp4"), "thumbnail": ""}, ATTR, None,
                     youtube=FakeYouTube([]), now=NOW)
