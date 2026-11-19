"""Download a Twitch clip (or any yt-dlp-supported URL) to .tmp/clips/.

    python tools/download_clip.py <clip_url> [--out NAME]
"""

import argparse
import time
from pathlib import Path

import yt_dlp

from _common import TMP_DIR

CLIPS_DIR = TMP_DIR / "clips"


def download(url: str, name: str) -> Path:
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    out = CLIPS_DIR / f"{name}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    opts = {
        "outtmpl": str(CLIPS_DIR / f"{name}.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
    }
    # A freshly created clip (hype detector) shows up in the API before its video file is ready -> 404 for a bit.
    for attempt in range(5):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = Path(ydl.prepare_filename(info))
            break
        except yt_dlp.utils.DownloadError as e:
            if "404" not in str(e) or attempt == 4:
                raise
            time.sleep(15)
    if path != out:
        path.replace(out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default="test_clip")
    args = ap.parse_args()
    print(download(args.url, args.out))
