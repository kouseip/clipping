"""Google OAuth helper shared by every tool that talks to Sheets, Slides, Drive, or YouTube.

First-time setup (once per token):
    python tools/google_auth.py            # Sheets / Slides / Drive  -> token.json   (your main Google account)
    python tools/google_auth.py youtube    # YouTube uploads          -> token_youtube.json  (pick hotmicclipsYT)
    python tools/google_auth.py youtube --force   # fresh sign-in even if the token still works
    python tools/google_auth.py renew      # both, fresh (Testing-mode apps: do this every <7 days)
Each opens a browser for consent. Later runs refresh the token silently.
YouTube gets its own token so the channel's Google account can differ from your Sheets account.

In other tools:
    from google_auth import get_service, YOUTUBE_SCOPES, YOUTUBE_TOKEN_FILE
    sheets = get_service("sheets", "v4")
    youtube = get_service("youtube", "v3", YOUTUBE_SCOPES, YOUTUBE_TOKEN_FILE)
"""

import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from _common import ROOT, get_env

# drive.file = only files this app creates or opens, which is narrower than full Drive access.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive.file",
]
# readonly: confirm which channel the token is bound to, and read view counts for the scoreboard.
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

CREDENTIALS_FILE = ROOT / get_env("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = ROOT / get_env("GOOGLE_TOKEN_FILE", "token.json")
YOUTUBE_TOKEN_FILE = ROOT / get_env("YOUTUBE_TOKEN_FILE", "token_youtube.json")


class NeedsSignIn(Exception):
    """No usable token and interactive sign-in isn't allowed (e.g. inside the unattended pipeline)."""


def get_credentials(scopes: list[str] = SCOPES, token_file: Path = TOKEN_FILE, interactive: bool = True,
                    force: bool = False) -> Credentials:
    """force=True always runs the browser sign-in, minting a new refresh token (restarts Testing mode's 7-day clock)."""
    creds = None
    if token_file.exists() and not force:
        info = json.loads(token_file.read_text())
        # A token minted with fewer scopes than requested can't be refreshed into a wider one.
        # Compare against the file's saved scopes: Credentials(scopes=...) just echoes back what you pass in.
        if set(scopes) <= set(info.get("scopes") or []):
            creds = Credentials.from_authorized_user_info(info, scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not interactive:
            raise NeedsSignIn(f"{token_file.name} missing or lacks required scopes")
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"{CREDENTIALS_FILE} not found. Download an OAuth 'Desktop app' client from "
                "Google Cloud Console > APIs & Services > Credentials and save it there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), scopes)
        creds = flow.run_local_server(port=0)

    Path(token_file).write_text(creds.to_json())
    return creds


def get_service(api: str, version: str, scopes: list[str] = SCOPES, token_file: Path = TOKEN_FILE, interactive: bool = True):
    """Build an authorized Google API client, e.g. get_service("sheets", "v4")."""
    creds = get_credentials(scopes, token_file, interactive)
    return build(api, version, credentials=creds, cache_discovery=False)


def _youtube(force: bool):
    creds = get_credentials(YOUTUBE_SCOPES, YOUTUBE_TOKEN_FILE, force=force)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    print(f"YouTube authorized. Token saved to {YOUTUBE_TOKEN_FILE}")
    for ch in yt.channels().list(part="snippet", mine=True).execute().get("items", []):
        print(f"Uploads will go to channel: {ch['snippet']['title']}  (https://youtube.com/channel/{ch['id']})")


def _sheets(force: bool):
    get_credentials(force=force)
    print(f"Sheets authorized. Token saved to {TOKEN_FILE}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else ""
    force = "--force" in sys.argv
    if mode == "youtube":
        _youtube(force)
    elif mode == "renew":
        print("1/2  YouTube: pick the hotmicclipsYT channel")
        _youtube(True)
        print("2/2  Sheets: pick your main Google account")
        _sheets(True)
    else:
        _sheets(force)
