"""Deepgram streaming adapter: one live websocket per speaking turn."""

import asyncio
import json
import logging
import ssl
from collections.abc import Callable
from urllib.parse import urlencode

import certifi
import websockets

from . import config

log = logging.getLogger("relay.stt")
SAMPLE_RATE = 16000
# Use certifi's CA bundle: python.org builds of Python on macOS ship without system CAs.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class LiveTranscription:
    """Feed PCM16 mono 16 kHz with send(); finish() returns the final transcript."""

    def __init__(self, on_interim: Callable[[str], None]):
        self.on_interim = on_interim
        self.finals: list[str] = []
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._pending: list[bytes] = []
        self._closed = asyncio.Event()
        self.error: str | None = None

    async def start(self) -> None:
        if not config.DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not set")
        params = urlencode({
            "model": config.DEEPGRAM_MODEL, "language": "en", "encoding": "linear16",
            "sample_rate": SAMPLE_RATE, "channels": 1, "interim_results": "true",
            "punctuate": "true",  # no smart_format: it rewrites "six thirty" as "06:30"
        })
        ws = await websockets.connect(
            f"wss://api.deepgram.com/v1/listen?{params}",
            additional_headers={"Authorization": f"Token {config.DEEPGRAM_API_KEY}"},
            open_timeout=5,
            ssl=SSL_CONTEXT,
        )
        # Flush audio buffered while connecting before send() may write directly, so the
        # first words stay in order. Frames that arrive during the flush join the buffer.
        while self._pending:
            await ws.send(self._pending.pop(0))
        self._ws = ws
        self._reader = asyncio.create_task(self._read())

    async def send(self, pcm: bytes) -> None:
        if self._ws is None:
            self._pending.append(pcm)
            return
        try:
            await self._ws.send(pcm)
        except websockets.ConnectionClosed:
            self.error = self.error or "transcription connection closed"

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if msg.get("type") != "Results":
                    continue
                alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
                text = (alt.get("transcript") or "").strip()
                if msg.get("is_final"):
                    if text:
                        self.finals.append(text)
                    self.on_interim(" ".join(self.finals))
                elif text:
                    self.on_interim(" ".join([*self.finals, text]))
        except websockets.ConnectionClosed as e:
            if e.rcvd and e.rcvd.code not in (1000, 1001):
                self.error = f"transcription closed: {e.rcvd.reason or e.rcvd.code}"
        except Exception as e:  # noqa: BLE001
            log.exception("deepgram read failed")
            self.error = str(e)
        finally:
            self._closed.set()

    async def finish(self, timeout: float = 3.0) -> str:
        """End input, wait (bounded) for the last results, and return the utterance."""
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "Finalize"}))
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await asyncio.wait_for(self._closed.wait(), timeout)
            except (TimeoutError, websockets.ConnectionClosed):
                pass
            finally:
                await self._ws.close()
        if self.error and not self.finals:
            raise RuntimeError(self.error)
        return " ".join(self.finals).strip()

    async def abort(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._reader:
            self._reader.cancel()
