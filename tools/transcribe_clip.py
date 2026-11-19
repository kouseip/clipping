"""Transcribe a clip locally with faster-whisper (free, runs on the NVIDIA GPU) into word-level timestamps.

    python tools/transcribe_clip.py <video.mp4> [--language en] [--model large-v3-turbo]

Writes .tmp/transcripts/<video stem>.json: {"language": ..., "words": [{"word", "start", "end", "prob"}]}
First run downloads the model (~1.6 GB for large-v3-turbo) into the Hugging Face cache.
"""

import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import numpy as np

from _common import TMP_DIR

TRANSCRIPTS_DIR = TMP_DIR / "transcripts"

# Whisper invents these on silence/music. Drop a segment if it's only one of these.
HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.", "thanks for watching.",
    "please subscribe.", "bye.", "you", "subtitles by the amara.org community",
}
MIN_WORD_PROB = 0.2   # below this the word is usually wrong; better no caption than a wrong one
TAIL_WORD_PROB = 0.4

_model = None


def _add_cuda_dlls():
    """pip's nvidia-* wheels put cuBLAS/cuDNN DLLs in site-packages/nvidia/<lib>/bin, which Windows doesn't search."""
    if os.name != "nt":
        return
    spec = importlib.util.find_spec("nvidia")
    if not spec or not spec.submodule_search_locations:
        return
    for base in spec.submodule_search_locations:
        for lib in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            d = Path(base) / lib / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


def load_model(name: str = "large-v3-turbo"):
    global _model
    if _model is None:
        _add_cuda_dlls()
        from faster_whisper import WhisperModel

        try:
            _model = WhisperModel(name, device="cuda", compute_type="float16")
        except Exception as e:  # no GPU / CUDA libs: still works, just slower
            print(f"CUDA unavailable ({e.__class__.__name__}: {e}); falling back to CPU int8")
            _model = WhisperModel(name, device="cpu", compute_type="int8")
    return _model


def load_audio(video: Path, sample_rate: int = 16000) -> np.ndarray:
    """Decode with ffmpeg rather than faster-whisper's PyAV path, which breaks on PyAV >= 19 (metadata_errors kwarg)."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(out.stdout, np.float32)


def transcribe(video: Path, language: str | None = None, model_name: str = "large-v3-turbo") -> Path:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    out = TRANSCRIPTS_DIR / f"{video.stem}.json"
    model = load_model(model_name)
    segments, info = model.transcribe(
        load_audio(video),
        language=language,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,  # stops Whisper looping on repeated phrases
    )
    words = []
    for seg in segments:
        if seg.text.strip().lower() in HALLUCINATIONS or seg.no_speech_prob > 0.8:
            continue
        seg_words = list(seg.words or [])
        for i, w in enumerate(seg_words):
            if w.probability < MIN_WORD_PROB:
                continue
            # Invented tail words ("you", "thank you") come in long and low-confidence at the end of a segment.
            if i == len(seg_words) - 1 and w.probability < TAIL_WORD_PROB and w.end - w.start >= 0.5:
                continue
            words.append({"word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3), "prob": round(w.probability, 3)})
    out.write_text(json.dumps({"language": info.language, "words": words}, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--language", default=None)
    ap.add_argument("--model", default="large-v3-turbo")
    args = ap.parse_args()
    path = transcribe(args.video, args.language, args.model)
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"{path}  ({len(data['words'])} words, lang={data['language']})")
    print(" ".join(w["word"] for w in data["words"])[:500])
