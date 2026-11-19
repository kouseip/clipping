"""Twitch *user* sign-in (needed to create clips; reading chat and clip lists only needs the app token).

Uses Twitch's Device Code flow: the script prints a link, you approve it on twitch.tv/activate, done.
The token (scope clips:edit) is saved to token_twitch.json (gitignored) and refreshed automatically.

    python tools/twitch_user_auth.py          # sign in (or show who is signed in)
    python tools/twitch_user_auth.py --force  # sign in again

Clips created with this token appear publicly on the streamer's channel under your Twitch name.
"""

import json
import sys
import time
import webbrowser

import requests

from _common import ROOT, get_env

TOKEN_FILE = ROOT / "token_twitch.json"
SCOPES = "clips:edit"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"


class TwitchNotSignedIn(Exception):
    pass


def _save(body: dict):
    data = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": time.time() + body.get("expires_in", 3600),
        "scope": body.get("scope", []),
    }
    TOKEN_FILE.write_text(json.dumps(data, indent=1))


def login() -> None:
    client_id = get_env("TWITCH_CLIENT_ID")
    d = requests.post(DEVICE_URL, data={"client_id": client_id, "scopes": SCOPES}, timeout=30)
    d.raise_for_status()
    d = d.json()
    print(f"Open {d['verification_uri']} and approve the request (code {d['user_code']}).", flush=True)
    try:
        webbrowser.open(d["verification_uri"])
    except Exception:
        pass
    interval, deadline = d.get("interval", 5), time.time() + d.get("expires_in", 1800)
    while time.time() < deadline:
        time.sleep(interval)
        r = requests.post(TOKEN_URL, timeout=30, data={
            "client_id": client_id,
            "client_secret": get_env("TWITCH_CLIENT_SECRET"),
            "scopes": SCOPES,
            "device_code": d["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if r.status_code == 200:
            _save(r.json())
            return
        msg = r.json().get("message", "")
        if msg == "authorization_pending":
            continue
        if msg == "slow_down":
            interval += 5
            continue
        raise RuntimeError(f"Twitch sign-in failed: {r.status_code} {msg}")
    raise TimeoutError("Twitch sign-in timed out (30 min).")


def _refresh(data: dict) -> dict:
    r = requests.post(TOKEN_URL, timeout=30, data={
        "client_id": get_env("TWITCH_CLIENT_ID"),
        "client_secret": get_env("TWITCH_CLIENT_SECRET"),
        "grant_type": "refresh_token",
        "refresh_token": data["refresh_token"],
    })
    if r.status_code != 200:
        raise TwitchNotSignedIn(f"refresh failed ({r.status_code}); run `python tools/twitch_user_auth.py --force`")
    _save(r.json())  # refresh tokens can be single-use, so always keep the newest one
    return json.loads(TOKEN_FILE.read_text())


def get_user_token(force_refresh: bool = False) -> str:
    if not TOKEN_FILE.exists():
        raise TwitchNotSignedIn("no token_twitch.json; run `python tools/twitch_user_auth.py`")
    data = json.loads(TOKEN_FILE.read_text())
    if force_refresh or time.time() > data["expires_at"] - 300:
        data = _refresh(data)
    return data["access_token"]


def whoami() -> dict:
    r = requests.get(VALIDATE_URL, headers={"Authorization": f"OAuth {get_user_token()}"}, timeout=30)
    if r.status_code == 401:
        r = requests.get(VALIDATE_URL, headers={"Authorization": f"OAuth {get_user_token(force_refresh=True)}"}, timeout=30)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    if "--force" in sys.argv or not TOKEN_FILE.exists():
        login()
    info = whoami()
    print(f"Signed in to Twitch as {info['login']} (scopes: {', '.join(info.get('scopes', []))})")
