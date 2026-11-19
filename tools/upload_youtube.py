"""Upload a rendered Short to YouTube, with title/description/tags built from the clip + transcript.

    python tools/upload_youtube.py <video.mp4> --title "..." [--description "..."] [--privacy private] [--dry-run]

Requires token_youtube.json (run `python tools/google_auth.py youtube` once).
Quota: uploads have their own bucket of 100/day (1 unit each) as of June 2026.
"""

import argparse
import json
import re
from pathlib import Path

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from generate_title import generate_title
from google_auth import YOUTUBE_SCOPES, YOUTUBE_TOKEN_FILE, NeedsSignIn, get_service

GAMING_CATEGORY = "20"
# Clip titles that say nothing; fall back to a hook from the transcript instead.
GENERIC_TITLES = {"gta", "gta 6", "gta6", "gtavi", "gta vi", "grand theft auto vi", "clip", "lol", "lul", "omg", "w", "l"}


class QuotaExceeded(Exception):
    """Daily upload quota or channel upload limit hit — stop uploading until tomorrow."""


class AuthExpired(Exception):
    """YouTube sign-in expired or was revoked (every 7 days while the Google app is in Testing mode)."""


def _clean(text: str) -> str:
    text = re.sub(r"![\w-]+", " ", text)            # !commands from stream titles
    text = re.sub(r"#\w+", " ", text)               # hashtags
    text = re.sub(r"[^\w\s'?!.,&:()-]", " ", text)  # emojis / symbols
    return re.sub(r"\s+", " ", text).strip(" -|:,.")


def _rule_based_title(streamer: str, clip_title: str, words: list[str]) -> str:
    hook = _clean(clip_title or "")
    if len(hook) < 6 or hook.lower() in GENERIC_TITLES:
        # First full sentence of what was said (3-10 words) makes a natural hook.
        first = []
        for w in words[:10]:
            first.append(w)
            if len(first) >= 3 and w.endswith(("?", "!", ".")):
                break
        hook = _clean(" ".join(first)) if len(first) >= 3 else "You Won't Believe This"
        hook = hook[:1].upper() + hook[1:]

    title = hook if streamer.lower()[:4] in hook.lower() else f"{streamer}: {hook}"
    if "gta" not in title.lower():
        title += " | GTA 6"
    return title[:100]


def build_metadata(streamer: str, clip_title: str, clip_url: str, transcript_path: str | None,
                   video_path: Path | None = None, title: str | None = None, spoiler: bool = False) -> dict:
    """With video_path, Claude Haiku writes the title from frames + transcript; otherwise (or on failure) rule-based.
    Pass title to reuse one generated earlier (e.g. a retry after a failed upload) without paying again.
    spoiler=True prefixes the title with "SPOILER:" (main-story moments, flagged by safety_check)."""
    words = []
    if transcript_path and Path(transcript_path).exists():
        words = [w["word"] for w in json.loads(Path(transcript_path).read_text(encoding="utf-8"))["words"]]

    cost, note = 0.0, "no video"
    if title:
        note = "reused"
    elif video_path and Path(video_path).exists():
        title, cost, note = generate_title(Path(video_path), streamer, clip_title, " ".join(words))
    title_source = "haiku" if title and note == "ok" else "saved" if note == "reused" else f"rules ({note})"
    title = title or _rule_based_title(streamer, clip_title, words)
    if spoiler and not title.upper().startswith("SPOILER"):
        title = f"SPOILER: {title}"[:100]
    hook = title

    login = streamer.lower() if re.fullmatch(r"\w+", streamer) else None
    tag = re.sub(r"\W", "", streamer)
    description = "\n".join([
        hook,
        "",
        f"🎮 Streamer: {streamer}" + (f" — https://twitch.tv/{login}" if login else ""),
        f"📺 Original clip: {clip_url}",
        "",
        f"All credit to {streamer}. Go support them live!",
        "",
        f"#GTA6 #GTAVI #{tag} #Shorts",
    ])
    tags = ["GTA 6", "GTA VI", "Grand Theft Auto VI", "GTA 6 gameplay", streamer, f"{streamer} GTA 6", "Twitch clips"]
    return {"title": title, "description": description, "tags": tags, "title_source": title_source, "title_cost": cost}


def upload(video: Path, title: str, description: str, tags: list[str], privacy: str = "private") -> str:
    try:
        # interactive=False: an unattended upload must never pop a browser sign-in.
        youtube = get_service("youtube", "v3", YOUTUBE_SCOPES, YOUTUBE_TOKEN_FILE, interactive=False)
    except (RefreshError, NeedsSignIn) as e:
        raise AuthExpired(str(e)) from e
    body = {
        "snippet": {"title": title, "description": description, "tags": tags, "categoryId": GAMING_CATEGORY},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False},
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        response = None
        while response is None:
            _, response = request.next_chunk()
    except HttpError as e:
        reason = ""
        try:
            reason = json.loads(e.content)["error"]["errors"][0]["reason"]
        except Exception:
            pass
        if reason in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise QuotaExceeded(reason) from e
        raise
    return response["id"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--title", required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--tags", default="GTA 6,GTA VI")
    ap.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.dry_run:
        print(json.dumps({"video": str(args.video), "title": args.title, "privacy": args.privacy}, indent=2))
    else:
        vid = upload(args.video, args.title, args.description, args.tags.split(","), args.privacy)
        print(f"Uploaded: https://youtube.com/shorts/{vid}")
