import json
from datetime import datetime, timedelta, timezone

import pytest

import upload_video as uv


def args(**kw):
    base = dict(title="My <Weird> Video", description="Desc", description_file=None, tags="fakta, unik,fakta",
                privacy="private", synthetic=False, category="27", language="en")
    base.update(kw)
    return type("A", (), base)()


def test_body_defaults_private_not_for_kids():
    b = uv.build_body(args(), None)
    assert b["snippet"]["title"] == "My Weird Video"
    assert b["snippet"]["tags"] == ["fakta", "unik"]
    assert b["status"] == {"selfDeclaredMadeForKids": False, "containsSyntheticMedia": False,
                           "embeddable": True, "license": "youtube", "privacyStatus": "private"}


def test_scheduled_publish_is_private_with_publish_at():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    b = uv.build_body(args(privacy="public"), future)
    assert b["status"]["privacyStatus"] == "private" and b["status"]["publishAt"].endswith("Z")


def test_publish_at_in_past_rejected():
    with pytest.raises(SystemExit):
        uv.parse_publish_at("2020-01-01 10:00", "Asia/Jakarta")


def test_check_mode_does_not_upload(tmp_path, capsys):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x" * 100)
    assert uv.main([str(v), "--title", "T", "--check"]) == 0
    assert '"privacyStatus": "private"' in capsys.readouterr().out


def test_missing_file():
    with pytest.raises(SystemExit, match="tidak ditemukan"):
        uv.main(["nope.mp4", "--title", "T", "--check"])


def test_env_and_client_secret_loading(tmp_path, monkeypatch):
    for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text("# c\nYT_REFRESH_TOKEN=abc\n", encoding="utf-8")
    (tmp_path / "cs.json").write_text(json.dumps({"installed": {"client_id": "id1", "client_secret": "s1"}}), encoding="utf-8")
    uv.load_env(tmp_path / ".env")
    uv.load_client_secret(tmp_path / "cs.json")
    import os
    assert (os.environ["YT_REFRESH_TOKEN"], os.environ["YT_CLIENT_ID"], os.environ["YT_CLIENT_SECRET"]) == ("abc", "id1", "s1")
