"""Shared fixtures. Every test runs against a throwaway database with stubbed AI providers."""

import os
import tempfile

# src.main builds a default app at import time, which opens RELAY_DB.
# Point it at a throwaway file before anything imports the package, so tests never
# touch the developer's live database.
os.environ["RELAY_DB"] = os.path.join(tempfile.mkdtemp(prefix="relay-tests-"), "import.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from helpers import Env  # noqa: E402
from src import llm as real_llm  # noqa: E402
from src.main import create_app  # noqa: E402


class StubLLM:
    """Replays queued results instead of calling Gemini, and records every prompt."""

    llm_input = staticmethod(real_llm.llm_input)

    def __init__(self):
        self.team: list = []  # TeamResult, or callable(payload) -> TeamResult
        self.answers: list = []  # async callable(payload, run_tool) -> Answer
        self.payloads: list[str] = []

    async def interpret_team(self, payload):
        self.payloads.append(payload)
        nxt = self.team.pop(0)
        return nxt(payload) if callable(nxt) else nxt

    async def answer_question(self, payload, run_tool):
        self.payloads.append(payload)
        return await self.answers.pop(0)(payload, run_tool)


class StubTTS:
    @staticmethod
    async def synthesize(text):
        return b"mp3"


@pytest.fixture
def env(tmp_path):
    """A room created by Ana (Stage manager), joined by Ben (Audio tech) and Cy (Lighting)."""
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        relay = app.state.relay
        relay.llm = StubLLM()
        relay.tts = StubTTS()
        r = client.post("/sessions", json={"room_name": "Gig", "setting": "Concert load-in",
                                           "name": "Ana", "role": "Stage manager"}).json()
        people = {"ana": (r["participant_id"], r["token"])}
        for key, name, role in [("ben", "Ben", "Audio tech"), ("cy", "Cy", "Lighting")]:
            j = client.post("/sessions/join", json={"room_code": r["room_code"], "name": name,
                                                    "role": role}).json()
            people[key] = (j["participant_id"], j["token"])
        yield Env(client, relay, r["session_id"], r["room_code"], people)
