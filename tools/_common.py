"""Shared helpers for all tools: project paths, .env loading, and config.json.

Usage from any tool in this folder:
    from _common import ROOT, TMP_DIR, DATA_DIR, get_env, load_config
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows consoles default to cp1252, which crashes on the emoji common in Twitch titles.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"
TMP_DIR.mkdir(exist_ok=True)
# Persistent state (clip database, logs). Not disposable like .tmp — losing it means duplicate uploads.
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")


def get_env(name: str, default: str | None = None, required: bool = True) -> str | None:
    """Read a value from .env / the environment. Exits with a clear message if a required key is missing."""
    value = os.getenv(name, default)
    if required and not value:
        sys.exit(f"Missing required env var {name!r}. Add it to {ROOT / '.env'} (see .env.example).")
    return value


def load_config() -> dict:
    """Non-secret pipeline settings from config.json (secrets stay in .env)."""
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
