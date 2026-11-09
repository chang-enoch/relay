"""Room access: joining, the websocket's auth, and deleting a room."""

import pytest
from starlette.websockets import WebSocketDisconnect

from helpers import act


def test_join_with_an_unknown_code_is_rejected(env):
    r = env.client.post("/sessions/join", json={"room_code": "NOPE99", "name": "Dee", "role": "Runner"})
    assert r.status_code == 404


def test_websocket_rejects_bad_tokens_with_4401(env):
    # The browser stops reconnecting on 4401, so it must arrive as a real close code.
    with env.client.websocket_connect(f"/sessions/{env.sid}/stream?token=wrong") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_deleting_a_room_closes_sockets_and_revokes_access(env):
    with env.client.websocket_connect(f"/sessions/{env.sid}/stream?token={env.people['ben'][1]}") as ws:
        ws.receive_json()  # connected (presence / turn state)
        # Only the creator may delete.
        assert env.client.delete(f"/sessions/{env.sid}", headers=env.headers("ben")).status_code == 403
        assert env.client.delete(f"/sessions/{env.sid}", headers=env.headers("ana")).status_code == 200
        types = []
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                types.append(ws.receive_json()["type"])
    assert "session.closed" in types and closed.value.code == 4401
    assert env.client.get(f"/sessions/{env.sid}/snapshot", headers=env.headers("ana")).status_code == 401
    assert act(env, "ana", type="create_request", description="x").status_code == 401
