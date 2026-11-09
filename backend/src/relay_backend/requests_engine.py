"""The single validation and execution path for request mutations.

Model proposals and manual /actions both go through execute(). It checks schema,
identity, permissions, recipient, lifecycle transition, expected_version and the
idempotency key, then writes the request, its audit event and a replay event
inside the caller's transaction.
"""

import json
from dataclasses import dataclass, field

from .db import Tx, dumps, new_id, now, row_dict

OUTSTANDING = ("open", "accepted", "in_progress")
TERMINAL = ("completed", "cancelled")
TRANSITIONS: dict[str, set[str]] = {
    "open": {"accepted", "cancelled"},
    "accepted": {"in_progress", "open", "completed", "cancelled"},
    "in_progress": {"open", "completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
MATERIAL_FIELDS = ("description", "quantity", "destination", "instructions")
DETAIL_FIELDS = (*MATERIAL_FIELDS, "deadline", "estimate", "estimate_due_at", "notes")
REQUEST_ACTIONS = (
    "create_request", "update_request", "accept", "decline",
    "report_progress", "complete", "cancel", "reassign",
)
PARTICIPANT_ACTIONS = ("update_profile", "set_permissions")
ALL_PERMISSIONS = ("manage_requests", "manage_room", "delete_session")


class ActionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class VersionConflict(ActionError):
    def __init__(self, message: str = "The request changed since this was proposed."):
        super().__init__("version_conflict", message)


@dataclass
class Actor:
    id: str
    session_id: str
    name: str
    role: str
    permissions: list[str]

    @classmethod
    def from_row(cls, row) -> "Actor":
        return cls(row["id"], row["session_id"], row["display_name"], row["role"],
                   json.loads(row["permissions"]))

    def can(self, permission: str) -> bool:
        return permission in self.permissions


@dataclass
class Context:
    origin: str = "manual"  # 'ai' or 'manual'
    source_message_id: str | None = None
    # Versions as they were when the batch started, so a batch such as accept + complete
    # proposed against one state snapshot validates against that snapshot.
    base_versions: dict[str, int] = field(default_factory=dict)


def public_request(row) -> dict:
    return row_dict(row, ("details",))


def execute(tx: Tx, actor: Actor, action: dict, ctx: Context) -> dict:
    kind = action.get("type")
    key = action.get("idempotency_key")
    if not isinstance(key, str) or not key:
        raise ActionError("invalid", "idempotency_key is required")
    prior = tx.one("SELECT result FROM idempotency WHERE session_id = ? AND key = ?",
                   (actor.session_id, key))
    if prior:
        return {**json.loads(prior["result"]), "duplicate": True}

    if kind in REQUEST_ACTIONS:
        result = _request_action(tx, actor, kind, action, ctx)
    elif kind in PARTICIPANT_ACTIONS:
        result = _participant_action(tx, actor, kind, action)
    else:
        raise ActionError("invalid", f"Unknown action type: {kind!r}")

    tx.execute("INSERT INTO idempotency (session_id, key, result) VALUES (?, ?, ?)",
               (actor.session_id, key, dumps(result)))
    return result


# ---------------------------------------------------------------- requests

def _participant(tx: Tx, session_id: str, pid: str | None) -> dict | None:
    if not pid:
        return None
    row = tx.one("SELECT * FROM participants WHERE id = ? AND session_id = ?", (pid, session_id))
    if row is None:
        raise ActionError("invalid_recipient", "That participant is not in this room.")
    return dict(row)


def _clean_details(raw: dict | None) -> dict:
    out = {}
    for k in DETAIL_FIELDS:
        v = (raw or {}).get(k)
        if v is None:
            continue
        if not isinstance(v, (str, int, float)):
            raise ActionError("invalid", f"{k} must be text or a number")
        v = str(v).strip()
        if v:
            out[k] = v
    return out


def _request_action(tx: Tx, actor: Actor, kind: str, a: dict, ctx: Context) -> dict:
    if kind == "create_request":
        return _create(tx, actor, a, ctx)

    req = tx.one("SELECT * FROM requests WHERE id = ? AND session_id = ?",
                 (a.get("request_id"), actor.session_id))
    if req is None:
        raise ActionError("not_found", "No such request in this room.")
    expected = a.get("expected_version")
    if not isinstance(expected, int):
        raise ActionError("invalid", "expected_version is required")
    base = ctx.base_versions.setdefault(req["id"], req["version"])
    if expected != base:
        raise VersionConflict()
    if req["status"] in TERMINAL:
        raise ActionError("invalid_transition", f"The request is already {req['status']}.")

    before = public_request(req)
    after = dict(before, details=dict(before["details"]))
    is_assignee = req["assignee_id"] == actor.id
    is_requester = req["requester_id"] == actor.id
    manager = actor.can("manage_requests")

    def need(ok: bool, what: str):
        if not ok:
            raise ActionError("forbidden", f"You are not allowed to {what} this request.")

    def to(status: str):
        if status == after["status"]:
            raise ActionError("no_change", f"The request is already {status}.")
        if status not in TRANSITIONS[after["status"]]:
            raise ActionError("invalid_transition",
                              f"Cannot move a request from {after['status']} to {status}.")
        after["status"] = status

    if kind == "update_request":
        need(is_requester or manager, "edit")
        changes = _clean_details(a.get("changes"))
        changed = {k: v for k, v in changes.items() if before["details"].get(k) != v}
        _sync_description(before["details"], changed)
        if not changed:
            raise ActionError("no_change", "Nothing to change.")
        after["details"].update(changed)
        if any(k in MATERIAL_FIELDS for k in changed):
            _material_change(after, to)

    elif kind == "reassign":
        need(is_requester or manager, "reassign")
        assignee = _participant(tx, actor.session_id, a.get("assignee_id"))
        target_role = (a.get("target_role") or "").strip() or None
        if assignee is None and target_role is None:
            raise ActionError("invalid", "Give a new assignee or target role.")
        new_assignee = assignee["id"] if assignee else None
        if new_assignee == req["assignee_id"] and target_role in (None, req["target_role"]):
            raise ActionError("no_change", "That is already the assignee.")
        after["assignee_id"] = new_assignee
        after["target_role"] = target_role or (assignee["role"] if assignee else None)
        _material_change(after, to)

    elif kind == "accept":
        if req["assignee_id"] is None:
            eligible = not req["target_role"] or req["target_role"].casefold() == actor.role.casefold()
            need(eligible, "accept")
            after["assignee_id"] = actor.id
        else:
            need(is_assignee, "accept")
        to("accepted")
        after["accepted_revision"] = after["instruction_revision"]
        after["details"].update(_clean_details({"estimate": a.get("estimate"),
                                                "estimate_due_at": a.get("estimate_due_at")}))

    elif kind == "decline":
        need(is_assignee, "decline")
        if after["status"] != "open":
            to("open")
        after["assignee_id"] = None
        after["accepted_revision"] = None

    elif kind == "report_progress":
        need(is_assignee, "report progress on")
        if req["status"] == "open":
            raise ActionError("invalid_transition", "Accept the request before reporting progress.")
        if a.get("started") and after["status"] == "accepted":
            to("in_progress")
        extra = _clean_details({"estimate": a.get("estimate"),
                                "estimate_due_at": a.get("estimate_due_at"),
                                "notes": a.get("note")})
        after["details"].update(extra)
        if after["status"] == before["status"] and not extra:
            raise ActionError("no_change", "No progress to record.")

    elif kind == "complete":
        need(is_assignee or manager, "complete")
        to("completed")
        if a.get("note"):
            after["details"]["notes"] = str(a["note"]).strip()

    elif kind == "cancel":
        need(is_requester or manager, "cancel")
        to("cancelled")

    ts = now()
    after["version"] = before["version"] + 1
    after["updated_at"] = ts
    if after["status"] in TERMINAL:
        after["closed_at"] = ts
    tx.execute(
        "UPDATE requests SET assignee_id=?, target_role=?, details=?, status=?, version=?,"
        " instruction_revision=?, accepted_revision=?, updated_at=?, closed_at=?"
        " WHERE id=? AND version=?",
        (after["assignee_id"], after["target_role"], dumps(after["details"]), after["status"],
         after["version"], after["instruction_revision"], after["accepted_revision"], ts,
         after.get("closed_at"), req["id"], before["version"]),
    )
    return _finish(tx, actor, ctx, kind, before, after)


def _sync_description(old: dict, changed: dict) -> None:
    """If a field changed but the description still states the old value, update the wording,
    so the headline of the request never contradicts its details."""
    if "description" in changed or not old.get("description"):
        return
    desc = old["description"]
    for name in ("quantity", "destination"):
        before, after = old.get(name), changed.get(name)
        if before and after and before.casefold() in desc.casefold():
            i = desc.casefold().index(before.casefold())
            desc = desc[:i] + after + desc[i + len(before):]
    if desc != old["description"]:
        changed["description"] = desc


def _material_change(after: dict, to) -> None:
    """New instructions: bump the revision, clear acceptance, send active work back to Open."""
    after["instruction_revision"] += 1
    after["accepted_revision"] = None
    if after["status"] != "open":
        to("open")


def _create(tx: Tx, actor: Actor, a: dict, ctx: Context) -> dict:
    details = _clean_details(a)
    if not details.get("description"):
        raise ActionError("invalid", "A request needs a description.")
    assignee = _participant(tx, actor.session_id, a.get("assignee_id"))
    target_role = (a.get("target_role") or "").strip() or (assignee["role"] if assignee else None)
    ts = now()
    req = {
        "id": new_id("req"),
        "session_id": actor.session_id,
        "source_message_id": ctx.source_message_id,
        "requester_id": actor.id,
        "assignee_id": assignee["id"] if assignee else None,
        "target_role": target_role,
        "details": details,
        "status": "open",
        "version": 1,
        "instruction_revision": 1,
        "accepted_revision": None,
        "created_at": ts,
        "updated_at": ts,
        "closed_at": None,
    }
    tx.execute(
        "INSERT INTO requests (id, session_id, source_message_id, requester_id, assignee_id,"
        " target_role, details, status, version, instruction_revision, accepted_revision,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (req["id"], req["session_id"], req["source_message_id"], req["requester_id"],
         req["assignee_id"], req["target_role"], dumps(details), "open", 1, 1, None, ts, ts),
    )
    return _finish(tx, actor, ctx, "create_request", None, req)


def _finish(tx: Tx, actor: Actor, ctx: Context, kind: str, before: dict | None,
            after: dict) -> dict:
    change = {"type": kind, "actor_id": actor.id, "origin": ctx.origin,
              "source_message_id": ctx.source_message_id}
    event = tx.emit(actor.session_id, "request.changed", {"request": after, "change": change})
    tx.execute(
        "INSERT INTO request_events (id, session_id, request_id, actor_id, source_message_id,"
        " origin, type, before, after, timestamp, sequence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("rev"), actor.session_id, after["id"], actor.id, ctx.source_message_id,
         ctx.origin, kind, dumps(before) if before else None, dumps(after),
         event["timestamp"], event["sequence"]),
    )
    if ctx.source_message_id:
        _link_message(tx, ctx.source_message_id, after["id"])

    return {"ok": True, "request": after}


def _link_message(tx: Tx, message_id: str, request_id: str) -> None:
    row = tx.one("SELECT request_ids FROM messages WHERE id = ?", (message_id,))
    if row is None:
        return
    ids = json.loads(row["request_ids"])
    if request_id not in ids:
        ids.append(request_id)
        tx.execute("UPDATE messages SET request_ids = ? WHERE id = ?", (dumps(ids), message_id))
        tx.emit(tx.one("SELECT session_id FROM messages WHERE id = ?", (message_id,))[0],
                "message.linked", {"message_id": message_id, "request_ids": ids})


# ---------------------------------------------------------------- participants

def _participant_action(tx: Tx, actor: Actor, kind: str, a: dict) -> dict:
    if kind == "update_profile":
        name = (a.get("name") or actor.name).strip()[:60]
        role = (a.get("role") or actor.role).strip()[:60]
        if not name or not role:
            raise ActionError("invalid", "Name and role cannot be empty.")
        tx.execute("UPDATE participants SET display_name = ?, role = ? WHERE id = ?",
                   (name, role, actor.id))
        target_id = actor.id
    else:  # set_permissions: only the room creator
        creator = tx.one("SELECT created_by FROM sessions WHERE id = ?", (actor.session_id,))
        if creator["created_by"] != actor.id:
            raise ActionError("forbidden", "Only the room creator can change permissions.")
        target = _participant(tx, actor.session_id, a.get("participant_id"))
        if target is None:
            raise ActionError("invalid", "participant_id is required")
        perms = a.get("permissions")
        if not isinstance(perms, list) or any(p not in ALL_PERMISSIONS for p in perms):
            raise ActionError("invalid", f"permissions must be a subset of {ALL_PERMISSIONS}")
        if target["id"] == actor.id:
            raise ActionError("invalid", "The creator keeps full permissions.")
        tx.execute("UPDATE participants SET permissions = ? WHERE id = ?",
                   (dumps(sorted(set(perms))), target["id"]))
        target_id = target["id"]
    row = tx.one("SELECT * FROM participants WHERE id = ?", (target_id,))
    participant = public_participant(row)
    tx.emit(actor.session_id, "participant.updated", {"participant": participant})
    return {"ok": True, "participant": participant}


def public_participant(row) -> dict:
    d = row_dict(row, ("permissions",))
    d.pop("authenticated_subject", None)
    return d
