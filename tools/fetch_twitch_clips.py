"""Discover highlight clips for the configured game and add new ones to the queue.

Twitch viewers clip the best moments live, so the most-viewed recent clips in a category are
a free, crowd-sourced highlight detector. Filters and limits come from config.json.

    python tools/fetch_twitch_clips.py            # fetch + enqueue
    python tools/fetch_twitch_clips.py --dry-run  # show what would be enqueued
"""

import argparse
from datetime import datetime, timedelta, timezone

import clip_db
from _common import load_config
from twitch_api import Twitch


def fetch(dry_run: bool = False) -> dict:
    cfg = load_config()
    tw = Twitch()
    gid = tw.game_id(cfg["game_name"])
    started_at = (datetime.now(timezone.utc) - timedelta(hours=cfg["lookback_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")

    if cfg["watchlist"]:
        ids = tw.user_ids(cfg["watchlist"])
        missing = set(l.lower() for l in cfg["watchlist"]) - set(ids)
        if missing:
            print(f"Warning: watchlist logins not found on Twitch: {sorted(missing)}")
        raw = []
        for bid in ids.values():
            raw += [c for c in tw.clips(broadcaster_id=bid, started_at=started_at) if c["game_id"] == gid]
    else:
        raw = tw.clips(game_id=gid, started_at=started_at, max_pages=cfg["max_pages"])

    blocked = {b.lower() for b in cfg["blocklist"]}
    # Substring match on purpose ("goon" catches "GoonerPixel"); a false positive only costs one clip.
    bad_words = [w.lower() for w in cfg.get("title_blocklist", [])]
    langs = set(cfg["languages"])
    candidates = [
        c for c in raw
        if c["view_count"] >= cfg["min_views"]
        and c["duration"] >= cfg["min_duration_sec"]
        and (not langs or c["language"] in langs)
        and c["broadcaster_name"].lower() not in blocked
        and not any(w in c["title"].lower() for w in bad_words)
    ]
    candidates.sort(key=lambda c: c["view_count"], reverse=True)

    stats = {"fetched": len(raw), "candidates": len(candidates), "new": 0, "duplicate": 0, "refreshed": 0}
    conn = clip_db.connect()
    for c in candidates:
        if clip_db.get(conn, c["id"]):
            if not dry_run:
                clip_db.update(conn, c["id"], view_count=c["view_count"], vod_offset=c["vod_offset"])
            stats["refreshed"] += 1
            continue

        status = "queued"
        overlaps = clip_db.find_overlapping(conn, c["broadcaster_id"], c["created_at"], cfg["duplicate_window_sec"], c["id"])
        if any(o["status"] != "queued" or o["view_count"] >= c["view_count"] for o in overlaps):
            status = "duplicate"
        elif not dry_run:
            for o in overlaps:  # this clip is the better copy of the same moment
                clip_db.update(conn, o["clip_id"], status="duplicate")

        stats["new" if status == "queued" else "duplicate"] += 1
        if dry_run:
            print(f"  [{status:<9}] {c['broadcaster_name']:<18} {c['view_count']:>6}v {c['duration']:>4.0f}s  {c['title'][:60]!r}")
            continue
        conn.execute(
            "INSERT INTO clips (clip_id, url, broadcaster_id, broadcaster_name, title, language, view_count, duration, "
            "created_at, video_id, vod_offset, status, discovered_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["id"], c["url"], c["broadcaster_id"], c["broadcaster_name"], c["title"], c["language"], c["view_count"],
             c["duration"], c["created_at"], c["video_id"], c["vod_offset"], status, clip_db.now_iso(), clip_db.now_iso()),
        )
        conn.commit()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    s = fetch(args.dry_run)
    print(f"fetched={s['fetched']} candidates={s['candidates']} new={s['new']} duplicate={s['duplicate']} refreshed={s['refreshed']}")
