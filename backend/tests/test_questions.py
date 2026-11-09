"""Ask Relay: read-only, private to the asker until shared."""

from helpers import create, snap, utter
from relay_backend.llm import Answer, TeamResult


def test_questions_are_read_only_and_private(env):
    req = create(env)
    before = snap(env)

    async def ans(payload, run_tool):
        status = await run_tool("get_status", {})
        assert status["outstanding"][0]["id"] == req["id"]
        forbidden = await run_tool("create_request", {"description": "sneaky"})
        assert "error" in forbidden
        return Answer("Ben is bringing 4 mics; not accepted yet.", ["msg_bogus"])
    env.relay.llm.answers.append(ans)
    utter(env, "ana", "q1", "what is still outstanding?", mode="ask")

    after = snap(env)
    assert after["requests"] == before["requests"] and after["messages"] == before["messages"]
    assert len(after["exchanges"]) == 1
    ex = after["exchanges"][0]
    assert ex["source_message_ids"] == []  # unknown ids are dropped

    # Ben cannot see Ana's exchange in snapshot or replay, nor share it.
    assert snap(env, "ben")["exchanges"] == []
    events = env.client.get(f"/sessions/{env.sid}/events?after=0", headers=env.headers("ben")).json()["events"]
    assert not any(e["type"].startswith("ask.") for e in events)
    share = f"/sessions/{env.sid}/answers/{ex['id']}/share"
    assert env.client.post(share, headers=env.headers("ben")).status_code == 404

    # Ana shares: a labelled copy appears, without the private question. Sharing twice
    # returns the same message.
    first = env.client.post(share, headers=env.headers("ana")).json()["message"]
    assert first["kind"] == "shared_answer" and first["original_transcript"] is None
    again = env.client.post(share, headers=env.headers("ana")).json()["message"]
    assert again["id"] == first["id"] and again["generated_at"]
    msgs = snap(env, "ben")["messages"]
    assert msgs[-1]["kind"] == "shared_answer" and msgs[-1]["generated_at"]


def test_bot_question_in_shared_channel_routes_to_answer(env):
    env.relay.llm.team.append(TeamResult("bot_question", "", None, [
        {"type": "create_request", "description": "should be ignored"}]))

    async def ans(payload, run_tool):
        assert '"mode": "question"' in payload
        return Answer("Nothing outstanding.", [])
    env.relay.llm.answers.append(ans)
    utter(env, "ben", "t1", "Relay, what's outstanding?")
    s = snap(env, "ben")
    assert s["requests"] == [] and s["messages"] == [] and len(s["exchanges"]) == 1


def test_private_exchanges_excluded_from_state(env):
    async def ans(payload, run_tool):
        return Answer("SECRET-ANSWER", [])
    env.relay.llm.answers.append(ans)
    utter(env, "ana", "q1", "SECRET-QUESTION", mode="ask")

    env.relay.llm.team.append(TeamResult("team_message", "hi", None, []))
    utter(env, "ben", "t1", "hello")
    assert "SECRET" not in env.relay.llm.payloads[-1]
