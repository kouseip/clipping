"""Minimal Twitch Helix client using a free app access token (client credentials).

Needs TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET in .env.

CLI (sanity check your credentials):
    python tools/twitch_api.py                          # resolve the game in config.json
    python tools/twitch_api.py live                     # top live streams in that game right now
"""

import sys
import time

import requests

from _common import get_env, load_config

HELIX = "https://api.twitch.tv/helix"


class Twitch:
    def __init__(self):
        self.client_id = get_env("TWITCH_CLIENT_ID")
        self.client_secret = get_env("TWITCH_CLIENT_SECRET")
        self._token = None
        self._token_expires = 0.0

    def _get_token(self) -> str:
        # App tokens last ~60 days; reuse in-process and only re-mint when close to expiry.
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + body.get("expires_in", 3600)
        return self._token

    def get(self, path: str, params) -> dict:
        for attempt in range(3):
            r = requests.get(
                f"{HELIX}/{path}",
                params=params,
                headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self._get_token()}"},
                timeout=30,
            )
            if r.status_code == 401:  # token revoked/expired early
                self._token = None
                continue
            if r.status_code == 429:  # rate limited: wait until the bucket resets
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 5))
                time.sleep(max(1.0, reset - time.time()))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return r.json()

    def game_id(self, name: str) -> str:
        data = self.get("games", {"name": name})["data"]
        if data:
            return data[0]["id"]
        # Fall back to fuzzy category search (exact names on Twitch can differ, e.g. "Grand Theft Auto VI").
        hits = self.get("search/categories", {"query": name, "first": 5})["data"]
        if not hits:
            raise LookupError(f"No Twitch category found for {name!r}")
        return hits[0]["id"]

    def user_ids(self, logins: list[str]) -> dict[str, str]:
        """login -> broadcaster_id, 100 per request."""
        out = {}
        for i in range(0, len(logins), 100):
            batch = logins[i : i + 100]
            for u in self.get("users", [("login", l.lower()) for l in batch])["data"]:
                out[u["login"]] = u["id"]
        return out

    def clips(self, *, game_id=None, broadcaster_id=None, started_at=None, max_pages=1) -> list[dict]:
        """Clips sorted by view count (desc). Exactly one of game_id / broadcaster_id."""
        params = {"first": 100}
        if game_id:
            params["game_id"] = game_id
        if broadcaster_id:
            params["broadcaster_id"] = broadcaster_id
        if started_at:
            params["started_at"] = started_at
        results, cursor = [], None
        for _ in range(max_pages):
            if cursor:
                params["after"] = cursor
            body = self.get("clips", params)
            results.extend(body["data"])
            cursor = body.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return results

    def live_streams(self, game_id: str, first: int = 20) -> list[dict]:
        return self.get("streams", {"game_id": game_id, "first": first, "type": "live"})["data"]


if __name__ == "__main__":
    cfg = load_config()
    tw = Twitch()
    gid = tw.game_id(cfg["game_name"])
    print(f"{cfg['game_name']!r} -> game_id {gid}")
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        for s in tw.live_streams(gid):
            print(f"  {s['user_name']:<20} {s['viewer_count']:>7} viewers  [{s['language']}]  {s['title'][:60]}")
