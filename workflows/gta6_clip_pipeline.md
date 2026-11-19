# Workflow: GTA 6 Twitch → YouTube Shorts Clip Pipeline

## Objective
Continuously find the best GTA 6 moments from Twitch streamers, turn each into a 9:16 YouTube Short with
clean word-by-word captions and streamer credit, and upload to the clipping channel. Target cost: **$0**
(free APIs, local GPU for transcription and encoding).

## Inputs
| Input | Required | Where | Notes |
|-------|----------|-------|-------|
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | Yes | `.env` | Free app at https://dev.twitch.tv/console/apps (needs 2FA on the Twitch account) |
| `credentials.json` | For upload | project root | Google Cloud OAuth client, type **Desktop app**, with **YouTube Data API v3** enabled |
| `token_youtube.json` | For upload | project root | Created by `python tools/google_auth.py youtube`. Sign in with the account that owns the clipping channel (pick the Brand Account channel if asked) |

**OAuth consent screen must be "In production", not "Testing".** In Testing, Google refresh tokens expire after
7 days and uploads silently stop. Publishing an unverified personal app is fine — click through the
"Google hasn't verified this app" warning when you sign in.
| `config.json` | Yes | project root | Filters and pacing (see below) |

## Tools
| Step | Tool | What it does |
|------|------|--------------|
| 1 | `tools/fetch_twitch_clips.py` | Top-viewed recent clips in the GTA VI category (or a watchlist) → filtered, de-duplicated, queued in `data/clips.db` |
| 2 | `tools/download_clip.py` | yt-dlp download to `.tmp/clips/` |
| 3 | `tools/transcribe_clip.py` | faster-whisper `large-v3-turbo` on the RTX 4060 → word timestamps in `.tmp/transcripts/` |
| 3b | `tools/safety_check.py` | Claude Haiku 5.5 reviews a frame every ~2s (max 30) + transcript against YouTube's rules (would it be removed or age-restricted?). Reject → `skipped`; can't run → clips held, never rendered unchecked. Same call flags main-story spoilers → title gets a `SPOILER:` prefix (~$0.0005–0.0017/clip) |
| 4 | `tools/render_short.py` | 1080×1920: blurred background, full clip, `@streamer` credit, animated captions; NVENC encode; loudness normalized |
| 5 | `tools/generate_title.py` | Claude Haiku 5.5 writes a short, punchy title from 3 frames + transcript (~$0.0005). No `ANTHROPIC_API_KEY` or API error → rule-based title |
| 6 | `tools/upload_youtube.py` | Builds description/tags (with credit + links) and uploads |
| — | `tools/run_pipeline.py` | The agent: runs 1→5 every `poll_minutes`, enforces pacing, cleans up files |
| — | `tools/clip_db.py` | Inspect state: `python tools/clip_db.py` (counts), `... list rendered` |

Run it: `start_clipper.bat` (loop) · `start_clipper.bat --once --no-upload` (render-only test pass).

**Auto-start:** a shortcut in the Windows Startup folder (`GTA 6 Clip Agent.lnk`) launches `start_clipper.bat`
minimized at logon. The .bat restarts the agent 60s after a crash. Only one agent can run at a time (it holds
localhost port 47823); a second copy prints "already running" and exits with code 3, which closes its window.
To restart onto new code: close the "GTA 6 Clip Agent" window and run `start_clipper.bat` again.

## Outputs
- **Deliverable:** Shorts on the YouTube channel. The uploaded clip's YouTube ID is in `data/clips.db`; log in `data/pipeline.log`.
- **Intermediates:** `.tmp/clips` (deleted after render), `.tmp/renders` + `.tmp/transcripts` (deleted after upload).
- **State:** `data/clips.db` is NOT disposable — it prevents duplicate uploads. Don't delete it.

## config.json reference
| Key | Default | Meaning |
|-----|---------|---------|
| `game_name` | Grand Theft Auto VI | Twitch category name (resolved to an ID each cycle) |
| `languages` | ["en"] | Stream languages to accept (`[]` = any) |
| `lookback_hours` / `max_pages` | 12 / 3 | How far back and how many 100-clip pages to scan |
| `min_views` / `min_duration_sec` | 50 / 8 | Quality floor for a clip |
| `max_clip_age_hours` | 48 | Queued clips older than this are skipped (stale) |
| `duplicate_window_sec` | 60 | Same streamer + clips created within this window = same moment; keep the most-viewed |
| `watchlist` / `blocklist` | [] / [] | Twitch logins. Watchlist set → only those streamers |
| `title_blocklist` | naked, shower, horny, … | Clip titles containing any of these (substring, case-insensitive) are never queued |
| `safety_check` | true | Frame-level content check before render (and before upload for anything rendered unchecked) |
| `max_uploads_per_day` | 15 | Rolling 24h cap. Ramp up as the channel gains history/verification (see Learnings) |
| `max_uploads_per_streamer_per_day` | 3 | Keeps the feed varied |
| `min_minutes_between_uploads` | 90 | Spacing; set ≈ 1440 / max_uploads_per_day to spread uploads across the whole day |
| `youtube_privacy` | private | Start `private` to review in YouTube Studio; switch to `public` once output is trusted |
| `poll_minutes` / `render_batch_size` | 15 / 3 | Cycle interval; how many rendered clips to keep ready |
| `whisper_model` | large-v3-turbo | Use `distil-large-v3` or `small` if VRAM is tight |

Edits to `config.json` take effect next cycle — no restart needed.

## Edge cases & failure handling
- **A clip fails 3 times** → marked `failed`, queue moves on. Check `python tools/clip_db.py list failed`.
- **YouTube quota / channel upload limit** → uploads pause 12h automatically; rendering continues up to the backlog cap.
- **YouTube not authorized** → pipeline renders only and logs a reminder. Never let the loop trigger a browser OAuth.
- **YouTube sign-in expired** (every 7 days while the Google app is in Testing) → log says so each cycle; rendered
  clips stay queued (not failed). Fix: `python tools/google_auth.py youtube --force` (pick hotmicclipsYT), then
  uploads resume next cycle. `--force` matters: without it a still-valid token just refreshes and the 7-day clock
  keeps running. Calendar reminder set for 2026-11-25 12:00 CT (token minted ~22:30 CT on 11-18).
- **Twitch 429** → client waits for `Ratelimit-Reset`. **401** → re-mints the app token.
- **Category not found** → falls back to Twitch category search. If GTA VI is listed under another name, set `game_name`.
- **No speech in clip** → renders with credit only; title falls back to the Twitch clip title.
- **Disk** (only ~67 GB free on C:) → files are deleted as each stage completes; watch `.tmp/` if uploads are paused for long.

## Policy guardrails (read before going public)
- Clips are other creators' content. Credit every clip (done automatically) and prefer streamers who welcome clippers
  (many run clipping programs/Discords). Remove anything a streamer asks you to — use `blocklist`.
- YouTube monetization: the **reused content** policy requires meaningful added value; captions/format help, but
  low-effort mass uploads risk demonetization under the **inauthentic content** policy. Keep volume moderate.
- Twitch clip titles are written by random viewers — `private` mode lets you catch bad ones before they go live.
- Rockstar's policy on posting its material covers **non-commercial** use — read it before monetizing:
  https://support.rockstargames.com/articles/200153756/Policy-on-posting-copyrighted-Rockstar-Games-material
- Launch week: story cutscenes are spoilers — keep them out of titles/hooks or label "SPOILER". Skip nudity frames
  (strip clubs etc.) entirely — `safety_check.py` now does this automatically; `private` review is the backstop.
- Paid alternative with permission built in: streamers' clipping campaigns (e.g. Content Rewards) pay per view
  (~$1.13 per 1k, Nov 2026) and grant the right to post their clips.

## Roadmap (not built yet)
1. **Live chat-spike detector** — watch chat of live GTA 6 streamers, detect hype spikes (message rate + KEKW/LUL/W),
   call Twitch **Create Clip** (`clips:edit` user token, `duration` 5–60s) to grab moments nobody clipped.
2. **Per-streamer facecam layouts** (split screen: facecam top, gameplay bottom).
3. ~~Better titles~~ — done 2026-11-18 (`generate_title.py`, Claude Haiku 5.5).
4. **Custom Shorts thumbnails** (`thumbnails.set` supports Shorts since Nov 3, 2026).
5. Kick / YouTube Live sources.
6. ~~Run at logon~~ — done 2026-11-18 (Startup-folder shortcut + crash-restart loop + single-instance lock).
7. ~~Spoiler guard~~ — done 2026-11-18 (flag from safety_check → "SPOILER:" title prefix).

## Learnings
- 2026-11-18: YouTube removed the "unverified API project → forced private" rule on **Nov 2, 2026**, and since
  June 1, 2026 uploads use their own quota bucket (100/day, 1 unit each). No audit needed for a personal channel.
- 2026-11-18: Twitch **Get Clips Download** requires editor access to the channel — not usable for clipping others.
  yt-dlp downloads public clip URLs without auth instead.
- 2026-11-18: faster-whisper 1.2.1 + PyAV 19 crash (`metadata_errors` kwarg). Fixed by decoding audio with ffmpeg
  and passing the numpy array to `model.transcribe`.
- 2026-11-18: Whisper word probability < 0.2 is usually a misplaced/incorrect word; low-confidence long words at a
  segment end are hallucinated tails ("you"). Both are filtered.
- 2026-11-18: ffmpeg `subtitles=` filter breaks on Windows `C:` paths — render runs with cwd=ROOT and relative paths.
- 2026-11-18: Windows console (cp1252) crashes on emoji in Twitch titles — `_common.py` forces UTF-8 stdout.
- 2026-11-18: Pipeline speed on RTX 4060: ~15–20s per 30s clip (download + transcribe + render).
- 2026-11-18: Upload limits: the API allows 100 uploads/day per Google project; YouTube also has an unpublished
  per-channel daily cap that is lower for new/unverified channels and rises with phone/ID verification and channel
  history (strikes lower it). The pipeline logs "upload limit hit after N uploads" when it finds the real cap.
- 2026-11-18: Shorts = vertical/square and ≤ 3 min, classified automatically (no API flag). Renders are capped at
  59s because Shorts over 1 minute with a Content ID claim are blocked; 60s Twitch clips came out at 60.1s.
- 2026-11-18: Google OAuth app in Testing → sign-in fails with "Error 403: access_denied" unless the account is
  listed under Google Auth Platform → Audience → Test users (owners are not added automatically).
- 2026-11-18: "Publish app" was blocked with "complete your configuration on the Branding page" even though name,
  support email and developer contact were filled. Unresolved — running in Testing with a test user meanwhile.
- 2026-11-18: Twitch's top GTA VI clips on launch day were heavy on suggestive content with harmless-looking
  titles ("neuron activation"). Title keyword filter catches the obvious ones; `safety_check.py` (added same day)
  catches the rest — it correctly rejected "neuron activation" (exposed nudity) and passed normal gameplay clips.
- 2026-11-18: Haiku 5.5 titles cost ~$0.00016 each in practice (3 frames at 360px + transcript, effort low).
- 2026-11-18: SQLite schema changes need a migration: `clip_db.connect()` adds missing columns via ALTER TABLE.
