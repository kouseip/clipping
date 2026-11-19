"""SQLite state for the clip pipeline: every clip we've seen and how far it got.

Status flow: queued -> downloaded -> transcribed -> rendered -> uploaded
Side exits:  duplicate (same moment clipped by someone else), skipped, failed

CLI:
    python tools/clip_db.py                 # status counts
    python tools/clip_db.py list [status]   # recent clips, optionally filtered
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _common import DATA_DIR

# CLIP_DB lets tests point at a scratch database instead of the real one.
DB_PATH = Path(os.getenv("CLIP_DB", DATA_DIR / "clips.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    clip_id          TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    broadcaster_id   TEXT,
    broadcaster_name TEXT,
    title            TEXT,
    language         TEXT,
    view_count       INTEGER,
    duration         REAL,
    created_at       TEXT,          -- when the clip was made on Twitch (UTC ISO)
    video_id         TEXT,
    vod_offset       INTEGER,
    source           TEXT DEFAULT 'twitch_clips',
    status           TEXT NOT NULL DEFAULT 'queued',
    source_path      TEXT,
    transcript_path  TEXT,
    render_path      TEXT,
    youtube_id       TEXT,
    yt_title         TEXT,
    safety           TEXT,          -- 'ok' once the content check passed (rejects go to status 'skipped')
    spoiler          INTEGER,       -- 1 = main-story moment; title gets a "SPOILER:" prefix
    trim_start       REAL,          -- edit plan: window kept from the source clip (seconds)
    trim_end         REAL,
    hook             TEXT,          -- on-screen hook banner text
    tags             TEXT,          -- JSON list of clip-specific tags
    error            TEXT,
    attempts         INTEGER DEFAULT 0,
    discovered_at    TEXT,
    updated_at       TEXT,
    uploaded_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON clips(status);
CREATE INDEX IF NOT EXISTS idx_broadcaster ON clips(broadcaster_id, created_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Columns added after the first release; CREATE TABLE IF NOT EXISTS won't add them to an existing DB.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}
    for col, kind in (("safety", "TEXT"), ("spoiler", "INTEGER"), ("trim_start", "REAL"), ("trim_end", "REAL"),
                      ("hook", "TEXT"), ("tags", "TEXT")):
        if col not in existing:
            conn.execute(f"ALTER TABLE clips ADD COLUMN {col} {kind}")
    conn.commit()
    return conn


def get(conn, clip_id: str):
    return conn.execute("SELECT * FROM clips WHERE clip_id = ?", (clip_id,)).fetchone()


def update(conn, clip_id: str, **fields):
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE clips SET {cols} WHERE clip_id = ?", (*fields.values(), clip_id))
    conn.commit()


def fail(conn, clip_id: str, error: str, max_attempts: int = 3):
    """Record an error; give up on the clip after max_attempts so one bad clip can't jam the queue."""
    row = get(conn, clip_id)
    attempts = (row["attempts"] or 0) + 1
    status = "failed" if attempts >= max_attempts else row["status"]
    update(conn, clip_id, error=error[:2000], attempts=attempts, status=status)


def find_overlapping(conn, broadcaster_id: str, created_at: str, window_sec: int, exclude_id: str):
    """Clips from the same streamer made within window_sec of each other are almost always the same moment."""
    t = parse_iso(created_at)
    lo = (t - timedelta(seconds=window_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (t + timedelta(seconds=window_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(
        "SELECT * FROM clips WHERE broadcaster_id = ? AND created_at BETWEEN ? AND ? "
        "AND clip_id != ? AND status NOT IN ('duplicate', 'skipped', 'failed')",
        (broadcaster_id, lo, hi, exclude_id),
    ).fetchall()


def uploads_since(conn, since: datetime, broadcaster_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM clips WHERE status = 'uploaded' AND uploaded_at >= ?"
    args = [since.strftime("%Y-%m-%dT%H:%M:%SZ")]
    if broadcaster_id:
        sql += " AND broadcaster_id = ?"
        args.append(broadcaster_id)
    return conn.execute(sql, args).fetchone()[0]


def last_upload_time(conn) -> datetime | None:
    row = conn.execute("SELECT MAX(uploaded_at) FROM clips WHERE status = 'uploaded'").fetchone()
    return parse_iso(row[0]) if row[0] else None


if __name__ == "__main__":
    conn = connect()
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        status = sys.argv[2] if len(sys.argv) > 2 else None
        sql = "SELECT * FROM clips" + (" WHERE status = ?" if status else "") + " ORDER BY updated_at DESC LIMIT 30"
        for r in conn.execute(sql, (status,) if status else ()):
            yt = f" yt={r['youtube_id']}" if r["youtube_id"] else ""
            err = f" err={r['error'][:60]!r}" if r["error"] else ""
            print(f"[{r['status']:<11}] {r['broadcaster_name']:<18} {r['view_count']:>6}v  {r['title'][:50]!r}{yt}{err}")
    else:
        rows = conn.execute("SELECT status, COUNT(*) FROM clips GROUP BY status ORDER BY 2 DESC").fetchall()
        print(f"DB: {DB_PATH}")
        for status, n in rows or [("(empty)", 0)]:
            print(f"  {status:<12} {n}")
