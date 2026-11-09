"""ElevenLabs text-to-speech adapter. Returns mp3 bytes."""

import httpx

from . import config


async def synthesize(text: str) -> bytes:
    if not config.ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVENLABS_VOICE_ID}",
            params={"output_format": "mp3_44100_64"},
            headers={"xi-api-key": config.ELEVENLABS_API_KEY, "accept": "audio/mpeg"},
            json={"text": text, "model_id": config.ELEVENLABS_MODEL},
        )
        resp.raise_for_status()
        return resp.content
