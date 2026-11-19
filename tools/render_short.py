"""Render a 16:9 clip into a 1080x1920 YouTube Short with burned-in word-by-word captions.

Layout: blurred, darkened copy of the clip fills the background; the full clip sits slightly above
center; "@streamer" credit above it; captions below it (clear of the Shorts UI at the bottom).

    python tools/render_short.py <video.mp4> <transcript.json> --streamer NAME [--out out.mp4]

Uses NVENC (GPU) when available, falls back to libx264.
"""

import argparse
import json
import re
import subprocess
from pathlib import Path

from _common import ROOT, TMP_DIR

RENDERS_DIR = TMP_DIR / "renders"
FONTS_DIR = ROOT / "assets" / "fonts"
FONT = "Montserrat ExtraBold"

W, H = 1080, 1920
MAX_WORDS_PER_PAGE = 3
MAX_CHARS_PER_PAGE = 18
PAUSE_BREAK_SEC = 0.6  # a pause this long starts a new caption page
# Shorts over 1 minute get blocked outright if they carry a Content ID claim (GTA radio music is often claimed).
MAX_SHORT_SEC = 59.0
HIGHLIGHT = "&H0000E5FF"  # ASS colours are &HAABBGGRR -> yellow #FFE500
WHITE = "&H00FFFFFF"
BLACK = "&H00000000"


def probe(video: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(out.stdout)
    s = info["streams"][0]
    return int(s["width"]), int(s["height"]), float(info["format"]["duration"])


def clean_word(word: str) -> str:
    word = re.sub(r"[{}\\]", "", word).strip()
    word = word.strip(".,;:\"“”…-–—")  # keep ? and ! — they carry tone
    return word.upper()


def build_pages(words: list[dict]) -> list[list[dict]]:
    pages, cur = [], []
    for w in words:
        text = clean_word(w["word"])
        if not text:
            continue
        item = {"text": text, "start": w["start"], "end": w["end"]}
        if cur:
            too_many = len(cur) >= MAX_WORDS_PER_PAGE
            too_long = len(" ".join(x["text"] for x in cur)) + 1 + len(text) > MAX_CHARS_PER_PAGE
            paused = w["start"] - cur[-1]["end"] > PAUSE_BREAK_SEC
            if too_many or too_long or paused:
                pages.append(cur)
                cur = []
        cur.append(item)
        if w["word"].rstrip().endswith((".", "?", "!")):  # end of sentence ends the page
            pages.append(cur)
            cur = []
    if cur:
        pages.append(cur)
    return pages


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _wrap_hook(hook: str, max_line: int = 18) -> str:
    """Split a hook into at most two balanced lines (WrapStyle 2 means ASS won't wrap by itself)."""
    hook = re.sub(r"[{}\\]", "", hook).upper()
    if len(hook) <= max_line:
        return hook
    words = hook.split()
    best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))), default=0)
    return " ".join(words[:best]) + "\\N" + " ".join(words[best:]) if best else hook


def build_ass(words: list[dict], streamer: str, duration: float, caption_y: int, credit_y: int,
              hook: str | None = None, hook_y: int = 0) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{FONT},92,{WHITE},{WHITE},&H00000000,&H99000000,0,0,0,0,100,100,1,0,1,7,4,5,40,40,0,1
Style: Credit,{FONT},50,{WHITE},{WHITE},&H00000000,&H99000000,0,0,0,0,100,100,1,0,1,5,2,5,40,40,0,1
Style: Hook,{FONT},68,{BLACK},{BLACK},{HIGHLIGHT},&H00000000,0,0,0,0,100,100,1,0,3,16,0,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{ass_time(0)},{ass_time(duration)},Credit,,0,0,0,,{{\\pos({W // 2},{credit_y})}}@{streamer}"]
    if hook:
        # Yellow banner above the clip from the very first frame, with a quick pop-in.
        pop = "\\fscx70\\fscy70\\t(0,120,\\fscx100\\fscy100)"
        lines.append(f"Dialogue: 2,{ass_time(0)},{ass_time(duration)},Hook,,0,0,0,,{{\\pos({W // 2},{hook_y}){pop}}}{_wrap_hook(hook)}")
    pages = build_pages(words)
    for p_idx, page in enumerate(pages):
        next_start = pages[p_idx + 1][0]["start"] if p_idx + 1 < len(pages) else duration
        page_end = min(page[-1]["end"] + 0.25, next_start)
        for i, w in enumerate(page):
            start = w["start"]
            end = page[i + 1]["start"] if i + 1 < len(page) else page_end
            if end - start < 0.05:
                end = start + 0.05
            text = " ".join(
                (f"{{\\c{HIGHLIGHT}}}{x['text']}{{\\c{WHITE}}}" if j == i else x["text"]) for j, x in enumerate(page)
            )
            pop = "\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)" if i == 0 else ""
            lines.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{{\\pos({W // 2},{caption_y}){pop}}}{text}")
    return header + "\n".join(lines) + "\n"


def _encode(cmd_base: list[str], encoder_args: list[str], out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd_base + encoder_args + [str(out)], cwd=ROOT, capture_output=True, text=True)


def render(video: Path, transcript: Path, streamer: str, out: Path | None = None,
           start: float = 0.0, end: float | None = None, hook: str | None = None) -> Path:
    """start/end trim the clip (seconds in the source); hook is drawn as a banner from the first frame."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    video = video.resolve()  # ffmpeg runs with cwd=ROOT, so caller-relative paths would break
    out = (out or RENDERS_DIR / f"{video.stem}_short.mp4").resolve()
    src_w, src_h, src_duration = probe(video)
    end = min(end or src_duration, src_duration)
    duration = min(end - start, MAX_SHORT_SEC)

    fg_h = round(W * src_h / src_w / 2) * 2
    fg_y = (H - fg_h) // 2 - 160
    caption_y = fg_y + fg_h + 180
    credit_y = fg_y - 70
    hook_y = credit_y - 170

    # Shift word timings onto the trimmed timeline and drop words outside the window.
    words = [
        {**w, "start": max(0.0, w["start"] - start), "end": w["end"] - start}
        for w in json.loads(transcript.read_text(encoding="utf-8"))["words"]
        if w["end"] > start + 0.05 and w["start"] < start + duration
    ]
    ass_path = RENDERS_DIR / f"{video.stem}.ass"
    ass_path.write_text(build_ass(words, streamer, duration, caption_y, credit_y, hook, hook_y), encoding="utf-8")

    # Relative, forward-slash paths keep ffmpeg's filter parser away from Windows "C:" escaping.
    ass_rel = ass_path.relative_to(ROOT).as_posix()
    fonts_rel = FONTS_DIR.relative_to(ROOT).as_posix()
    vf = (
        f"[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
        f"scale={W}:{H},eq=brightness=-0.12:saturation=1.2,setsar=1[bg];"
        f"[0:v]scale={W}:{fg_h},setsar=1[fg];"
        f"[bg][fg]overlay=0:{fg_y},subtitles=filename='{ass_rel}':fontsdir='{fonts_rel}',format=yuv420p[v]"
    )
    base = [
        # -ss before -i seeks the input, so the trimmed clip (and the subtitles) start at t=0.
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(video),
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-t", f"{duration:.3f}",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",  # consistent loudness across streamers
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
    ]
    result = _encode(base, ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                            "-maxrate", "14M", "-bufsize", "28M", "-profile:v", "high"], out)
    if result.returncode != 0:
        result = _encode(base, ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high"], out)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-1500:]}")
    ass_path.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--streamer", required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--hook", default=None)
    args = ap.parse_args()
    print(render(args.video, args.transcript, args.streamer, args.out, args.start, args.end, args.hook))
