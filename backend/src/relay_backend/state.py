"""Builds the compact current_state sent on every LLM call, plus the read-only
query tools used by Ask Relay. Private bot exchanges are never included."""

import json

from .db import Database, now, row_dict
from .requests_engine import OUTSTANDING, public_participant, public_request

RECENT_MESSAGES = 15


def serialize_message(db: Database, row) -> dict:
    """A message row as the client sees it; shared answers carry their sources."""
    m = row_dict(row, ("request_ids",))
    if m["kind"] == "shared_answer" and m["shared_answer_id"]:
        ex = db.one("SELECT source_message_ids, created_at FROM bot_exchanges WHERE id = ?",
                    (m["shared_answer_id"],))
        if ex:
            m["sources"] = json.loads(ex["source_message_ids"])
            m["generated_at"] = ex["created_at"]
    return m


def _outstanding(db: Database, session_id: str) -> list:
    return db.all(
        f"SELECT * FROM requests WHERE session_id = ? AND status IN ({','.join('?' * len(OUTSTANDING))})"
        " ORDER BY created_at", (session_id, *OUTSTANDING))


def _names(db: Database, session_id: str) -> dict[str, dict]:
    rows = db.all("SELECT id, display_name, role FROM participants WHERE session_id = ?", (session_id,))
    return {r["id"]: {"name": r["display_name"], "role": r["role"]} for r in rows}


def compact_request(r: dict, names: dict) -> dict:
    who = lambda pid: names.get(pid, {}).get("name") if pid else None  # noqa: E731
    return {
        "id": r["id"],
        "details": r["details"],
        "status": r["status"],
        "requester": who(r["requester_id"]),
        "requester_id": r["requester_id"],
        "assignee": who(r["assignee_id"]),
        "assignee_id": r["assignee_id"],
        "target_role": r["target_role"],
        "version": r["version"],
        "instruction_revision": r["instruction_revision"],
        "accepted_revision": r["accepted_revision"],
        "source_message_id": r["source_message_id"],
        "updated_at": r["updated_at"],
    }


def compact_message(m: dict, names: dict) -> dict:
    p = names.get(m["participant_id"] or "", {})
    return {
        "id": m["id"],
        "speaker": p.get("name"),
        "role": p.get("role"),
        "kind": m["kind"],
        "summary": m["summary"],
        "time": m["timestamp"],
        "request_ids": m["request_ids"],
    }


def build_state(db: Database, session_id: str, speaker_id: str, connected: set[str]) -> dict:
    session = db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    names = _names(db, session_id)
    participants = []
    for row in db.all("SELECT * FROM participants WHERE session_id = ?", (session_id,)):
        p = public_participant(row)
        participants.append({
            "id": p["id"], "name": p["display_name"], "role": p["role"],
            "permissions": p["permissions"], "online": p["id"] in connected,
            "is_creator": p["id"] == session["created_by"],
        })
    outstanding = [compact_request(public_request(r), names) for r in _outstanding(db, session_id)]
    msgs = [row_dict(r, ("request_ids",)) for r in db.all(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
        (session_id, RECENT_MESSAGES))]
    msgs.reverse()
    total = db.one("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))[0]
    older = total - len(msgs)
    closed = db.all(
        "SELECT details, status FROM requests WHERE session_id = ? AND status IN ('completed','cancelled')"
        " ORDER BY updated_at DESC LIMIT 10", (session_id,))
    closed_text = "; ".join(f"{json.loads(r['details']).get('description')} ({r['status']})" for r in closed)
    clarifications = [dict(r) for r in db.all(
        "SELECT id, related_request_id, question, expires_at FROM clarifications"
        " WHERE session_id = ? AND participant_id = ? AND resolved_at IS NULL AND expires_at > ?",
        (session_id, speaker_id, now()))]
    return {
        "room": {"name": session["name"], "setting": session["setting"], "room_time_utc": now()},
        "participants": participants,
        "outstanding_requests": outstanding,
        "recent_messages": [compact_message(m, names) for m in msgs],
        "pending_clarifications_for_speaker": clarifications,
        "older_history_summary": (
            f"{older} older messages not shown; use search to retrieve them. "
            f"Recently closed requests: {closed_text or 'none'}."
        ),
        "latest_sequence": session["event_seq"],
    }


# ---------------------------------------------------------------- read-only tools

def get_status(db: Database, session_id: str, **_) -> dict:
    names = _names(db, session_id)
    return {"outstanding": [compact_request(public_request(r), names) for r in _outstanding(db, session_id)],
            "room_time_utc": now()}


def get_request(db: Database, session_id: str, request_id: str = "", **_) -> dict:
    row = db.one("SELECT * FROM requests WHERE id = ? AND session_id = ?", (request_id, session_id))
    if row is None:
        return {"error": "not found"}
    names = _names(db, session_id)
    history = [
        {"type": e["type"], "actor": names.get(e["actor_id"] or "", {}).get("name"),
         "origin": e["origin"], "source_message_id": e["source_message_id"], "time": e["timestamp"]}
        for e in db.all("SELECT * FROM request_events WHERE request_id = ? ORDER BY sequence", (request_id,))
    ]
    src = db.one("SELECT id, original_transcript, summary FROM messages WHERE id = ?",
                 (row["source_message_id"],))
    return {"request": compact_request(public_request(row), names), "history": history,
            "source_message": dict(src) if src else None}


def search_messages(db: Database, session_id: str, query: str = "", **_) -> dict:
    names = _names(db, session_id)
    terms = [t for t in query.lower().split() if len(t) > 2][:6] or [query.lower()]
    clause = " OR ".join("(lower(summary) LIKE ? OR lower(coalesce(original_transcript,'')) LIKE ?)"
                         for _ in terms)
    params = [x for t in terms for x in (f"%{t}%", f"%{t}%")]
    rows = db.all(f"SELECT * FROM messages WHERE session_id = ? AND ({clause})"
                  " ORDER BY timestamp DESC LIMIT 10", (session_id, *params))
    return {"messages": [
        {**compact_message(row_dict(r, ("request_ids",)), names), "transcript": r["original_transcript"]}
        for r in rows]}


def get_updates_since(db: Database, session_id: str, asker_id: str = "",
                      since_time: str | None = None, **_) -> dict:
    """Shared changes since the asker's last-seen marker, or since an explicit time."""
    marker = db.one("SELECT last_seen_sequence FROM participants WHERE id = ?", (asker_id,))
    seq = marker["last_seen_sequence"] if marker else None
    if since_time:
        rows = db.all("SELECT * FROM events WHERE session_id = ? AND visibility = 'shared' AND timestamp > ?"
                      " ORDER BY sequence LIMIT 60", (session_id, since_time))
    elif seq is not None:
        rows = db.all("SELECT * FROM events WHERE session_id = ? AND visibility = 'shared' AND sequence > ?"
                      " ORDER BY sequence LIMIT 60", (session_id, seq))
    else:
        return {"error": "No last-seen marker for this participant. Ask them for a time range."}
    names = _names(db, session_id)
    changes = []
    for e in rows:
        payload = json.loads(e["payload"])
        if e["type"] == "message.created":
            changes.append({"type": "message", "time": e["timestamp"],
                            **compact_message(payload["message"], names)})
        elif e["type"] == "request.changed":
            changes.append({"type": payload["change"]["type"], "time": e["timestamp"],
                            "request": compact_request(payload["request"], names),
                            "by": names.get(payload["change"]["actor_id"], {}).get("name")})
    return {"since_sequence": seq, "since_time": since_time, "changes": changes}


READ_ONLY_TOOLS = {
    "get_status": get_status,
    "get_request": get_request,
    "search_messages": search_messages,
    "get_updates_since": get_updates_since,
}
