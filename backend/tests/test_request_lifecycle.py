"""Request lifecycle: transitions, material changes, reassignment, versions and idempotency."""

from helpers import act, create


def test_lifecycle_and_highlight_rules(env):
    req = create(env)
    assert req["status"] == "open" and req["version"] == 1

    r = act(env, "ben", type="complete", request_id=req["id"], expected_version=1)
    assert r.status_code == 400 and r.json()["code"] == "invalid_transition"

    req = act(env, "ben", type="accept", request_id=req["id"], expected_version=1,
              estimate="10 min").json()["request"]
    assert req["status"] == "accepted" and req["accepted_revision"] == 1
    assert req["details"]["estimate"] == "10 min"

    req = act(env, "ben", type="report_progress", request_id=req["id"], expected_version=2,
              started=True).json()["request"]
    assert req["status"] == "in_progress"
    req = act(env, "ben", type="complete", request_id=req["id"], expected_version=3).json()["request"]
    assert req["status"] == "completed" and req["closed_at"]

    r = act(env, "ana", type="cancel", request_id=req["id"], expected_version=4)
    assert r.json()["code"] == "invalid_transition"


def test_material_change_requires_reacceptance(env):
    req = create(env, quantity="4", destination="stage left")
    req = act(env, "ben", type="accept", request_id=req["id"], expected_version=1).json()["request"]

    req = act(env, "ana", type="update_request", request_id=req["id"], expected_version=2,
              changes={"quantity": "6"}).json()["request"]
    assert req["status"] == "open" and req["instruction_revision"] == 2 and req["accepted_revision"] is None
    # The headline is kept in step with the changed field ("Bring 4 mics" -> "Bring 6 mics").
    assert req["details"]["description"] == "Bring 6 mics to stage left"

    req = act(env, "ana", type="update_request", request_id=req["id"], expected_version=3,
              changes={"destination": "stage right"}).json()["request"]
    assert req["details"]["description"] == "Bring 6 mics to stage right"
    assert req["instruction_revision"] == 3

    # A note is not material: no new revision.
    req = act(env, "ana", type="update_request", request_id=req["id"], expected_version=4,
              changes={"notes": "use the blue case"}).json()["request"]
    assert req["instruction_revision"] == 3

    req = act(env, "ben", type="accept", request_id=req["id"], expected_version=5).json()["request"]
    assert req["accepted_revision"] == 3


def test_reassign_clears_acceptance(env):
    req = create(env)
    req = act(env, "ben", type="accept", request_id=req["id"], expected_version=1).json()["request"]
    req = act(env, "ana", type="reassign", request_id=req["id"], expected_version=2,
              assignee_id=env.pid("cy")).json()["request"]
    assert req["assignee_id"] == env.pid("cy")
    assert req["status"] == "open" and req["accepted_revision"] is None


def test_version_conflict_and_idempotency(env):
    req = create(env)
    act(env, "ana", type="update_request", request_id=req["id"], expected_version=1, changes={"notes": "n1"})
    r = act(env, "ana", type="update_request", request_id=req["id"], expected_version=1,
            changes={"notes": "stale"})
    assert r.status_code == 409

    body = dict(type="update_request", request_id=req["id"], expected_version=2,
                changes={"notes": "n2"}, idempotency_key="same")
    first = act(env, "ana", **body).json()
    again = act(env, "ana", **body).json()
    assert again["duplicate"] and again["request"]["version"] == first["request"]["version"] == 3
