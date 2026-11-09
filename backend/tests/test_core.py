"""Core behaviour with a stubbed LLM: lifecycle, permissions, versions, dedupe,
read-only questions and private-exchange isolation."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from relay_backend import llm as real_llm
from relay_backend.llm import Answer, TeamResult
from relay_backend.main import create_app
from relay_backend.pipeline import handle_utterance


class StubLLM:
    llm_input = staticmethod(real_llm.llm_input)

    def __init__(self):
        self.team: list = []          # queue of TeamResult or callables(payload)->TeamResult
        self.answers: list = []       # queue of callables(payload, run_tool)->Answer
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
    app = create_app(str(tmp_path / "t.db"))
    client = TestClient(app)
    relay = app.state.relay
    relay.llm = StubLLM()
    relay.tts = StubTTS()
    r = client.post("/sessions", json={"room_name": "Gig", "setting": "Concert load-in",
                                       "name": "Ana", "role": "Stage manager"}).json()
    sid = r["session_id"]
    people = {"ana": (r["participant_id"], r["token"])}
    for key, name, role in [("ben", "Ben", "Audio tech"), ("cy", "Cy", "Lighting")]:
        j = client.post("/sessions/join", json={"room_code": r["room_code"], "name": name, "role": role}).json()
        people[key] = (j["participant_id"], j["token"])
    return client, relay, sid, people


def H(people, who):
    return {"Authorization": f"Bearer {people[who][1]}"}


def act(client, sid, people, who, **body):
    body.setdefault("idempotency_key", f"k-{who}-{body['type']}-{body.get('request_id', '')}-{id(body)}")
    return client.post(f"/sessions/{sid}/actions", json=body, headers=H(people, who))


def create(client, sid, people, who="ana", assignee="ben", **details):
    r = act(client, sid, people, who, type="create_request", description="Bring 4 mics to stage left",
            assignee_id=people[assignee][0] if assignee else None, **details)
    assert r.status_code == 200, r.text
    return r.json()["request"]


def utter(relay, sid, pid, turn, text, mode="team"):
    asyncio.run(handle_utterance(relay, sid, pid, turn, mode, text))


def snap(client, sid, people, who="ana"):
    return client.get(f"/sessions/{sid}/snapshot", headers=H(people, who)).json()


# ---------------------------------------------------------------- lifecycle

def test_lifecycle_and_highlight_rules(env):
    client, _, sid, people = env
    req = create(client, sid, people)
    assert req["status"] == "open" and req["version"] == 1

    r = act(client, sid, people, "ben", type="complete", request_id=req["id"], expected_version=1)
    assert r.status_code == 400 and r.json()["code"] == "invalid_transition"

    r = act(client, sid, people, "ben", type="accept", request_id=req["id"], expected_version=1, estimate="10 min")
    req = r.json()["request"]
    assert req["status"] == "accepted" and req["accepted_revision"] == 1
    assert req["details"]["estimate"] == "10 min"

    req = act(client, sid, people, "ben", type="report_progress", request_id=req["id"],
              expected_version=2, started=True).json()["request"]
    assert req["status"] == "in_progress"
    req = act(client, sid, people, "ben", type="complete", request_id=req["id"], expected_version=3).json()["request"]
    assert req["status"] == "completed" and req["closed_at"]

    r = act(client, sid, people, "ana", type="cancel", request_id=req["id"], expected_version=4)
    assert r.json()["code"] == "invalid_transition"


def test_material_change_requires_reacceptance(env):
    client, _, sid, people = env
    req = create(client, sid, people, quantity="4", destination="stage left")
    req = act(client, sid, people, "ben", type="accept", request_id=req["id"], expected_version=1).json()["request"]
    r = act(client, sid, people, "ana", type="update_request", request_id=req["id"], expected_version=2,
            changes={"quantity": "6"})
    out = r.json()
    req = out["request"]
    assert req["status"] == "open" and req["instruction_revision"] == 2 and req["accepted_revision"] is None
    assert out["announcements"] and "please confirm" in out["announcements"][0]["message"]
    # The headline is kept in step with the changed field ("Bring 4 mics" -> "Bring 6 mics").
    r2 = act(client, sid, people, "ana", type="update_request", request_id=req["id"], expected_version=3,
             changes={"destination": "stage right"}).json()["request"]
    assert out["request"]["details"]["description"] == "Bring 6 mics to stage left"
    assert r2["details"]["description"] == "Bring 6 mics to stage right"
    req = act(client, sid, people, "ana", type="update_request", request_id=req["id"], expected_version=4,
              changes={"destination": "stage left"}).json()["request"]

    # A comment / estimate is not material.
    req = act(client, sid, people, "ana", type="update_request", request_id=req["id"], expected_version=5,
              changes={"notes": "use the blue case"}).json()["request"]
    assert req["instruction_revision"] == 4

    req = act(client, sid, people, "ben", type="accept", request_id=req["id"], expected_version=6).json()["request"]
    assert req["accepted_revision"] == 4


def test_reassign_clears_acceptance(env):
    client, _, sid, people = env
    req = create(client, sid, people)
    req = act(client, sid, people, "ben", type="accept", request_id=req["id"], expected_version=1).json()["request"]
    req = act(client, sid, people, "ana", type="reassign", request_id=req["id"], expected_version=2,
              assignee_id=people["cy"][0]).json()["request"]
    assert req["assignee_id"] == people["cy"][0] and req["status"] == "open" and req["accepted_revision"] is None


# ---------------------------------------------------------------- permissions

def test_permissions_enforced(env):
    client, _, sid, people = env
    req = create(client, sid, people, who="ben", assignee="cy")
    # Not the assignee.
    assert act(client, sid, people, "ana", type="accept", request_id=req["id"], expected_version=1).status_code == 403
    # Not the requester and no manage permission.
    assert act(client, sid, people, "cy", type="cancel", request_id=req["id"], expected_version=1).status_code == 403
    # Creator holds manage_requests.
    assert act(client, sid, people, "ana", type="cancel", request_id=req["id"], expected_version=1).status_code == 200
    # Only the creator manages permissions.
    r = act(client, sid, people, "ben", type="set_permissions", participant_id=people["cy"][0],
            permissions=["manage_requests"])
    assert r.status_code == 403


def test_cross_session_isolation(env, tmp_path):
    client, _, sid, people = env
    req = create(client, sid, people)
    other = client.post("/sessions", json={"room_name": "Other", "name": "Zed", "role": "x"}).json()
    hz = {"Authorization": f"Bearer {other['token']}"}
    r = client.post(f"/sessions/{other['session_id']}/actions", headers=hz,
                    json={"type": "cancel", "request_id": req["id"], "expected_version": 1, "idempotency_key": "z"})
    assert r.status_code == 404
    # A token from one room cannot read another.
    assert client.get(f"/sessions/{sid}/snapshot", headers=hz).status_code == 401


def test_unassigned_role_request_can_be_claimed_by_matching_role(env):
    client, _, sid, people = env
    req = create(client, sid, people, assignee=None, target_role="lighting")
    assert act(client, sid, people, "ben", type="accept", request_id=req["id"], expected_version=1).status_code == 403
    req = act(client, sid, people, "cy", type="accept", request_id=req["id"], expected_version=1).json()["request"]
    assert req["assignee_id"] == people["cy"][0]


# ---------------------------------------------------------------- versions & idempotency

def test_version_conflict_and_idempotency(env):
    client, _, sid, people = env
    req = create(client, sid, people)
    act(client, sid, people, "ana", type="update_request", request_id=req["id"], expected_version=1,
        changes={"notes": "n1"})
    r = act(client, sid, people, "ana", type="update_request", request_id=req["id"], expected_version=1,
            changes={"notes": "stale"})
    assert r.status_code == 409

    body = dict(type="update_request", request_id=req["id"], expected_version=2, changes={"notes": "n2"},
                idempotency_key="same")
    first = client.post(f"/sessions/{sid}/actions", json=body, headers=H(people, "ana")).json()
    again = client.post(f"/sessions/{sid}/actions", json=body, headers=H(people, "ana")).json()
    assert again["duplicate"] and again["request"]["version"] == first["request"]["version"] == 3


# ---------------------------------------------------------------- pipeline

def test_utterance_pipeline_and_duplicate_suppression(env):
    client, relay, sid, people = env
    ana, ben = people["ana"][0], people["ben"][0]
    relay.llm.team.append(TeamResult("team_message", "Ben: bring 4 mics to stage left.", None, [
        {"type": "create_request", "description": "Bring 4 mics to stage left", "quantity": "4",
         "assignee_id": ben},
    ]))
    utter(relay, sid, ana, "turn1", "uh so Ben can you bring like four mics to stage left")
    utter(relay, sid, ana, "turn1", "uh so Ben can you bring like four mics to stage left")  # duplicate
    s = snap(client, sid, people)
    assert len(s["messages"]) == 1 and len(s["requests"]) == 1
    msg, req = s["messages"][0], s["requests"][0]
    assert req["source_message_id"] == msg["id"] and msg["request_ids"] == [req["id"]]
    assert msg["original_transcript"].startswith("uh so Ben")

    payload = relay.llm.payloads[0]
    for key in ('"user_id"', '"name": "Ana"', '"role": "Stage manager"', '"content"',
                '"source_utterance_id"', '"mode": "team"', '"current_state"'):
        assert key in payload

    # Assignee says it's done while still open: accept then complete, both against the same snapshot.
    relay.llm.team.append(TeamResult("team_message", "Ben: mics are on stage left.", None, [
        {"type": "accept", "request_id": req["id"], "expected_version": 1},
        {"type": "complete", "request_id": req["id"], "expected_version": 1},
    ]))
    utter(relay, sid, ben, "turn2", "done, mics are on stage left")
    assert snap(client, sid, people)["requests"][0]["status"] == "completed"


def test_invalid_proposal_changes_nothing(env):
    client, relay, sid, people = env
    req = create(client, sid, people)
    # Cy tries to complete Ben's request by voice.
    relay.llm.team.append(TeamResult("team_message", "Cy says the mics are done.", None, [
        {"type": "complete", "request_id": req["id"], "expected_version": 1}]))
    utter(relay, sid, people["cy"][0], "t1", "the mics are done")
    s = snap(client, sid, people)
    assert s["requests"][0]["status"] == "open" and s["requests"][0]["version"] == 1
    assert len(s["messages"]) == 1  # the message is still recorded


def test_ai_failure_leaves_state_unchanged(env):
    client, relay, sid, people = env

    def boom(_):
        raise RuntimeError("model down")
    relay.llm.team.append(boom)
    utter(relay, sid, people["ana"][0], "t1", "Ben bring mics")
    s = snap(client, sid, people)
    assert s["messages"] == [] and s["requests"] == []


def test_clarification_is_recorded_and_resolved(env):
    client, relay, sid, people = env
    relay.llm.team.append(TeamResult("team_message", "Ana asks for those microphones.", None, [
        {"type": "ask_clarification", "question": "Which microphones do you mean?"}]))
    utter(relay, sid, people["ana"][0], "t1", "bring those microphones")
    s = snap(client, sid, people)
    assert len(s["clarifications"]) == 1 and s["requests"] == []
    cid = s["clarifications"][0]["id"]
    relay.llm.team.append(TeamResult("clarification", "The wireless ones.", cid, []))
    utter(relay, sid, people["ana"][0], "t2", "the wireless ones")
    assert snap(client, sid, people)["clarifications"] == []


# ---------------------------------------------------------------- questions

def test_questions_are_read_only_and_private(env):
    client, relay, sid, people = env
    ana, ben = people["ana"][0], people["ben"][0]
    req = create(client, sid, people)
    before = snap(client, sid, people)

    async def ans(payload, run_tool):
        status = await run_tool("get_status", {})
        assert status["outstanding"][0]["id"] == req["id"]
        forbidden = await run_tool("create_request", {"description": "sneaky"})
        assert "error" in forbidden
        return Answer("Ben is bringing 4 mics; not accepted yet.", ["msg_bogus"])
    relay.llm.answers.append(ans)
    utter(relay, sid, ana, "q1", "what is still outstanding?", mode="ask")

    after = snap(client, sid, people)
    assert after["requests"] == before["requests"] and after["messages"] == before["messages"]
    assert len(after["exchanges"]) == 1
    ex = after["exchanges"][0]
    assert ex["source_message_ids"] == []  # unknown ids are dropped
    # Ben cannot see Ana's exchange in snapshot or replay.
    assert snap(client, sid, people, "ben")["exchanges"] == []
    events = client.get(f"/sessions/{sid}/events?after=0", headers=H(people, "ben")).json()["events"]
    assert not any(e["type"].startswith("ask.") for e in events)
    # Nor can Ben share it.
    assert client.post(f"/sessions/{sid}/answers/{ex['id']}/share", headers=H(people, "ben")).status_code == 404

    # Ana shares: a labelled copy appears, without the private question.
    r = client.post(f"/sessions/{sid}/answers/{ex['id']}/share", headers=H(people, "ana")).json()["message"]
    assert r["kind"] == "shared_answer" and r["original_transcript"] is None
    msgs = snap(client, sid, people, "ben")["messages"]
    assert msgs[-1]["kind"] == "shared_answer" and msgs[-1]["generated_at"]


def test_bot_question_in_shared_channel_routes_to_answer(env):
    client, relay, sid, people = env
    relay.llm.team.append(TeamResult("bot_question", "", None, [
        {"type": "create_request", "description": "should be ignored"}]))

    async def ans(payload, run_tool):
        assert '"mode": "question"' in payload
        return Answer("Nothing outstanding.", [])
    relay.llm.answers.append(ans)
    utter(relay, sid, people["ben"][0], "t1", "Relay, what's outstanding?")
    s = snap(client, sid, people, "ben")
    assert s["requests"] == [] and s["messages"] == [] and len(s["exchanges"]) == 1


def test_private_exchanges_excluded_from_state(env):
    client, relay, sid, people = env

    async def ans(payload, run_tool):
        return Answer("SECRET-ANSWER", [])
    relay.llm.answers.append(ans)
    utter(relay, sid, people["ana"][0], "q1", "SECRET-QUESTION", mode="ask")
    relay.llm.team.append(TeamResult("team_message", "hi", None, []))
    utter(relay, sid, people["ben"][0], "t1", "hello")
    assert "SECRET" not in relay.llm.payloads[-1]


def test_internal_ids_are_stripped_from_user_facing_text():
    assert real_llm.clean("Alex is bringing the mics (msg_3057847fffd14517).") == "Alex is bringing the mics."
    assert real_llm.clean("Updated [req_0123456789abcdef] for Cy.") == "Updated for Cy."
    assert real_llm.clean("Nothing outstanding.") == "Nothing outstanding."
