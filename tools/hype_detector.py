"""Live hype detector: watch chat on the top live GTA 6 streams and clip moments the instant chat explodes.

Reads chat anonymously over Twitch IRC (free, no login). Per channel it compares the last 10 seconds of chat
(messages with KEKW/LUL/LMAO/💀/"NO WAY"... count extra, "clip it" counts triple) against the channel's
normal 10-second level over the last 5 minutes. A big enough spike creates a 30s Twitch clip through the API
(needs `python tools/twitch_user_auth.py` once) and queues it in data/clips.db with source='hype'; the
normal pipeline then safety-checks, trims, hooks, renders and uploads it.

Settings: the "hype" block in config.json. Runs inside run_pipeline.py as a background thread, or alone:

    python tools/hype_detector.py --dry-run --minutes 10   # just log spikes, create nothing
    python tools/hype_detector.py                          # create clips (no rendering/uploading)
"""

import argparse
import logging
import queue
import random
import re
import socket
import ssl
import statistics
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import requests

import clip_db
from _common import load_config
from schedule import hype_slots_left, in_upload_window
from twitch_api import HELIX, Twitch
from twitch_user_auth import TOKEN_FILE, TwitchNotSignedIn, get_user_token

log = logging.getLogger("pipeline.hype")

PRIVMSG = re.compile(r"^:[^ ]+ PRIVMSG #(\w+) :(.*)$")
HYPE_WORDS = re.compile(
    r"(KEKW|OMEGALUL|LUL|LMA+O+|ICANT|PogChamp|POGGERS|\bPog\b|NO WAY|WTF|HOLY|LETS GO+|\bW+\b|💀|😂|🤣|\?\?\?)", re.I)
CLIP_CALL = re.compile(r"\bclip (it|that|this)\b", re.I)


def message_weight(text: str) -> float:
    weight = 1.0
    if HYPE_WORDS.search(text):
        weight += 1.0
    if CLIP_CALL.search(text):
        weight += 2.0
    return weight


class ChannelStats:
    """Weighted chat messages per second for the last few minutes of one channel."""
    WINDOW = 10    # seconds that make up "right now"
    HISTORY = 300  # seconds used for the channel's normal level

    def __init__(self, now: float):
        self.started = now
        self.counts: dict[int, float] = defaultdict(float)
        self.last_trigger = 0.0

    def add(self, t: float, weight: float):
        self.counts[int(t)] += weight

    def _sum(self, start: int) -> float:
        return sum(self.counts.get(s, 0.0) for s in range(start, start + self.WINDOW))

    def recent(self, now: float) -> float:
        return self._sum(int(now) - self.WINDOW + 1)

    def baseline(self, now: float) -> float:
        """Median 10s total over the history, excluding the current window (robust to earlier spikes).
        Only windows since we started watching count — empty pre-join windows would drag it to zero."""
        end = int(now) - self.WINDOW + 1          # current window starts here
        first = max(end - self.HISTORY, int(self.started) + 1)
        sums = [self._sum(s) for s in range(first, end - self.WINDOW + 1, self.WINDOW)]
        return statistics.median(sums) if sums else 0.0

    def prune(self, now: float):
        cutoff = int(now) - self.HISTORY - 2 * self.WINDOW
        for s in [s for s in self.counts if s < cutoff]:
            del self.counts[s]


class HypeDetector:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg, self.h, self.dry_run = cfg, cfg["hype"], dry_run
        self.tw = Twitch()
        self.channels: dict[str, dict] = {}       # login -> {id, name, viewers}
        self.stats: dict[str, ChannelStats] = {}
        self.blocked_until: dict[str, float] = {}  # login -> time; clipping restricted on that channel
        self.clip_times: deque[float] = deque()    # for the global per-hour limit
        self.jobs: queue.Queue = queue.Queue()
        self.sock = None
        self.joined: set[str] = set()
        self.stop = threading.Event()

    # ---- which chats to watch -------------------------------------------------------------------
    def pick_channels(self) -> dict[str, dict]:
        gid = self.tw.game_id(self.cfg["game_name"])
        langs = set(self.cfg["languages"])
        blocked = {b.lower() for b in self.cfg["blocklist"]}
        watch = {w.lower() for w in self.cfg["watchlist"]}
        streams = [s for s in self.tw.live_streams(gid, first=100)
                   if (not langs or s["language"] in langs) and s["user_login"] not in blocked]
        streams.sort(key=lambda s: s["viewer_count"], reverse=True)
        top = [s for s in streams if s["viewer_count"] >= self.h["min_viewers"]][: self.h["top_streams"]]
        top += [s for s in streams if s["user_login"] in watch and s not in top]
        return {s["user_login"]: {"id": s["user_id"], "name": s["user_name"], "viewers": s["viewer_count"]} for s in top}

    # ---- chat connection ------------------------------------------------------------------------
    def _send(self, line: str):
        self.sock.sendall((line + "\r\n").encode())

    def connect(self):
        raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=15)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
        self._send("PASS SCHMOOPIIE")  # anonymous read-only login
        self._send(f"NICK justinfan{random.randint(10000, 99999)}")
        self.sock.settimeout(1.0)
        self.joined = set()

    def sync_joins(self, wanted: set[str]):
        for login in self.joined - wanted:
            self._send(f"PART #{login}")
            self.stats.pop(login, None)
        for login in wanted - self.joined:
            self._send(f"JOIN #{login}")
            self.stats[login] = ChannelStats(time.time())
            time.sleep(0.6)  # stay well under Twitch's join rate limit
        self.joined = set(wanted)

    def handle(self, line: str):
        if line.startswith("PING"):
            self._send("PONG :tmi.twitch.tv")
        elif " RECONNECT" in line:
            raise ConnectionError("Twitch asked us to reconnect")
        elif m := PRIVMSG.match(line):
            if st := self.stats.get(m.group(1)):
                st.add(time.time(), message_weight(m.group(2)))

    # ---- spike detection ------------------------------------------------------------------------
    def check_spikes(self):
        now = time.time()
        for login, st in self.stats.items():
            st.prune(now)
            if now - st.started < self.h["warmup_seconds"]:
                continue
            if now - st.last_trigger < self.h["channel_cooldown_minutes"] * 60:
                continue
            recent = st.recent(now)
            base = max(st.baseline(now), self.h["baseline_floor"])
            if recent >= self.h["min_weighted_msgs"] and recent >= self.h["spike_ratio"] * base:
                st.last_trigger = now
                score = round(recent / base, 2)
                name = self.channels.get(login, {}).get("name", login)
                log.info(f"Hype spike  {name}: chat {recent:.0f} vs normal {base:.0f} per 10s (x{score})"
                         + (" [dry run]" if self.dry_run else ""))
                if not self.dry_run:
                    self.jobs.put((login, score, now))

    # ---- clip creation (worker thread) ------------------------------------------------------------
    def _create_clip(self, login: str, broadcaster_id: str) -> str | None:
        r = None
        for attempt in range(2):
            r = requests.post(
                f"{HELIX}/clips", timeout=30,
                params={"broadcaster_id": broadcaster_id, "duration": self.h["clip_seconds"]},
                headers={"Client-Id": self.tw.client_id, "Authorization": f"Bearer {get_user_token(force_refresh=attempt == 1)}"},
            )
            if r.status_code != 401:
                break
        if r.status_code == 202:
            return r.json()["data"][0]["id"]
        if r.status_code == 403:  # followers/subs-only clipping, or clips disabled
            self.blocked_until[login] = time.time() + 6 * 3600
            log.info(f"Hype: clipping is restricted on {login}; skipping that channel for 6h")
        elif r.status_code == 429:
            log.warning("Hype: Twitch rate-limited clip creation; pausing clips for 2 minutes")
            time.sleep(120)
        else:
            log.warning(f"Hype: create clip on {login} failed: {r.status_code} {r.text[:200]}")
        return None

    def _wait_for_clip(self, clip_id: str) -> dict | None:
        """Clip creation is async; Twitch says to assume failure if it hasn't appeared after ~60s."""
        for _ in range(14):
            time.sleep(5)
            data = self.tw.get("clips", {"id": clip_id})["data"]
            if data:
                return data[0]
        return None

    def _worth_clipping(self, conn, name: str, score: float) -> bool:
        """Only create (public) clips we can actually use: inside the upload window, while hype upload slots
        remain, and with at most max_pending unused hype clips. A much stronger spike replaces the weakest one."""
        if not in_upload_window(self.cfg) or hype_slots_left(conn, self.cfg) <= 0:
            return False
        pending = conn.execute(
            "SELECT clip_id, hype_score, render_path FROM clips WHERE source = 'hype' "
            "AND status IN ('queued', 'downloaded', 'transcribed', 'rendered') ORDER BY hype_score ASC"
        ).fetchall()
        if len(pending) < self.h["max_pending"]:
            return True
        weakest = pending[0]
        if score < (weakest["hype_score"] or 0) * 1.25:
            log.info(f"Hype: {name} spike x{score} not stronger than the clips already waiting; not clipping")
            return False
        clip_db.update(conn, weakest["clip_id"], status="skipped", error=f"superseded by a stronger hype clip (x{score})")
        if weakest["render_path"]:
            Path(weakest["render_path"]).unlink(missing_ok=True)
        return True

    def make_clip(self, login: str, score: float, spotted_at: float):
        wait = self.h["clip_delay_seconds"] - (time.time() - spotted_at)
        if wait > 0:
            time.sleep(wait)  # let the streamer's reaction land inside the clip
        ch = self.channels.get(login)
        now = time.time()
        while self.clip_times and now - self.clip_times[0] > 3600:
            self.clip_times.popleft()
        if not ch or self.blocked_until.get(login, 0) > now:
            return
        if len(self.clip_times) >= self.h["max_clips_per_hour"]:
            log.info(f"Hype: hourly clip limit ({self.h['max_clips_per_hour']}) reached; not clipping {ch['name']}")
            return
        conn = clip_db.connect()
        try:
            if clip_db.find_overlapping(conn, ch["id"], clip_db.now_iso(), self.cfg["duplicate_window_sec"], ""):
                return  # we already have a clip of this moment
            if not self._worth_clipping(conn, ch["name"], score):
                return
            clip_id = self._create_clip(login, ch["id"])
            if not clip_id:
                return
            self.clip_times.append(time.time())
            info = self._wait_for_clip(clip_id)
            if not info:
                log.warning(f"Hype: clip {clip_id} on {ch['name']} never appeared")
                return
            conn.execute(
                "INSERT OR IGNORE INTO clips (clip_id, url, broadcaster_id, broadcaster_name, title, language, view_count, "
                "duration, created_at, video_id, vod_offset, source, hype_score, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (info["id"], info["url"], info["broadcaster_id"], info["broadcaster_name"], info["title"], info["language"],
                 info["view_count"], info["duration"], info["created_at"], info["video_id"], info["vod_offset"],
                 "hype", score, "queued", clip_db.now_iso(), clip_db.now_iso()),
            )
            conn.commit()
            log.info(f"Hype clip   {ch['name']} (x{score}) queued: {info['url']}")
        finally:
            conn.close()

    def _clip_worker(self):
        while not self.stop.is_set():
            try:
                login, score, spotted_at = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.make_clip(login, score, spotted_at)
            except TwitchNotSignedIn as e:
                log.warning(f"Hype: {e}")
            except Exception as e:
                log.warning(f"Hype: clipping {login} failed: {e.__class__.__name__}: {e}")

    # ---- main loop ------------------------------------------------------------------------------
    def run(self, minutes: float | None = None):
        threading.Thread(target=self._clip_worker, daemon=True, name="hype-clips").start()
        started, next_refresh, next_check, buf = time.time(), 0.0, 0.0, b""
        while not self.stop.is_set() and not (minutes and time.time() - started > minutes * 60):
            try:
                if self.sock is None:
                    self.connect()
                    next_refresh = 0.0
                if time.time() >= next_refresh:
                    self.cfg = load_config()
                    self.h = self.cfg["hype"]
                    self.channels = self.pick_channels()
                    self.sync_joins(set(self.channels))
                    next_refresh = time.time() + self.h["refresh_minutes"] * 60
                    log.info(f"Hype: watching {len(self.channels)} chats: "
                             + ", ".join(c["name"] for c in self.channels.values()))
                try:
                    data = self.sock.recv(65536)
                    if not data:
                        raise ConnectionError("chat connection closed")
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    for line in lines:
                        self.handle(line.decode("utf-8", "replace"))
                except socket.timeout:
                    pass
                if time.time() >= next_check:
                    self.check_spikes()
                    next_check = time.time() + 1.0
            except (OSError, ConnectionError) as e:
                log.warning(f"Hype: chat connection lost ({e}); reconnecting in 15s")
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock, buf = None, b""
                time.sleep(15)
            except Exception as e:  # never let the detector die quietly
                log.exception(f"Hype: unexpected error: {e}")
                time.sleep(15)


def start_background(cfg: dict) -> threading.Thread | None:
    """Used by run_pipeline: start the detector thread if enabled and signed in to Twitch."""
    if not cfg.get("hype", {}).get("enabled"):
        return None
    if not TOKEN_FILE.exists():
        log.warning("Hype detector is off until you sign in to Twitch: `python tools/twitch_user_auth.py`")
        return None
    t = threading.Thread(target=HypeDetector(cfg).run, daemon=True, name="hype")
    t.start()
    return t


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only log spikes; don't create clips")
    ap.add_argument("--minutes", type=float, default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    HypeDetector(load_config(), dry_run=args.dry_run).run(minutes=args.minutes)
