"""Upload-schedule rules shared by run_pipeline.py and hype_detector.py (window, daily-cap period, hype slots)."""

from datetime import datetime, timedelta, timezone

import clip_db


def _window_times(cfg):
    win = cfg.get("upload_window")
    if not win:
        return None
    return datetime.strptime(win["start"], "%H:%M").time(), datetime.strptime(win["end"], "%H:%M").time()


def in_upload_window(cfg) -> bool:
    """upload_window {"start": "11:00", "end": "23:00"} in PC local time; supports windows that cross midnight."""
    times = _window_times(cfg)
    if not times:
        return True
    start, end = times
    now = datetime.now().time()
    return start <= now < end if start <= end else (now >= start or now < end)


def cap_period_start(cfg) -> datetime:
    """Daily caps count uploads since the latest window opening (or local midnight without a window).
    A rolling 24h count would let late-night uploads eat into the next day's quota."""
    times = _window_times(cfg)
    local_now = datetime.now().astimezone()
    start = local_now.replace(hour=times[0].hour if times else 0, minute=times[0].minute if times else 0,
                              second=0, microsecond=0)
    if start > local_now:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


def hype_slots_left(conn, cfg) -> int:
    cap = cfg.get("hype", {}).get("max_uploads_per_day", 0)
    return cap - clip_db.uploads_since(conn, cap_period_start(cfg), source="hype")
