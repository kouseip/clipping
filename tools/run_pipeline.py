"""The always-on clip agent: fetch -> download -> transcribe -> render -> upload, every poll_minutes.

    python tools/run_pipeline.py                    # loop forever (Ctrl+C to stop)
    python tools/run_pipeline.py --once             # one cycle, then exit
    python tools/run_pipeline.py --once --no-upload # render only (review files in .tmp/renders)
    python tools/run_pipeline.py --skip-fetch       # work through what's already queued

Pacing and filters live in config.json. Log: data/pipeline.log
"""

import argparse
import json
import logging
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clip_db
from _common import DATA_DIR, load_config
from download_clip import download
from hype_detector import start_background as start_hype
from fetch_twitch_clips import fetch
from google_auth import YOUTUBE_TOKEN_FILE
from plan_edit import plan_edit
from render_short import render
from safety_check import check_clip
from schedule import cap_period_start, hype_slots_left, in_upload_window
from track_shorts import run as track_shorts, summary as tracker_summary
from transcribe_clip import load_model, transcribe
from upload_youtube import AuthExpired, QuotaExceeded, build_metadata, upload

log = logging.getLogger("pipeline")
IN_PROGRESS = ("queued", "downloaded", "transcribed")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(), logging.FileHandler(DATA_DIR / "pipeline.log", encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def _safe_stem(clip_id: str) -> str:
    return re.sub(r"[^\w-]", "_", clip_id)[:80]


def _remove(path: str | None):
    if path:
        Path(path).unlink(missing_ok=True)


def expire_stale(conn, cfg):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=cfg["max_clip_age_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Reposts are days old by design, so they're exempt.
    n = conn.execute(
        f"UPDATE clips SET status = 'skipped', error = 'too old', updated_at = ? "
        f"WHERE status IN {IN_PROGRESS} AND created_at < ? AND COALESCE(source, '') != 'repost'",
        (clip_db.now_iso(), cutoff),
    ).rowcount
    conn.commit()
    if n:
        log.info(f"Skipped {n} clips older than {cfg['max_clip_age_hours']}h")

    # Hype clips are only worth it while the moment is fresh; drop them (and any rendered file) after max_age_hours.
    hype_hours = cfg.get("hype", {}).get("max_age_hours", 6)
    hype_cutoff = (datetime.now(timezone.utc) - timedelta(hours=hype_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = conn.execute(
        f"SELECT clip_id, source_path, transcript_path, render_path FROM clips WHERE source = 'hype' "
        f"AND status IN ('queued', 'downloaded', 'transcribed', 'rendered') AND created_at < ?", (hype_cutoff,),
    ).fetchall()
    for r in stale:
        clip_db.update(conn, r["clip_id"], status="skipped", error=f"stale hype clip (> {hype_hours}h)")
        for p in (r["source_path"], r["transcript_path"], r["render_path"]):
            _remove(p)
    if stale:
        log.info(f"Skipped {len(stale)} hype clips older than {hype_hours}h")




def by_priority(rows, hype_open: bool) -> list:
    """Fresh hype clips first (strongest chat spike first) while hype upload slots remain; then reposts of flops
    you deleted (oldest first); then most-viewed clips.
    With no hype slots left, hype clips are dropped from consideration so they don't clog the render backlog."""
    hype = sorted((r for r in rows if r["source"] == "hype"), key=lambda r: -(r["hype_score"] or 0))
    reposts = sorted((r for r in rows if r["source"] == "repost"), key=lambda r: r["updated_at"] or "")
    rest = sorted((r for r in rows if r["source"] not in ("hype", "repost")), key=lambda r: -(r["view_count"] or 0))
    return (hype if hype_open else []) + reposts + rest








def streamer_capped(conn, cfg, broadcaster_id: str) -> bool:
    return clip_db.uploads_since(conn, cap_period_start(cfg), broadcaster_id) >= cfg["max_uploads_per_streamer_per_day"]


def process(conn, cfg):
    """Render just enough clips to keep a small ready-to-upload backlog (no wasted GPU time or disk).
    Hype clips get their own slot (one rendered and waiting at a time) so a full regular backlog
    never makes a fresh moment wait."""
    hype_open = hype_slots_left(conn, cfg) > 0
    rendered = [r["source"] for r in conn.execute("SELECT source FROM clips WHERE status = 'rendered'")]
    slots = {
        "regular": cfg["render_batch_size"] - sum(s != "hype" for s in rendered),
        "hype": (1 - rendered.count("hype")) if hype_open else 0,
    }
    if max(slots.values()) <= 0:
        return
    rows = by_priority(conn.execute(f"SELECT * FROM clips WHERE status IN {IN_PROGRESS}").fetchall(), hype_open)
    for row in rows:
        kind = "hype" if row["source"] == "hype" else "regular"
        if slots[kind] <= 0:
            continue
        if streamer_capped(conn, cfg, row["broadcaster_id"]):
            continue
        cid, stem = row["clip_id"], _safe_stem(row["clip_id"])
        try:
            if row["status"] == "queued":
                src = download(row["url"], stem)
                clip_db.update(conn, cid, status="downloaded", source_path=str(src))
                row = clip_db.get(conn, cid)
            if row["status"] == "downloaded":
                tr = transcribe(Path(row["source_path"]), row["language"] or None, cfg["whisper_model"])
                clip_db.update(conn, cid, status="transcribed", transcript_path=str(tr))
                row = clip_db.get(conn, cid)
            if row["status"] == "transcribed" and cfg.get("safety_check", True) and row["safety"] != "ok":
                text = " ".join(w["word"] for w in json.loads(Path(row["transcript_path"]).read_text(encoding="utf-8"))["words"])
                verdict, reason, cost, spoiler = check_clip(Path(row["source_path"]), text)
                if verdict is None:
                    # Check couldn't run (API down, no key): hold everything rather than render unchecked clips.
                    log.warning(f"Safety check unavailable ({reason}); holding clips until next cycle")
                    return
                if verdict == "reject":
                    clip_db.update(conn, cid, status="skipped", error=f"safety: {reason}")
                    _remove(row["source_path"])
                    _remove(row["transcript_path"])
                    log.info(f"Rejected  {row['broadcaster_name']} ({row['view_count']}v): {row['title']!r} [${cost:.4f}] {reason}")
                    continue
                clip_db.update(conn, cid, safety="ok", spoiler=int(spoiler))
                row = clip_db.get(conn, cid)
            if row["status"] == "transcribed" and cfg.get("auto_edit", True) and row["trim_end"] is None:
                words = json.loads(Path(row["transcript_path"]).read_text(encoding="utf-8"))["words"]
                recent = [r[0] for r in conn.execute(
                    "SELECT hook FROM clips WHERE hook IS NOT NULL ORDER BY updated_at DESC LIMIT 10")]
                before = conn.execute("SELECT title, hook FROM shorts WHERE clip_id = ?", (cid,)).fetchall()
                recent += [b["hook"] for b in before if b["hook"]]
                plan = plan_edit(Path(row["source_path"]), words, row["broadcaster_name"], row["title"],
                                 cfg.get("edit_max_seconds", 20), recent_hooks=recent,
                                 previous_titles=[b["title"] for b in before if b["title"]])
                clip_db.update(conn, cid, trim_start=plan.start, trim_end=plan.end, hook=plan.hook,
                               tags=json.dumps(plan.tags), yt_title=plan.title,
                               moment_score=plan.score, moment=plan.what_happens)
                min_score = cfg.get("min_moment_score", 6)
                if plan.score is not None and plan.score < min_score:
                    # Not obvious what happens (talking to chat, menus, inside joke...): don't spend a slot on it.
                    clip_db.update(conn, cid, status="skipped", error=f"not a clear moment ({plan.score}/10): {plan.what_happens}")
                    _remove(row["source_path"])
                    _remove(row["transcript_path"])
                    log.info(f"Passed on {row['broadcaster_name']} [{plan.score}/10, ${plan.cost:.4f}]: {plan.what_happens}")
                    continue
                log.info(f"Edit plan [{plan.note}, ${plan.cost:.4f}] {plan.score}/10 {plan.what_happens!r} "
                         f"{plan.start:.1f}-{plan.end:.1f}s hook={plan.hook!r} title={plan.title!r}")
                row = clip_db.get(conn, cid)
            if row["status"] == "transcribed":
                out = render(Path(row["source_path"]), Path(row["transcript_path"]), row["broadcaster_name"],
                             start=row["trim_start"] or 0.0, end=row["trim_end"], hook=row["hook"])
                clip_db.update(conn, cid, status="rendered", render_path=str(out))
                _remove(row["source_path"])
                label = (f"hype x{row['hype_score']}" if kind == "hype" else
                         "repost" if row["source"] == "repost" else f"{row['view_count']}v")
                log.info(f"Rendered  {row['broadcaster_name']} ({label}): {row['title']!r} -> {out.name}")
                slots[kind] -= 1
        except Exception as e:
            log.warning(f"Failed    {cid} at {row['status']}: {e.__class__.__name__}: {e}")
            clip_db.fail(conn, cid, f"{e.__class__.__name__}: {e}")


def burst_active(cfg) -> bool:
    """A temporary "upload_burst" in config.json (it expires on its own at "until", PC local time)."""
    burst = cfg.get("upload_burst") or {}
    if not burst.get("until"):
        return False
    until = datetime.fromisoformat(burst["until"])
    return (datetime.now(until.tzinfo) if until.tzinfo else datetime.now()) < until


def upload_spacing_minutes(cfg) -> int:
    return cfg["upload_burst"]["min_minutes_between_uploads"] if burst_active(cfg) else cfg["min_minutes_between_uploads"]


def try_upload(conn, cfg) -> bool:
    """Upload at most one clip per cycle, respecting the upload window, daily caps and spacing.
    Returns False if YouTube's quota is exhausted."""
    now = datetime.now(timezone.utc)
    if not burst_active(cfg) and not in_upload_window(cfg):
        return True
    if clip_db.uploads_since(conn, cap_period_start(cfg)) >= cfg["max_uploads_per_day"]:
        return True
    last = clip_db.last_upload_time(conn)
    if last and now - last < timedelta(minutes=upload_spacing_minutes(cfg)):
        return True

    rendered = conn.execute("SELECT * FROM clips WHERE status = 'rendered'").fetchall()
    for row in by_priority(rendered, hype_open=hype_slots_left(conn, cfg) > 0):
        if streamer_capped(conn, cfg, row["broadcaster_id"]):
            continue
        if cfg.get("safety_check", True) and row["safety"] != "ok":
            # Rendered before the check existed (or check was off): check the finished Short itself.
            verdict, reason, cost, spoiler = check_clip(Path(row["render_path"]))
            if verdict is None:
                log.warning(f"Safety check unavailable ({reason}); not uploading this cycle")
                return True
            if verdict == "reject":
                clip_db.update(conn, row["clip_id"], status="skipped", error=f"safety: {reason}")
                _remove(row["render_path"])
                _remove(row["transcript_path"])
                log.info(f"Rejected  {row['broadcaster_name']} ({row['view_count']}v): {row['title']!r} [${cost:.4f}] {reason}")
                continue
            clip_db.update(conn, row["clip_id"], safety="ok", spoiler=int(spoiler))
            row = clip_db.get(conn, row["clip_id"])
        meta = build_metadata(row["broadcaster_name"], row["title"], row["url"], row["transcript_path"],
                              video_path=Path(row["render_path"]), title=row["yt_title"], spoiler=bool(row["spoiler"]),
                              clip_tags=json.loads(row["tags"]) if row["tags"] else None)
        if meta["title_source"] == "haiku":
            clip_db.update(conn, row["clip_id"], yt_title=meta["title"])  # a retry reuses it instead of paying again
        if meta["title_source"] != "saved":
            log.info(f"Title     [{meta['title_source']}, ${meta['title_cost']:.4f}] {meta['title']!r}")
        try:
            vid = upload(Path(row["render_path"]), meta["title"], meta["description"], meta["tags"], cfg["youtube_privacy"])
        except QuotaExceeded as e:
            # YouTube doesn't publish the per-channel cap; this log line is how we learn it.
            n = clip_db.uploads_since(conn, now - timedelta(days=1))
            log.warning(f"YouTube upload limit hit ({e}) after {n} uploads in the last 24h; pausing uploads 12h")
            return False
        except AuthExpired:
            # Not the clip's fault — keep it rendered and retry next cycle once the user signs in again.
            log.warning("YouTube sign-in expired: run `python tools/google_auth.py youtube`. Clips stay queued.")
            return True
        except Exception as e:
            log.warning(f"Upload failed for {row['clip_id']}: {e.__class__.__name__}: {e}")
            clip_db.fail(conn, row["clip_id"], f"upload: {e}")
            return True
        clip_db.update(conn, row["clip_id"], status="uploaded", youtube_id=vid, yt_title=meta["title"], uploaded_at=clip_db.now_iso())
        _remove(row["render_path"])
        _remove(row["transcript_path"])
        kind = ", repost" if row["source"] == "repost" else ""
        log.info(f"Uploaded  [{cfg['youtube_privacy']}{kind}] https://youtube.com/shorts/{vid}  {meta['title']!r}")
        return True
    return True


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def keep_awake(on: bool):
    """Block Windows idle sleep while uploads are due (the PC sleeps after 5h idle on AC). The screen can still
    turn off and manual sleep still works; outside the upload window normal sleep is allowed again."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


ALREADY_RUNNING_EXIT = 3  # start_clipper.bat checks for this so a duplicate window just closes


def claim_single_instance() -> socket.socket | None:
    """Two agents on one DB would process and upload the same clips. Holding a localhost port is a lock
    the OS releases automatically if the process dies. Test runs against a scratch DB (CLIP_DB) skip it."""
    if os.getenv("CLIP_DB"):
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 47823))
    except OSError:
        print("Clip agent is already running in another window; exiting.")
        sys.exit(ALREADY_RUNNING_EXIT)
    return sock


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true")
    args = ap.parse_args()
    _lock = claim_single_instance()  # noqa: F841 — keep the socket open for the life of the process
    setup_logging()

    cfg = load_config()
    load_model(cfg["whisper_model"])  # load once up front; stays on the GPU between cycles
    uploads_paused_until = None
    hype_thread = None
    run_hype = not args.once and not os.getenv("CLIP_DB")  # never from one-off or scratch-DB test runs
    run_tracker = not os.getenv("CLIP_DB")  # a scratch DB would overwrite the real sheet with test rows
    last_track = 0.0

    while True:
        cfg = load_config()  # pick up config.json edits without restarting
        if not args.once:
            keep_awake(burst_active(cfg) or in_upload_window(cfg))
        if run_hype and (hype_thread is None or not hype_thread.is_alive()):
            hype_thread = start_hype(cfg)  # also picks up a Twitch sign-in done after startup
        conn = clip_db.connect()
        try:
            if not args.skip_fetch:
                s = fetch()
                log.info(f"Fetch: {s['fetched']} clips, {s['candidates']} passed filters, {s['new']} new, {s['duplicate']} dupes")
            expire_stale(conn, cfg)
            process(conn, cfg)

            if args.no_upload:
                pass
            elif not YOUTUBE_TOKEN_FILE.exists():
                log.warning("YouTube not authorized yet: run `python tools/google_auth.py youtube`. Rendering only.")
            elif uploads_paused_until and datetime.now(timezone.utc) < uploads_paused_until:
                pass
            elif not try_upload(conn, cfg):
                uploads_paused_until = datetime.now(timezone.utc) + timedelta(hours=12)

            tcfg = cfg.get("tracker", {})
            if run_tracker and tcfg.get("enabled") and time.time() - last_track >= tcfg.get("refresh_minutes", 60) * 60:
                last_track = time.time()
                for note in track_shorts(conn, cfg):
                    # Everything else the tracker reports is a flop to delete or a problem to fix.
                    (log.info if "repost queued" in note else log.warning)(note)
                log.info(f"Tracker: {tracker_summary(conn)}")
        except Exception as e:  # never let one bad cycle kill the agent
            log.exception(f"Cycle error: {e}")
        finally:
            conn.close()

        if args.once:
            break
        wait_for_next_cycle(cfg)


def wait_for_next_cycle(cfg):
    """Sleep until the next cycle, but wake early when the hype detector queues a fresh clip."""
    started = time.time()
    deadline = started + cfg["poll_minutes"] * 60
    while time.time() < deadline:
        time.sleep(30)
        if time.time() - started < 120:  # never cycle more often than every 2 minutes
            continue
        conn = clip_db.connect()
        try:
            counts = dict(conn.execute(
                "SELECT status, COUNT(*) FROM clips WHERE source = 'hype' AND status IN ('queued', 'rendered') GROUP BY status"
            ).fetchall())
            # Only worth waking if a fresh hype clip can actually be rendered now (its slot is free).
            if counts.get("queued") and not counts.get("rendered") and hype_slots_left(conn, cfg) > 0:
                return
        finally:
            conn.close()


if __name__ == "__main__":
    main()
