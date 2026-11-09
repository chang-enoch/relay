"""Environment configuration. Keys live in backend/.env and never reach the browser."""

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.6-flash")

LIVEKIT_URL = _env("LIVEKIT_URL")
LIVEKIT_API_KEY = _env("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = _env("LIVEKIT_API_SECRET")

DEEPGRAM_API_KEY = _env("DEEPGRAM_API_KEY")
DEEPGRAM_MODEL = _env("DEEPGRAM_MODEL", "nova-3")

ELEVENLABS_API_KEY = _env("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = _env("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
ELEVENLABS_MODEL = _env("ELEVENLABS_MODEL", "eleven_flash_v2_5")

DATABASE_PATH = _env("RELAY_DB", str(BACKEND_DIR / "relay.db"))
CORS_ORIGINS = [o for o in _env("RELAY_CORS_ORIGINS", "http://localhost:3000").split(",") if o]
RETENTION_DAYS = int(_env("RELAY_RETENTION_DAYS", "7"))
CLARIFICATION_TTL_SECONDS = int(_env("RELAY_CLARIFICATION_TTL", "300"))
TURN_LEASE_SECONDS = float(_env("RELAY_TURN_LEASE", "5"))


# Which keys each provider needs. Drives the status pills and the startup warning.
PROVIDER_KEYS = {
    "audio": ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"),
    "transcription": ("DEEPGRAM_API_KEY",),
    "ai": ("GEMINI_API_KEY",),
    "speech": ("ELEVENLABS_API_KEY",),
}


def provider_status() -> dict[str, bool]:
    """Which providers have credentials configured."""
    return {name: all(globals()[k] for k in keys) for name, keys in PROVIDER_KEYS.items()}


def missing_keys() -> list[str]:
    return [k for keys in PROVIDER_KEYS.values() for k in keys if not globals()[k]]
