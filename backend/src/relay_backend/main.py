"""FastAPI app: REST routes and the per-participant websocket."""

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config
from .db import Database, dumps, new_id, now, purge_expired, row_dict
from .hub import Connection, Hub
from .pipeline import App, ephemeral, handle_utterance, share_answer
from .requests_engine import ALL_PERMISSIONS, ActionError, Actor, Context, VersionConflict, execute, public_participant, public_request

log = logging.getLogger("relay")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
AUDIO_TAGS = {1: "team", 2: "ask"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class CreateBody(BaseModel):
    room_name: str = Field(min_length=1, max_length=80)
    setting: str = Field(default="", max_length=500)
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(min_length=1, max_length=60)


class JoinBody(BaseModel):
    room_code: str = Field(min_length=4, max_length=12)
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(min_length=1, max_length=60)


class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        q, t = self.hits[key], time.monotonic()
        while q and q[0] < t - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(t)
        return True


def serialize_message(db: Database, row) -> dict:
    m = row_dict(row, ("request_ids",))
    if m["kind"] == "shared_answer" and m["shared_answer_id"]:
        ex = db.one("SELECT source_message_ids, created_at FROM bot_exchanges WHERE id = ?",
                    (m["shared_answer_id"],))
        if ex:
            m["sources"] = json.loads(ex["source_message_ids"])
            m["generated_at"] = ex["created_at"]
    return m


def create_app(db_path: str | None = None) -> FastAPI:
    hub = Hub()
    db = Database(db_path or config.DATABASE_PATH, publish=hub.publish)
    relay = App(db, hub)
    joins = RateLimiter(limit=20, window=60)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        purged = purge_expired(db.conn, config.RETENTION_DAYS)
        if purged:
            log.info("purged %d expired sessions", purged)
        db.conn.execute("UPDATE participants SET connection_state = 'offline'")
        missing = config.missing_keys()
        if missing:
            log.warning("Missing provider keys (set them in backend/.env): %s", ", ".join(missing))
        yield

    app = FastAPI(title="Relay", lifespan=lifespan)
    app.state.relay = relay
    app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"],
                       allow_headers=["*"])

    @app.exception_handler(ActionError)
    async def action_error(_: Request, e: ActionError):
        status = {"forbidden": 403, "not_found": 404, "version_conflict": 409}.get(e.code, 400)
        return JSONResponse({"code": e.code, "message": e.message}, status_code=status)

    async def auth(session_id: str, authorization: str = Header(default="")) -> Actor:
        token = authorization.removeprefix("Bearer ").strip()
        row = db.one("SELECT p.* FROM participants p JOIN sessions s ON s.id = p.session_id"
                     " WHERE p.authenticated_subject = ? AND p.session_id = ? AND s.closed_at IS NULL",
                     (hash_token(token), session_id)) if token else None
        if row is None:
            raise HTTPException(401, "Invalid or expired credentials for this room.")
        return Actor.from_row(row)

    def add_participant(tx, session_id: str, name: str, role: str, perms: list[str]) -> tuple[dict, str]:
        token = secrets.token_urlsafe(32)
        pid = new_id("par")
        tx.execute("INSERT INTO participants (id, session_id, display_name, role, permissions,"
                   " authenticated_subject, created_at) VALUES (?,?,?,?,?,?,?)",
                   (pid, session_id, name.strip(), role.strip(), dumps(perms), hash_token(token), now()))
        participant = public_participant(tx.one("SELECT * FROM participants WHERE id = ?", (pid,)))
        tx.emit(session_id, "participant.joined", {"participant": participant})
        return participant, token

    @app.get("/health")
    async def health():
        return {"ok": True, "providers": config.provider_status(), "model": config.GEMINI_MODEL}

    @app.post("/sessions")
    async def create_session(body: CreateBody, request: Request):
        if not joins.allow(request.client.host if request.client else "?"):
            raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
        with db.tx() as tx:
            for _ in range(20):
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
                if not tx.one("SELECT 1 FROM sessions WHERE room_code = ?", (code,)):
                    break
            else:
                raise HTTPException(503, "Could not allocate a room code.")
            sid = new_id("ses")
            tx.execute("INSERT INTO sessions (id, room_code, name, setting, created_at) VALUES (?,?,?,?,?)",
                       (sid, code, body.room_name.strip(), body.setting.strip(), now()))
            participant, token = add_participant(tx, sid, body.name, body.role, list(ALL_PERMISSIONS))
            tx.execute("UPDATE sessions SET created_by = ? WHERE id = ?", (participant["id"], sid))
        return {"session_id": sid, "room_code": code, "participant_id": participant["id"], "token": token}

    @app.post("/sessions/join")
    async def join_session(body: JoinBody, request: Request):
        if not joins.allow(request.client.host if request.client else "?"):
            raise HTTPException(429, "Too many join attempts. Wait a minute and try again.")
        code = body.room_code.strip().upper()
        session = db.one("SELECT * FROM sessions WHERE room_code = ?", (code,))
        if session is None or session["closed_at"]:
            raise HTTPException(404, "That room code is invalid or the room has closed.")
        with db.tx() as tx:
            participant, token = add_participant(tx, session["id"], body.name, body.role, [])
        return {"session_id": session["id"], "room_code": code, "participant_id": participant["id"],
                "token": token}

    @app.post("/sessions/{session_id}/audio-token")
    async def audio_token(session_id: str, actor: Actor = Depends(auth)):
        if not config.provider_status()["audio"]:
            raise HTTPException(503, "LiveKit is not configured on the server.")
        from livekit import api

        token = (api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
                 .with_identity(actor.id).with_name(actor.name)
                 .with_grants(api.VideoGrants(room_join=True, room=session_id, can_publish=True,
                                              can_subscribe=True, can_publish_data=False,
                                              can_publish_sources=["microphone"]))
                 .to_jwt())
        return {"url": config.LIVEKIT_URL, "token": token}

    @app.get("/sessions/{session_id}/snapshot")
    async def snapshot(session_id: str, actor: Actor = Depends(auth)):
        session = db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        online = hub.connected_ids(session_id)
        participants = [
            {**public_participant(r), "online": r["id"] in online}
            for r in db.all("SELECT * FROM participants WHERE session_id = ? ORDER BY created_at", (session_id,))
        ]
        return {
            "session": {k: session[k] for k in ("id", "room_code", "name", "setting", "created_by", "created_at")},
            "me": actor.id,
            "participants": participants,
            "messages": [serialize_message(db, r) for r in db.all(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp", (session_id,))],
            "requests": [public_request(r) for r in db.all(
                "SELECT * FROM requests WHERE session_id = ? ORDER BY created_at", (session_id,))],
            "request_events": [row_dict(r, ("before", "after")) for r in db.all(
                "SELECT * FROM request_events WHERE session_id = ? ORDER BY sequence", (session_id,))],
            "exchanges": [row_dict(r, ("source_message_ids",)) for r in db.all(
                "SELECT * FROM bot_exchanges WHERE session_id = ? AND asker_id = ? ORDER BY created_at",
                (session_id, actor.id))],
            "clarifications": [dict(r) for r in db.all(
                "SELECT * FROM clarifications WHERE session_id = ? AND resolved_at IS NULL AND expires_at > ?",
                (session_id, now()))],
            "turn": relay.room(session_id).floor.public(),
            "providers": config.provider_status(),
            "latest_sequence": session["event_seq"],
        }

    @app.get("/sessions/{session_id}/events")
    async def events(session_id: str, after: int = 0, actor: Actor = Depends(auth)):
        rows = db.all("SELECT * FROM events WHERE session_id = ? AND sequence > ?"
                      " AND visibility IN ('shared', ?) ORDER BY sequence LIMIT 1000",
                      (session_id, after, actor.id))
        return {"events": [row_dict(r, ("payload",)) for r in rows]}

    @app.post("/sessions/{session_id}/actions")
    async def actions(session_id: str, body: dict, actor: Actor = Depends(auth)):
        room = relay.room(session_id)
        async with room.lock:
            with db.tx() as tx:
                result = execute(tx, actor, body, Context(origin="manual",
                                                         source_message_id=None))
        for note in result.get("announcements", []):
            room.announce(note)
        return result

    @app.post("/sessions/{session_id}/answers/{exchange_id}/share")
    async def share(session_id: str, exchange_id: str, actor: Actor = Depends(auth)):
        async with relay.room(session_id).lock:
            message = share_answer(relay, session_id, actor.id, exchange_id)
        return {"message": message if isinstance(message, dict) else serialize_message(db, message)}

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, actor: Actor = Depends(auth)):
        if not actor.can("delete_session"):
            raise HTTPException(403, "You don't have permission to delete this room.")
        hub.send(session_id, ephemeral("session.closed", {}))
        await asyncio.sleep(0.1)
        db.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        relay.rooms.pop(session_id, None)
        return {"ok": True}

    @app.websocket("/sessions/{session_id}/stream")
    async def stream(ws: WebSocket, session_id: str, token: str = Query(default="")):
        try:
            actor = await auth(session_id, f"Bearer {token}")
        except HTTPException:
            await ws.close(code=4401)
            return
        await ws.accept()
        room = relay.room(session_id)
        conn = Connection(session_id, actor.id)
        hub.add(conn)
        room.connected(actor.id)
        db.conn.execute("UPDATE participants SET connection_state = 'online' WHERE id = ?", (actor.id,))
        hub.send(session_id, ephemeral("presence", {"participant_id": actor.id, "online": True}))
        conn.queue.put_nowait(ephemeral("turn.state", room.floor.public()))

        async def sender():
            while True:
                await ws.send_json(await conn.queue.get())

        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    data = msg["bytes"]
                    mode = AUDIO_TAGS.get(data[0])
                    if mode:
                        await room.audio(actor.id, mode, data[1:])
                    continue
                try:
                    control = json.loads(msg.get("text") or "{}")
                except json.JSONDecodeError:
                    continue
                kind = control.get("type")
                if kind == "turn.request":
                    room.request_turn(actor.id, "ask" if control.get("mode") == "ask" else "team")
                elif kind == "turn.release":
                    room.release_turn(actor.id, control.get("mode"))
                elif kind == "turn.cancel":
                    room.cancel_turn(actor.id)
                elif kind == "announcement.state":
                    room.announcement_feedback(str(control.get("id")), str(control.get("state")))
                elif kind == "utterance.text":
                    # Typed fallback: same pipeline, same identity, its own turn id.
                    text = str(control.get("text") or "")[:2000]
                    mode = "ask" if control.get("mode") == "ask" else "team"
                    room.spawn(handle_utterance(relay, session_id, actor.id, new_id("typed"), mode, text))
                elif kind == "ping":
                    conn.queue.put_nowait(ephemeral("pong", {}))
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            hub.remove(conn)
            if actor.id not in hub.connected_ids(session_id):
                db.conn.execute(
                    "UPDATE participants SET connection_state = 'offline', last_seen_sequence ="
                    " (SELECT event_seq FROM sessions WHERE id = ?) WHERE id = ?", (session_id, actor.id))
                hub.send(session_id, ephemeral("presence", {"participant_id": actor.id, "online": False}))
                room.disconnected(actor.id)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("relay_backend.main:app", host="127.0.0.1", port=8000)
