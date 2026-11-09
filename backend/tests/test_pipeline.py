"""The voice pipeline: utterance -> model proposals -> engine -> one commit."""

from helpers import create, snap, utter
from relay_backend.llm import TeamResult


def test_utterance_pipeline_and_duplicate_suppression(env):
    ben = env.pid("ben")
    env.relay.llm.team.append(TeamResult("team_message", "Ben: bring 4 mics to stage left.", None, [
        {"type": "create_request", "description": "Bring 4 mics to stage left", "quantity": "4",
         "assignee_id": ben},
    ]))
    utter(env, "ana", "turn1", "uh so Ben can you bring like four mics to stage left")
    utter(env, "ana", "turn1", "uh so Ben can you bring like four mics to stage left")  # duplicate
    s = snap(env)
    assert len(s["messages"]) == 1 and len(s["requests"]) == 1
    msg, req = s["messages"][0], s["requests"][0]
    assert req["source_message_id"] == msg["id"] and msg["request_ids"] == [req["id"]]
    assert msg["original_transcript"].startswith("uh so Ben")

    payload = env.relay.llm.payloads[0]
    for key in ('"user_id"', '"name": "Ana"', '"role": "Stage manager"', '"content"',
                '"source_utterance_id"', '"mode": "team"', '"current_state"'):
        assert key in payload

    # Assignee says it's done while still open: accept then complete, both against the same snapshot.
    env.relay.llm.team.append(TeamResult("team_message", "Ben: mics are on stage left.", None, [
        {"type": "accept", "request_id": req["id"], "expected_version": 1},
        {"type": "complete", "request_id": req["id"], "expected_version": 1},
    ]))
    utter(env, "ben", "turn2", "done, mics are on stage left")
    assert snap(env)["requests"][0]["status"] == "completed"


def test_invalid_proposal_changes_nothing(env):
    req = create(env)
    # Cy tries to complete Ben's request by voice.
    env.relay.llm.team.append(TeamResult("team_message", "Cy says the mics are done.", None, [
        {"type": "complete", "request_id": req["id"], "expected_version": 1}]))
    utter(env, "cy", "t1", "the mics are done")
    s = snap(env)
    assert s["requests"][0]["status"] == "open" and s["requests"][0]["version"] == 1
    assert len(s["messages"]) == 1  # the message is still recorded


def test_ai_failure_leaves_state_unchanged(env):
    def boom(_):
        raise RuntimeError("model down")
    env.relay.llm.team.append(boom)
    utter(env, "ana", "t1", "Ben bring mics")
    s = snap(env)
    assert s["messages"] == [] and s["requests"] == []


def test_clarification_is_recorded_and_resolved(env):
    env.relay.llm.team.append(TeamResult("team_message", "Ana asks for those microphones.", None, [
        {"type": "ask_clarification", "question": "Which microphones do you mean?"}]))
    utter(env, "ana", "t1", "bring those microphones")
    s = snap(env)
    assert len(s["clarifications"]) == 1 and s["requests"] == []
    cid = s["clarifications"][0]["id"]

    env.relay.llm.team.append(TeamResult("clarification", "The wireless ones.", cid, []))
    utter(env, "ana", "t2", "the wireless ones")
    assert snap(env)["clarifications"] == []
