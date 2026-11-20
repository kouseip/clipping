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
| `token.json` | For the tracker sheet | project root | Created by `python tools/google_auth.py`. Sign in with your **main** Google account (the sheet lives in its Drive; Brand Accounts have no Drive) |

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
| 3c | `tools/plan_edit.py` | Claude Haiku 5.5 sees ~15 timestamped frames + timed transcript → first judges the clip (one-sentence "what visibly happens" + a 1–10 score for "does a stranger get it right away"; under `min_moment_score` → `skipped`, never rendered), then the best 10–20s window (ends on the payoff, never cuts a word), a 3–6 word on-screen hook (varied vs. the last 10 hooks), the title, and 3–4 clip-specific tags (~$0.0003). Fails → last 20s, no hook, not judged |
| 4 | `tools/render_short.py` | 1080×1920: blurred background, full clip, `@streamer` credit, animated captions; NVENC encode; loudness normalized |
| 5 | `tools/generate_title.py` | Claude Haiku 5.5 writes a short, punchy title from 3 frames + transcript (~$0.0005). No `ANTHROPIC_API_KEY` or API error → rule-based title |
| 6 | `tools/upload_youtube.py` | Builds description/tags (with credit + links) and uploads |
| 1b | `tools/hype_detector.py` | Background thread: reads chat (anonymous IRC) on the top live English GTA 6 streams, detects spikes vs. each channel's normal level (hype emotes weigh extra, "clip it" triple), creates a 30s Twitch clip via the API and queues it as `source='hype'`. Needs `tools/twitch_user_auth.py` once. `--dry-run` logs spikes without clipping |
| — | `tools/twitch_user_auth.py` | Twitch device-code sign-in (scope `clips:edit`) → `token_twitch.json` (gitignored), auto-refreshed |
| 7 | `tools/track_shorts.py` | Every `tracker.refresh_minutes`: views/likes/comments per Short from YouTube (free, 1 quota unit per 50 videos), snapshots at 12/24/48/72h public, flags flops "DELETE + repost", requeues a flagged Short once you delete it, and rewrites the Google Sheet "hotmicclipsYT Shorts tracker" (tabs Shorts + By streamer). Run by hand: `python tools/track_shorts.py [--no-sheet]` |
| — | `tools/run_pipeline.py` | The agent: runs 1→7 every `poll_minutes`, enforces pacing, cleans up files |
| — | `tools/clip_db.py` | Inspect state: `python tools/clip_db.py` (counts), `... list rendered` |

Run it: `start_clipper.bat` (loop) · `start_clipper.bat --once --no-upload` (render-only test pass).

**Auto-start:** a shortcut in the Windows Startup folder (`GTA 6 Clip Agent.lnk`) launches `start_clipper.bat`
minimized at logon. The .bat restarts the agent 60s after a crash. Only one agent can run at a time (it holds
localhost port 47823); a second copy prints "already running" and exits with code 3, which closes its window.
To restart onto new code: close the "GTA 6 Clip Agent" window and run `start_clipper.bat` again.

**Runs without VS Code** as long as it was started by Windows (logon shortcut, or double-clicking the .bat/shortcut).
A copy started from a VS Code / Claude Code terminal lives in that session's job object and may die when VS Code
closes. Relaunch it detached with `explorer.exe "<Startup folder>\GTA 6 Clip Agent.lnk"` (2026-11-19 learning).
**Sleep:** during the upload window (or a burst) the agent blocks Windows *idle* sleep (SetThreadExecutionState);
the screen can still turn off and manual sleep / lid close still works. Outside the window normal sleep applies, so
the PC must be awake (or woken) by 11:00 for uploads to start on time.

## Outputs
- **Deliverable:** Shorts on the YouTube channel. The uploaded clip's YouTube ID is in `data/clips.db`; log in `data/pipeline.log`.
- **Deliverable:** the Google Sheet tracker (its ID is saved in `data/tracker.json`; deleted from Drive → a new one is made).
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
| `auto_edit` / `edit_max_seconds` | true / 20 | Trim to the best window + hook banner. Raise the max to test longer Shorts |
| `min_moment_score` | 6 | plan_edit's clarity score a clip needs (1–3 nothing happening / talking to chat / needs context, 4–5 mild or hard to follow, 6–7 clear moment, 8–10 great). Skips log as "Passed on …" with the reason; the score and sentence are saved in `clips.moment_score` / `clips.moment` |
| `upload_burst` | {until, min_minutes_between_uploads} | Temporary faster spacing that switches itself off at `until` (PC local time) |
| `hype.*` | see config.json | `top_streams` 15, `min_viewers` 1000, `warmup_seconds` 240, `spike_ratio` 3.0 (× normal 10s chat level), `min_weighted_msgs` 40, `min_reaction_share` 0.25, `max_answer_share` 0.4, `baseline_floor` 8, `channel_cooldown_minutes` 5, `max_clips_per_hour` 6, `clip_seconds` 30, `clip_delay_seconds` 4, `max_uploads_per_day` 2 (hype share of the daily uploads), `max_pending` 2 (unused hype clips at once; a spike ≥1.25× the weakest replaces it), `max_age_hours` 6 (stale after) |

Hype clips are only **created** inside the upload window and while hype upload slots remain — no point posting
public clips we'll never use.

**A spike must be a reaction.** Chat also explodes when the streamer asks a question, runs a poll or a giveaway, or
gets raided, with nothing happening in the game. So for every spike the detector looks at *what* chat typed in that
10s window: it needs at least `hype.min_reaction_share` of messages laughing or shocked (KEKW, LMAO, 💀, NO WAY,
"clip it"…) and at most `hype.max_answer_share` answering ("1", "yes", "b", "!enter"). Otherwise the log says
"-> ignored, not a reaction" with the top 3 messages, so thresholds can be tuned from real chat. Bot commands
("!…") never count toward a spike at all. Clips that pass still go through plan_edit's clarity score.

**Hype clip priority:** while hype upload slots remain today, fresh hype clips (strongest spike first) go ahead
of viewer clips, with their own render slot (one rendered hype clip waiting at a time) so a full backlog never
delays them; the agent also wakes between cycles when one is queued. Out of hype slots → hype clips are ignored
until tomorrow and expire after `max_age_hours`. Hype clips appear publicly on the streamer's Twitch channel
under the signed-in account's name.

**Delete + repost (flops):** a first upload still under `tracker.min_views` (100) after `tracker.judge_after_hours`
(48) public gets Verdict "DELETE + repost" in the sheet and a WARNING in the log. The user deletes it in YouTube
Studio (deleting stays manual on purpose: it's irreversible, and the token has no delete scope). On the next
refresh the tracker sees it gone and requeues the clip as `source='repost'`: it's re-downloaded from Twitch,
re-planned with a **new title and hook** (plan_edit is told the old ones), and uploaded ahead of regular clips
(after fresh hype clips) in a normal slot, so on a different day than the original. Reposts never expire as "too old". Only one
repost per clip; a repost that flops too shows "Flopped twice (leave it)". Deleting a Short the tracker didn't flag
does nothing. If the streamer deleted the Twitch clip, the repost fails 3 times and is marked `failed`.
The 48h clock starts when the tracker first sees a Short public (YouTube's `publishedAt` isn't reliable for
private→public videos), so a verdict can come a few hours late but never early.
| `max_uploads_per_day` | 5 | Counted from each day's `upload_window` start (local midnight if no window). First-two-weeks plan (from 2026-11-19): 5/day, then raise if views per Short hold |
| `max_uploads_per_streamer_per_day` | 2 | Keeps the feed varied |
| `min_minutes_between_uploads` | 150 | Spacing; ≈ window length / max_uploads_per_day |
| `upload_window` | 11:00–23:00 | Uploads only happen inside this window (PC local time, Central). Remove the key to upload around the clock |
| `youtube_privacy` | private | Start `private` to review in YouTube Studio; switch to `public` once output is trusted |
| `tracker.*` | see config.json | `enabled` true, `refresh_minutes` 60, `judge_after_hours` 48, `min_views` 100 (the growth video's rule: under ~100 views after 2 days = missed the algorithm), `auto_repost` true (false = flag only, never requeue) |
| `poll_minutes` / `render_batch_size` | 15 / 2 | Cycle interval; how many rendered clips to keep ready (small = fresher clips at 5/day) |
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
- **Sheets sign-in expired** (same 7-day rule; token.json minted 2026-11-19) → tracker still updates the DB and
  still requeues reposts, only the sheet goes stale; log warns hourly. Renew both tokens in one go:
  `python tools/google_auth.py renew` (first sign-in: pick hotmicclipsYT; second: your main account).
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
0. ~~Hook text + auto-trim to ≤20s + clip-specific tags~~ — done 2026-11-18 (`plan_edit.py`).
1. ~~Live chat-spike detector~~ — done 2026-11-19 (`hype_detector.py`). Tune thresholds from real results.
2. **Per-streamer facecam layouts** (split screen: facecam top, gameplay bottom).
3. ~~Better titles~~ — done 2026-11-18 (`generate_title.py`, Claude Haiku 5.5).
4. **Custom Shorts thumbnails** (`thumbnails.set` supports Shorts since Nov 3, 2026).
5. Kick / YouTube Live sources.
6. ~~Run at logon~~ — done 2026-11-18 (Startup-folder shortcut + crash-restart loop + single-instance lock).
7. ~~Spoiler guard~~ — done 2026-11-18 (flag from safety_check → "SPOILER:" title prefix).
8. ~~Performance scoreboard + delete/repost tracker~~ — done 2026-11-19 (`track_shorts.py`, Google Sheet).
   Use it for the 2-week volume review: views per Short by streamer, hype vs. regular, spoiler vs. not.

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
- 2026-11-18: Shorts growth advice adopted from a creator video (unverified, test with the scoreboard): aim for
  ≥70% "viewed vs. swiped away" (YouTube Studio metric) via ≤20s Shorts + a hook in the first 2s; tags = 3-4
  clip-specific + niche + broad; no hashtags in titles, ~3 in the description; new channels get pushed slowly;
  posting 1/day may beat many/day for new channels (volume decision pending, 2026-11-18).
- 2026-11-18: Queue clips averaged 33s (only 17% ≤20s) before auto-trim.
- 2026-11-18: Daily caps used a rolling 24h window, so late-night uploads would eat the next day's quota. Now
  counted from the upload window's start.
- 2026-11-18: Haiku titles occasionally break the rules (quotes, 83 chars). `generate_title._clean` now strips
  quotes and cuts a tacked-on second clause past 70 chars. Don't rely on the prompt alone for hard limits.
- 2026-11-18: The upload-time safety check caught a strip-club clip ("LMAO", Dinah) rendered before the check
  existed — keep that backstop.
- 2026-11-19: Hype detector: anonymous IRC (`justinfan#####`) still works. Bug found in dry run: the baseline
  counted the empty pre-join minutes → every channel "spiked" at warm-up end. Fixed (baseline only uses windows
  since joining). At 2.5× it fired ~2/min across 15 chats (top ratios ~3.2) → raised to 3.0× + 40 weighted msgs.
- 2026-11-19: Create Clip returned 403 on caseoh_ (clipping restricted to followers/subs). Following big GTA
  streamers with the clipping account (vinsmokedav) opens up followers-only channels; sub-only stays closed.
- 2026-11-19: A just-created clip appears in Get Clips before its video is downloadable (404 for ~10-30s).
  `download_clip.py` now retries 404s (5 × 15s).
- 2026-11-18: Haiku copied the prompt's example hook pattern ("WAIT FOR THE …") — varied examples + passing the
  last 10 hooks fixed it. Watch for other repeated patterns in titles.
- 2026-11-19: YouTube `snippet.publishedAt` is not a reliable "went public" time: two Shorts uploaded private and
  made public later showed ~3 min after upload, and a still-private one showed a time from the next morning. The
  tracker uses "first seen public" instead.
- 2026-11-19: The growth video's delete/repost rule: a good Short stuck under ~100 views after 2 days probably
  missed the algorithm; delete it, change the title, re-upload on a different day (their 47-view flop went to
  ~6M on the re-upload). Built as the tracker's DELETE + repost flag; renders are deleted after upload, so a
  repost re-downloads the Twitch clip.
- 2026-11-19: User feedback: a BrycexD clip (scrolling an in-game phone, asking chat about a stock ticker) made no
  sense to a stranger, and chat spikes can come from the streamer asking chat a question. Added plan_edit's clarity
  score + the hype reaction gate. Score test on 11 known clips ($0.004): every bad one scored 2-3 (BrycexD 3, Lacy
  giveaway 2, Lacy driving 2, Elajjaz radio 2, jasontheween cutscene skip 2, xQc menu talk 3, caseoh_ seaplane 3);
  decent ones 5-7 (Quin69 5, MissMikkaa swamp monster 5, king_wolfe_ sharks 6, LIRIK jet-ski dog 7). Threshold set
  to 5. The biggest hype spike of the day (Lacy x13.9) was a giveaway — chat volume alone is not a moment.
- 2026-11-19: Reaction-gate dry run (12 min, 15 chats, mid-afternoon): 26 raw spikes, 4 passed. Ignored ones were
  polls ("1" x82 on Lacy, "no" x42 on stableronaldo, "me" x42 on RealKatieB), giveaway/copypasta spam ("delights"
  x121), emote spam and chat banter (<10% reactions). Passed ones were laughing chats at 35-48% reactions (KEKW/ICANT,
  Pog, lol/lul/lmao, "???"). "lol" and "LOLW" were missing from the laugh list (IShowSpeed and Quin69 laughing
  chats scored 16-22%) — added, plus HUH and OOOO. Expect roughly 1 in 6 raw spikes to pass.
- 2026-11-19: User's favorite Shorts so far ("the exact type of clips we want"): caseoh_ tumbling off a mobility
  scooter into a canal, and Jynxzi's first plane flight crashing into the woods. Pattern: one simple thing goes
  visibly wrong, payoff at the end, works on mute. The scorer had them at 5-6, so plan_edit now names them as the
  8-10 example and scores only the cut window (junk elsewhere in a raw 30s hype clip no longer drags a good moment
  down). Re-test: favorites 8/10, bad clips still 2, borderline 4-6. Upload order now prefers the higher score.
- 2026-11-19: The spoiler flag fired on gameplay during story missions (scooter fall, sharks, swamp monster: 3 of
  the first 6 uploads). Narrowed to plot reveals only (story cutscenes, character deaths/twists/endings); a real
  rooftop story cutscene is still flagged.
