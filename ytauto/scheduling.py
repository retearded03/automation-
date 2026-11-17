"""Menghitung waktu tayang (publishAt) per video di jam ramai penonton target."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config


def slot_times(config: Config, day: date) -> list[datetime]:
    """Semua slot pada tanggal `day` (zona waktu penonton), dikonversi ke UTC dan terurut."""
    tz = ZoneInfo(config.schedule.timezone)
    out = []
    for s in config.schedule.publish_slots:
        hh, mm = map(int, s.split(":"))
        out.append(datetime.combine(day, time(hh, mm), tzinfo=tz).astimezone(timezone.utc))
    return sorted(out)


def plan_publish_times(config: Config, count: int, now: datetime | None = None) -> list[datetime]:
    """Pilih `count` slot berikutnya yang masih cukup jauh dari sekarang.

    Slot hari ini yang sudah lewat diganti slot hari berikutnya, jadi waktu tayang tidak pernah bentrok
    dan tidak pernah di masa lalu.
    """
    now = now or datetime.now(timezone.utc)
    lead = timedelta(minutes=config.schedule.min_lead_minutes)
    tz = ZoneInfo(config.schedule.timezone)
    day = now.astimezone(tz).date()
    result: list[datetime] = []
    while len(result) < count:
        for t in slot_times(config, day):
            if t >= now + lead and len(result) < count:
                result.append(t)
        day += timedelta(days=1)
    return result


def resolve_publish_at(planned: str | None, now: datetime | None = None, lead_minutes: int = 20) -> datetime | None:
    """Saat upload: kalau slot rencana sudah (hampir) lewat, kembalikan None = tayang sekarang."""
    if not planned:
        return None
    now = now or datetime.now(timezone.utc)
    t = datetime.fromisoformat(planned)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t if t >= now + timedelta(minutes=lead_minutes) else None


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
