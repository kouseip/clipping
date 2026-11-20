"""Shorts tracker: views and likes for every uploaded Short, and which flops to delete and repost.

Shorts sometimes just miss the algorithm. The fix from the growth video: if a Short hasn't passed ~100 views
after 2 days public, delete it and re-upload it on a different day with a new title. This tool:
  1. reads each uploaded Short's stats from YouTube (videos.list: 1 quota unit per 50 videos, free),
  2. snapshots views at 12/24/48/72 hours after it went public,
  3. marks flops "DELETE + repost" (under tracker.min_views after tracker.judge_after_hours; first uploads only),
  4. when you delete a flagged Short in YouTube Studio, requeues its clip: the pipeline downloads it again and
     uploads it with a fresh title and hook. Only once; a repost that flops too is left alone.
  5. writes everything to a Google Sheet in your Drive (tabs "Shorts" and "By streamer").

    python tools/track_shorts.py              # refresh now and print the table
    python tools/track_shorts.py --no-sheet   # skip Google Sheets

run_pipeline.py runs this every tracker.refresh_minutes. Sheets needs a one-time sign-in with your main Google
account: python tools/google_auth.py
"""

import argparse
import json
from datetime import datetime, timezone

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

import clip_db
from _common import DATA_DIR, load_config
from google_auth import SCOPES, TOKEN_FILE, YOUTUBE_SCOPES, YOUTUBE_TOKEN_FILE, NeedsSignIn, get_service

SHEET_FILE = DATA_DIR / "tracker.json"  # remembers which spreadsheet is ours
SHEET_TITLE = "hotmicclipsYT Shorts tracker"
SNAPSHOT_HOURS = (12, 24, 48, 72)
DELETE = "DELETE + repost"
HEADERS = ["Uploaded", "Streamer", "Title", "Short", "Status", "Hours public", "Views", "Likes", "Comments",
           "Views @12h", "Views @24h", "Views @48h", "Views @72h", "Spoiler", "Repost", "Verdict"]
STREAMER_HEADERS = ["Streamer", "Shorts", "Public", "Total views", "Avg views (public)", "Best Short", "Best views"]


def _settings(cfg) -> dict:
    t = cfg.get("tracker", {})
    return {"judge_after_hours": t.get("judge_after_hours", 48), "min_views": t.get("min_views", 100),
            "auto_repost": t.get("auto_repost", True)}


def sync_new_uploads(conn) -> int:
    """Add every uploaded clip that isn't tracked yet, and link a repost to the Short it replaces."""
    rows = conn.execute(
        "SELECT * FROM clips WHERE status = 'uploaded' AND youtube_id IS NOT NULL "
        "AND youtube_id NOT IN (SELECT youtube_id FROM shorts)").fetchall()
    for c in rows:
        conn.execute(
            "INSERT INTO shorts (youtube_id, clip_id, streamer, title, hook, source, spoiler, repost_no, uploaded_at, "
            "verdict) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (c["youtube_id"], c["clip_id"], c["broadcaster_name"], c["yt_title"], c["hook"], c["source"], c["spoiler"],
             c["reposts"] or 0, c["uploaded_at"], "New"))
        if c["reposts"]:
            conn.execute("UPDATE shorts SET reposted_as = ?, verdict = 'Reposted (see newer row)' "
                         "WHERE clip_id = ? AND youtube_id != ? AND reposted_as IS NULL",
                         (c["youtube_id"], c["clip_id"], c["youtube_id"]))
    conn.commit()
    return len(rows)


def fetch_stats(youtube, ids: list[str]) -> dict:
    """{video_id: item} for the videos YouTube still has. Deleted videos are simply missing from the response."""
    found = {}
    for i in range(0, len(ids), 50):
        resp = youtube.videos().list(part="snippet,statistics,status", id=",".join(ids[i:i + 50]),
                                     maxResults=50).execute()
        found.update({item["id"]: item for item in resp.get("items", [])})
    return found


def verdict_for(s: dict, hours: float | None, cfg) -> str:
    st = _settings(cfg)
    if s["privacy"] != "public":
        return "Private (not judged yet)" if s["privacy"] == "private" else "Not public (not judged yet)"
    if (s["views"] or 0) >= st["min_views"]:
        return "Keep"
    if hours is None or hours < st["judge_after_hours"]:
        left = st["judge_after_hours"] - (hours or 0)
        return f"Waiting ({left:.0f}h left)"
    return DELETE if not s["repost_no"] else "Flopped twice (leave it)"


def requeue_for_repost(conn, short) -> bool:
    """Send the clip behind a deleted flop back through the pipeline. The old title and hook stay in the shorts
    table so plan_edit can write different ones. Keeps the safety verdict, so the check isn't paid for twice."""
    clip = clip_db.get(conn, short["clip_id"])
    if not clip or clip["status"] != "uploaded" or clip["youtube_id"] != short["youtube_id"]:
        return False
    clip_db.update(conn, clip["clip_id"], status="queued", source="repost", reposts=(clip["reposts"] or 0) + 1,
                   youtube_id=None, yt_title=None, uploaded_at=None, trim_start=None, trim_end=None, hook=None,
                   source_path=None, transcript_path=None, render_path=None, error=None, attempts=0)
    return True


def refresh(conn, cfg, youtube) -> list[str]:
    """Update every tracked Short from YouTube. Returns log lines worth showing (new flags, requeued reposts)."""
    st = _settings(cfg)
    notes = []
    shorts = conn.execute("SELECT * FROM shorts").fetchall()
    if not shorts:
        return notes
    found = fetch_stats(youtube, [s["youtube_id"] for s in shorts])
    now = datetime.now(timezone.utc)
    for s in shorts:
        s = dict(s)
        item = found.get(s["youtube_id"])
        url = f"https://youtube.com/shorts/{s['youtube_id']}"
        if item is None:
            if s["state"] != "deleted":
                s["state"] = "deleted"
                if s["flagged"] and st["auto_repost"] and requeue_for_repost(conn, s):
                    s["verdict"] = "Deleted, repost queued"
                    notes.append(f"Tracker: you deleted {url} ({s['streamer']}); repost queued with a new title")
                else:
                    s["verdict"] = "Deleted"
            conn.execute("UPDATE shorts SET state = ?, verdict = ?, checked_at = ? WHERE youtube_id = ?",
                         (s["state"], s["verdict"], clip_db.now_iso(), s["youtube_id"]))
            continue

        status, stats = item.get("status", {}), item.get("statistics", {})
        s["privacy"] = status.get("privacyStatus")
        s["views"] = int(stats.get("viewCount", 0))
        s["likes"] = int(stats.get("likeCount", 0))
        s["comments"] = int(stats.get("commentCount", 0))
        s["title"] = item.get("snippet", {}).get("title") or s["title"]
        if s["privacy"] == "public" and not s["public_since"]:
            # First time we see it public. Erring late only delays a verdict, never deletes early.
            s["public_since"] = clip_db.now_iso()
        elif s["privacy"] != "public":
            s["public_since"] = None  # pulled back to private: the 2-day clock restarts if it goes public again
        hours = (now - clip_db.parse_iso(s["public_since"])).total_seconds() / 3600 if s["public_since"] else None
        for h in SNAPSHOT_HOURS:
            if hours is not None and hours >= h and s[f"views_{h}h"] is None:
                s[f"views_{h}h"] = s["views"]

        if status.get("uploadStatus") in ("rejected", "failed", "deleted"):
            s["state"] = "removed"
            s["verdict"] = f"Removed by YouTube ({status.get('rejectionReason') or status.get('failureReason') or status['uploadStatus']})"
        elif s["reposted_as"]:
            s["state"] = "live"
            s["verdict"] = "Reposted (see newer row)"
        else:
            s["state"] = "live"
            s["verdict"] = verdict_for(s, hours, cfg)
            if s["verdict"] == DELETE and not s["flagged"]:
                s["flagged"] = 1
                notes.append(f"Tracker: {DELETE}: {url} ({s['streamer']}, {s['views']} views after "
                             f"{hours:.0f}h). Delete it in YouTube Studio and the agent reposts it.")
        conn.execute(
            "UPDATE shorts SET title = ?, privacy = ?, state = ?, views = ?, likes = ?, comments = ?, public_since = ?, "
            "views_12h = ?, views_24h = ?, views_48h = ?, views_72h = ?, flagged = ?, verdict = ?, checked_at = ? "
            "WHERE youtube_id = ?",
            (s["title"], s["privacy"], s["state"], s["views"], s["likes"], s["comments"], s["public_since"],
             s["views_12h"], s["views_24h"], s["views_48h"], s["views_72h"], s["flagged"], s["verdict"],
             clip_db.now_iso(), s["youtube_id"]))
    conn.commit()
    return notes


# ---------- Google Sheet ----------

def _local(ts: str | None) -> str:
    if not ts:
        return ""
    return clip_db.parse_iso(ts).astimezone().strftime("%b %d %I:%M %p").replace(" 0", " ")


def _text(v: str | None) -> str:
    """Sheets would run a title starting with = + - @ as a formula."""
    v = v or ""
    return "'" + v if v[:1] in "=+-@" else v


def _hours_public(s) -> str:
    if not s["public_since"]:
        return ""
    return f"{(datetime.now(timezone.utc) - clip_db.parse_iso(s['public_since'])).total_seconds() / 3600:.0f}"


def _create_sheet(sheets) -> str:
    resp = sheets.spreadsheets().create(body={
        "properties": {"title": SHEET_TITLE},
        "sheets": [{"properties": {"title": "Shorts", "gridProperties": {"frozenRowCount": 1}}},
                   {"properties": {"title": "By streamer", "gridProperties": {"frozenRowCount": 1}}}],
    }).execute()
    sid = resp["spreadsheetId"]
    shorts_tab, streamer_tab = (t["properties"]["sheetId"] for t in resp["sheets"])
    verdict_col = HEADERS.index("Verdict")

    def width(tab, col, px):
        return {"updateDimensionProperties": {"range": {"sheetId": tab, "dimension": "COLUMNS", "startIndex": col,
                "endIndex": col + 1}, "properties": {"pixelSize": px}, "fields": "pixelSize"}}

    def highlight(text, rgb):
        return {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": shorts_tab, "startRowIndex": 1, "startColumnIndex": verdict_col,
                        "endColumnIndex": verdict_col + 1}],
            "booleanRule": {"condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": text}]},
                            "format": {"backgroundColor": rgb, "textFormat": {"bold": True}}}}}}

    def bold_header(tab):
        return {"repeatCell": {"range": {"sheetId": tab, "startRowIndex": 0, "endRowIndex": 1},
                               "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                               "fields": "userEnteredFormat.textFormat.bold"}}

    sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
        bold_header(shorts_tab),
        bold_header(streamer_tab),
        width(shorts_tab, HEADERS.index("Title"), 420),
        width(shorts_tab, verdict_col, 220),
        width(streamer_tab, STREAMER_HEADERS.index("Best Short"), 420),
        highlight("DELETE", {"red": 0.96, "green": 0.78, "blue": 0.76}),
        highlight("Keep", {"red": 0.80, "green": 0.92, "blue": 0.80}),
    ]}).execute()
    SHEET_FILE.write_text(json.dumps({"spreadsheet_id": sid}))
    return sid


def _sheet_id(sheets) -> str:
    if SHEET_FILE.exists():
        sid = json.loads(SHEET_FILE.read_text()).get("spreadsheet_id")
        if sid:
            return sid
    return _create_sheet(sheets)


def write_sheet(conn) -> str:
    """Rewrite both tabs from the shorts table. Returns the spreadsheet URL."""
    sheets = get_service("sheets", "v4", SCOPES, TOKEN_FILE, interactive=False)
    try:
        sid = _sheet_id(sheets)
        sheets.spreadsheets().get(spreadsheetId=sid, fields="spreadsheetId").execute()
    except HttpError as e:
        if e.resp.status != 404:
            raise
        sid = _create_sheet(sheets)  # the sheet was deleted from Drive: make a new one

    shorts = conn.execute("SELECT * FROM shorts ORDER BY uploaded_at DESC").fetchall()
    updated = "Updated " + _local(clip_db.now_iso())
    rows = [HEADERS + [updated]]
    for s in shorts:
        rows.append([
            _local(s["uploaded_at"]), s["streamer"], _text(s["title"]),
            f'=HYPERLINK("https://youtube.com/shorts/{s["youtube_id"]}", "watch")',
            "Deleted" if s["state"] == "deleted" else "Removed" if s["state"] == "removed" else (s["privacy"] or "").capitalize(),
            _hours_public(s), s["views"] if s["views"] is not None else "", s["likes"] if s["likes"] is not None else "",
            s["comments"] if s["comments"] is not None else "",
            *[s[f"views_{h}h"] if s[f"views_{h}h"] is not None else "" for h in SNAPSHOT_HOURS],
            "yes" if s["spoiler"] else "", "repost" if s["repost_no"] else "", s["verdict"] or "",
        ])

    by = {}
    for s in shorts:
        b = by.setdefault(s["streamer"], {"n": 0, "public": 0, "views": 0, "best": None})
        b["n"] += 1
        if s["privacy"] == "public" and s["state"] != "deleted":
            b["public"] += 1
            b["views"] += s["views"] or 0
            if b["best"] is None or (s["views"] or 0) > (b["best"]["views"] or 0):
                b["best"] = s
    srows = [STREAMER_HEADERS]
    for name, b in sorted(by.items(), key=lambda kv: -(kv[1]["views"] / kv[1]["public"] if kv[1]["public"] else -1)):
        srows.append([name, b["n"], b["public"], b["views"], round(b["views"] / b["public"]) if b["public"] else "",
                      _text(b["best"]["title"]) if b["best"] else "", b["best"]["views"] if b["best"] else ""])

    values = sheets.spreadsheets().values()
    values.batchClear(spreadsheetId=sid, body={"ranges": ["Shorts", "By streamer"]}).execute()
    values.batchUpdate(spreadsheetId=sid, body={"valueInputOption": "USER_ENTERED", "data": [
        {"range": "Shorts!A1", "values": rows}, {"range": "'By streamer'!A1", "values": srows}]}).execute()
    return f"https://docs.google.com/spreadsheets/d/{sid}"


def run(conn, cfg, sheet: bool = True) -> list[str]:
    """One tracker pass for run_pipeline.py. Returns log lines; never raises for sign-in problems."""
    notes = []
    sync_new_uploads(conn)
    try:
        youtube = get_service("youtube", "v3", YOUTUBE_SCOPES, YOUTUBE_TOKEN_FILE, interactive=False)
        notes += refresh(conn, cfg, youtube)
    except (RefreshError, NeedsSignIn):
        notes.append("Tracker: YouTube sign-in expired, stats not refreshed: run `python tools/google_auth.py renew`")
    except HttpError as e:
        notes.append(f"Tracker: YouTube stats not refreshed: {_api_error(e)}")
    if sheet:
        try:
            write_sheet(conn)
        except (RefreshError, NeedsSignIn):
            notes.append("Tracker: Google Sheets not signed in (or expired), sheet not updated: "
                         "run `python tools/google_auth.py`")
        except HttpError as e:
            notes.append(f"Tracker: sheet not updated: {_api_error(e)}")
    return notes


def _api_error(e: HttpError) -> str:
    """One readable line instead of a traceback (e.g. the Sheets API not being enabled in the Cloud project)."""
    try:
        err = json.loads(e.content)["error"]
        if any(d.get("reason") == "SERVICE_DISABLED" for d in err.get("details", [])):
            return (f"the API is turned off in the Google Cloud project; enable it at "
                    f"{next(d['metadata']['activationUrl'] for d in err['details'] if d.get('reason') == 'SERVICE_DISABLED')}")
        return f"HTTP {e.resp.status}: {err.get('message', '')[:200]}"
    except Exception:
        return f"HTTP {e.resp.status}"


def summary(conn) -> str:
    counts = {}
    for (v,) in conn.execute("SELECT verdict FROM shorts"):
        key = (v or "New").split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{n} {k.lower()}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])) or "nothing uploaded yet"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-sheet", action="store_true")
    args = ap.parse_args()
    conn = clip_db.connect()
    for line in run(conn, load_config(), sheet=not args.no_sheet):
        print(line)
    for s in conn.execute("SELECT * FROM shorts ORDER BY uploaded_at DESC"):
        views = "-" if s["views"] is None else s["views"]
        likes = "-" if s["likes"] is None else s["likes"]
        print(f"{_local(s['uploaded_at']):<16} {s['streamer']:<16} {views:>6}v {likes:>4}l  {s['verdict'] or '':<26} "
              f"{(s['title'] or '')[:55]}")
    print(f"Summary: {summary(conn)}")
    if SHEET_FILE.exists() and not args.no_sheet:
        print(f"Sheet: https://docs.google.com/spreadsheets/d/{json.loads(SHEET_FILE.read_text())['spreadsheet_id']}")
