"""Small helpers so tests read as the scenario they check."""

from dataclasses import dataclass
from itertools import count

from fastapi.testclient import TestClient

from relay_backend.pipeline import App, handle_utterance

_keys = count()


@dataclass
class Env:
    client: TestClient
    relay: App
    sid: str
    room_code: str
    people: dict[str, tuple[str, str]]  # key -> (participant id, token)

    def pid(self, who: str) -> str:
        return self.people[who][0]

    def headers(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.people[who][1]}"}


def act(env: Env, who: str, **body):
    """A manual action, as the request buttons send it."""
    body.setdefault("idempotency_key", f"test-{next(_keys)}")
    return env.client.post(f"/sessions/{env.sid}/actions", json=body, headers=env.headers(who))


def create(env: Env, who: str = "ana", assignee: str | None = "ben", **details) -> dict:
    r = act(env, who, type="create_request", description="Bring 4 mics to stage left",
            assignee_id=env.pid(assignee) if assignee else None, **details)
    assert r.status_code == 200, r.text
    return r.json()["request"]


def utter(env: Env, who: str, turn: str, text: str, mode: str = "team") -> None:
    """A finished speaking turn, run through the pipeline on the app's own event loop."""
    env.client.portal.call(handle_utterance, env.relay, env.sid, env.pid(who), turn, mode, text)


def snap(env: Env, who: str = "ana") -> dict:
    return env.client.get(f"/sessions/{env.sid}/snapshot", headers=env.headers(who)).json()
