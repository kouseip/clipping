"""Grab evenly spaced JPEG frames from a video (for sending to a vision model)."""

import base64
import subprocess
from pathlib import Path


def duration(video: Path) -> float:
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip())


def sample_frames(video: Path, count: int, width: int, start: float = 0.0, end: float = 1.0) -> list[str]:
    """count base64 JPEGs spread between start and end (fractions of the duration), scaled to width px."""
    dur = duration(video)
    out = []
    for i in range(count):
        t = dur * (start + (end - start) * (i + 0.5) / count)
        jpg = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, check=True,
        ).stdout
        if jpg:
            out.append(base64.standard_b64encode(jpg).decode())
    return out


def image_blocks(frames: list[str]) -> list[dict]:
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": f}} for f in frames]
