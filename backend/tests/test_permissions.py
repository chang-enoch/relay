"""Who may do what, and isolation between rooms."""

from helpers import act, create


def test_permissions_enforced(env):
    req = create(env, who="ben", assignee="cy")
    # Not the assignee.
    assert act(env, "ana", type="accept", request_id=req["id"], expected_version=1).status_code == 403
    # Not the requester and no manage permission.
    assert act(env, "cy", type="cancel", request_id=req["id"], expected_version=1).status_code == 403
    # The room creator holds manage_requests.
    assert act(env, "ana", type="cancel", request_id=req["id"], expected_version=1).status_code == 200
    # Only the creator manages permissions.
    r = act(env, "ben", type="set_permissions", participant_id=env.pid("cy"), permissions=["manage_requests"])
    assert r.status_code == 403


def test_cross_session_isolation(env):
    req = create(env)
    other = env.client.post("/sessions", json={"room_name": "Other", "name": "Zed", "role": "x"}).json()
    hz = {"Authorization": f"Bearer {other['token']}"}
    r = env.client.post(f"/sessions/{other['session_id']}/actions", headers=hz,
                        json={"type": "cancel", "request_id": req["id"], "expected_version": 1,
                              "idempotency_key": "z"})
    assert r.status_code == 404
    # A token from one room cannot read another.
    assert env.client.get(f"/sessions/{env.sid}/snapshot", headers=hz).status_code == 401


def test_unassigned_role_request_can_be_claimed_by_matching_role(env):
    req = create(env, assignee=None, target_role="lighting")
    assert act(env, "ben", type="accept", request_id=req["id"], expected_version=1).status_code == 403
    req = act(env, "cy", type="accept", request_id=req["id"], expected_version=1).json()["request"]
    assert req["assignee_id"] == env.pid("cy")


def test_decline_allowed_wherever_accept_is(env):
    # Assigned: only the assignee may decline, and declining cancels the request.
    req = create(env, who="ana", assignee="ben")
    assert act(env, "cy", type="decline", request_id=req["id"], expected_version=1).status_code == 403
    req = act(env, "ben", type="decline", request_id=req["id"], expected_version=1).json()["request"]
    assert req["status"] == "cancelled" and req["closed_at"]
    # It is closed: nothing more can happen to it.
    assert act(env, "ben", type="accept", request_id=req["id"], expected_version=2).status_code != 200

    # Unassigned, for a role: a matching teammate may decline it, which also cancels it.
    req = create(env, assignee=None, target_role="lighting")
    assert act(env, "ben", type="decline", request_id=req["id"], expected_version=1).status_code == 403
    req = act(env, "cy", type="decline", request_id=req["id"], expected_version=1).json()["request"]
    assert req["status"] == "cancelled"


def test_accepted_request_can_still_be_declined(env):
    req = create(env, assignee="ben")
    req = act(env, "ben", type="accept", request_id=req["id"], expected_version=1).json()["request"]
    req = act(env, "ben", type="decline", request_id=req["id"], expected_version=2).json()["request"]
    assert req["status"] == "cancelled"
