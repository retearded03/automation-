"""Pembungkus requests: retry otomatis untuk 429/5xx/koneksi, tanpa membocorkan API key ke log."""
from __future__ import annotations

import re
from pathlib import Path

import requests

from .retry import RetryableError, retry

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
USER_AGENT = "yt-autopilot/1.0 (automated educational video research)"


class HTTPError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} dari {safe_url(url)}: {body[:300]}")
        self.status = status
        self.body = body


def safe_url(url: str) -> str:
    """Buang query string (bisa berisi key=...) dan token bot Telegram sebelum ditulis ke log/pesan error."""
    return re.sub(r"/bot[^/]+/", "/bot***/", url.split("?", 1)[0])


def _retry_after(resp: requests.Response) -> float | None:
    val = resp.headers.get("Retry-After", "")
    try:
        return float(val)
    except ValueError:
        return None


def request(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    attempts: int = 5,
    timeout: float = 60,
    base_delay: float = 1.0,
    **kwargs,
) -> requests.Response:
    sess = session or requests
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}

    @retry(attempts=attempts, base_delay=base_delay, retry_on=(RetryableError,))
    def _do() -> requests.Response:
        try:
            resp = sess.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise RetryableError(f"koneksi gagal ke {safe_url(url)}: {type(e).__name__}") from None
        if resp.status_code in RETRY_STATUS:
            raise RetryableError(f"HTTP {resp.status_code} dari {safe_url(url)}", _retry_after(resp))
        if resp.status_code >= 400:
            raise HTTPError(resp.status_code, url, resp.text)
        return resp

    return _do()


def get_json(url: str, **kwargs):
    return request("GET", url, **kwargs).json()


def post_json(url: str, payload: dict, **kwargs):
    return request("POST", url, json=payload, **kwargs).json()


def download(url: str, dest: Path, *, max_bytes: int, session: requests.Session | None = None) -> Path:
    """Unduh file ke dest secara streaming; batalkan kalau melebihi max_bytes."""
    resp = request("GET", url, session=session, stream=True, timeout=120)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            size += len(chunk)
            if size > max_bytes:
                f.close()
                tmp.unlink(missing_ok=True)
                raise HTTPError(413, url, f"file melebihi batas {max_bytes} byte")
            f.write(chunk)
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPError(204, url, "file kosong")
    tmp.replace(dest)
    return dest
