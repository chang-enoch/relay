"""Environment configuration. Keys live in backend/.env and never reach the browser."""

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.6-flash")

OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-5-mini")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL")  # optional: any OpenAI-compatible endpoint

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-opus-5-5")
ANTHROPIC_EFFORT = _env("ANTHROPIC_EFFORT", "low")  # empty for models without effort support

# Which LLM vendor runs summaries, requests and Ask Relay: its key and its model setting.
LLM_KEYS = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
LLM_MODELS = {"gemini": "GEMINI_MODEL", "openai": "OPENAI_MODEL", "anthropic": "ANTHROPIC_MODEL"}


def pick_llm_provider(explicit: str, configured_keys: set[str]) -> str:
    """LLM_PROVIDER if set; otherwise the first vendor with a key (Gemini when none has one)."""
    if explicit:
        if explicit not in LLM_KEYS:
            raise ValueError(f"LLM_PROVIDER must be one of {', '.join(LLM_KEYS)}, not {explicit!r}")
        return explicit
    return next((name for name, key in LLM_KEYS.items() if key in configured_keys), "gemini")


LLM_PROVIDER = pick_llm_provider(_env("LLM_PROVIDER").lower(),
                                 {k for k in LLM_KEYS.values() if globals()[k]})
LLM_MODEL = globals()[LLM_MODELS[LLM_PROVIDER]]

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
    "ai": (LLM_KEYS[LLM_PROVIDER],),
    "speech": ("ELEVENLABS_API_KEY",),
}


def provider_status() -> dict[str, bool]:
    """Which providers have credentials configured."""
    return {name: all(globals()[k] for k in keys) for name, keys in PROVIDER_KEYS.items()}


def missing_keys() -> list[str]:
    return [k for keys in PROVIDER_KEYS.values() for k in keys if not globals()[k]]
