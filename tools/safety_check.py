"""Check a clip against YouTube's content rules before it's rendered, using Claude Haiku 5.5 vision.

Samples a frame every ~2 seconds (max 30) plus the transcript and asks whether the clip would likely be
removed or age-restricted on YouTube (age-restricted Shorts get almost no reach, so that counts as a fail).
Normal GTA content — shootouts, crashes, blood, swearing, swimwear in ordinary scenes — passes.
Costs roughly $0.0005–0.001 per clip.

    python tools/safety_check.py <video.mp4> [--transcript t.json]

Same call also flags main-story spoilers (cutscenes, plot moments) so the title can be prefixed "SPOILER:".

Returns ("ok" | "reject", reason, cost, is_spoiler). Verdict None means the check couldn't run
(no API key, API error) — callers should hold the clip and retry, not let it through unchecked.
"""

import argparse
import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from _common import ROOT
from frames import duration, image_blocks, sample_frames

MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6

SYSTEM = """You review short gameplay clips (GTA 6, streamed on Twitch, with the streamer's facecam and chat overlay) before they are posted as YouTube Shorts. Decide whether the clip would likely be REMOVED or AGE-RESTRICTED under YouTube's Community Guidelines. Check every frame and every part of each frame, including the facecam.

REJECT if any frame shows:
- Nudity, even in-game or partially obscured: exposed breasts or nipples, genitals, or bare buttocks.
- Sexual activity or simulated sex, including implied sex scenes.
- Sexually suggestive focus: lap dances, pole dancing, twerking, or the camera lingering on a body in a sexualized way.
- Extremely graphic gore: close-up dismemberment, mutilation, or exposed organs.
Also REJECT if the transcript is sexually explicit.

These are FINE (normal for the game, do not reject): shootouts, explosions, car crashes, police chases, ordinary blood, fistfights, swearing, drinking or smoking, swimwear or revealing outfits in ordinary non-sexual scenes (beach, pool, walking around), dark humor.

Separately, decide if the clip is a STORY SPOILER: it shows a main-story cutscene or main-story mission moment (plot events, character reveals, deaths, betrayals, twists, endings - including the prologue/opening story scenes). Free-roam gameplay, driving, chaos, side activities, menus, and the streamer just reacting to the map or graphics are NOT spoilers.

Reply with exactly one line, one of:
OK
OK SPOILER
REJECT: <short reason>"""


def check_clip(video: Path, transcript_text: str = "") -> tuple[str | None, str, float, bool]:
    """Returns (verdict, reason, cost, is_spoiler)."""
    load_dotenv(ROOT / ".env", override=True)
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None, "no ANTHROPIC_API_KEY", 0.0, False

    count = max(8, min(30, int(duration(video) / 2)))
    content = image_blocks(sample_frames(video, count, width=480))
    content.append({"type": "text", "text": f"{count} frames in order, ~2s apart.\nTranscript: {transcript_text or '(no speech)'}"})

    try:
        response = anthropic.Anthropic().messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        return None, "invalid ANTHROPIC_API_KEY", 0.0, False
    except anthropic.RateLimitError:
        return None, "rate limited", 0.0, False
    except anthropic.APIStatusError as e:
        return None, f"API error {e.status_code}", 0.0, False
    except anthropic.APIConnectionError:
        return None, "network error", 0.0, False

    cost = response.usage.input_tokens * PRICE_IN + response.usage.output_tokens * PRICE_OUT
    if response.stop_reason == "refusal":  # model wouldn't look at it: treat as unsafe
        return "reject", "model refused to review (likely explicit)", cost, False
    line = next((b.text for b in response.content if b.type == "text"), "").strip().splitlines()
    line = line[0].strip() if line else ""
    if line.upper().startswith("OK"):
        return "ok", "", cost, "SPOILER" in line.upper()
    if line.upper().startswith("REJECT"):
        return "reject", line.split(":", 1)[-1].strip() or "unspecified", cost, False
    return None, f"unparseable reply: {line[:80]!r}", cost, False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--transcript", type=Path, default=None)
    args = ap.parse_args()
    words = json.loads(args.transcript.read_text(encoding="utf-8"))["words"] if args.transcript else []
    verdict, reason, cost, spoiler = check_clip(args.video, " ".join(w["word"] for w in words))
    print(f"verdict: {verdict}\nspoiler: {spoiler}\nreason:  {reason}\ncost:    ${cost:.5f}")
