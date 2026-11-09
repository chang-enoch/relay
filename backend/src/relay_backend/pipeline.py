"""Room runtime: turns + audio routing, the utterance pipeline, and shared announcements.

finalised utterance -> dedupe (participant, turn) -> LLM -> engine -> commit -> optional speech.
Any transcription or reasoning failure leaves state unchanged and tells the speaker privately.
"""

import asyncio
import base64
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from . import config, llm, state, stt, tts
from .db import Database, Tx, dumps, new_id, now
from .hub import Hub
from .requests_engine import ActionError, Actor, Context, VersionConflict, execute
from .turns import Floor, Turn

log = logging.getLogger("relay.pipeline")


def ephemeral(type_: str, payload: dict) -> dict:
    return {"type": type_, "payload": payload, "ephemeral": True}


class Room:
    def __init__(self, app: "App", session_id: str):
        self.app = app
        self.session_id = session_id
        self.lock = asyncio.Lock()  # serialises interpretation + mutation for the room
        self.floor = Floor()
        self.floor_free = asyncio.Event()
        self.floor_free.set()
        self.leases: dict[str, asyncio.Task] = {}
        self.announcements: list[tuple[dict, int]] = []  # (notification, attempts)
        self.announcer: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()
        self.repeated: set[str] = set()

    # ------------------------------------------------------------ helpers
    def send(self, msg: dict, to: str = "shared") -> None:
        self.app.hub.send(self.session_id, msg, to)

    def notice(self, pid: str, text: str, level: str = "warning") -> None:
        self.send(ephemeral("notice", {"text": text, "level": level}), pid)

    def status(self, pid: str | None, component: str, ok: bool, detail: str = "") -> None:
        self.send(ephemeral("status", {"component": component, "state": "ok" if ok else "error",
                                       "detail": detail}), pid or "shared")

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def broadcast_floor(self) -> None:
        if self.floor.speaker is None:
            self.floor_free.set()
        else:
            self.floor_free.clear()
        self.send(ephemeral("turn.state", self.floor.public()))

    # ------------------------------------------------------------ turns
    def request_turn(self, pid: str, mode: str) -> None:
        turn = self.floor.request(pid, mode)
        if turn:
            self._start(turn)
        self.broadcast_floor()

    def cancel_turn(self, pid: str) -> None:
        self.floor.cancel(pid)
        self.broadcast_floor()

    def release_turn(self, pid: str, mode: str | None = None, process: bool = True) -> None:
        turn = self.floor.release(pid, mode)
        if turn:
            self.spawn(self._finish(turn, process))
            self.send(ephemeral("turn.ended", {"turn_id": turn.id, "mode": turn.mode}), pid)
        nxt = self.floor.grant_next()
        if nxt:
            self._start(nxt)
        self.broadcast_floor()

    def _start(self, turn: Turn) -> None:
        pid = turn.participant_id
        self.send(ephemeral("turn.granted", {"turn_id": turn.id, "mode": turn.mode}), pid)
        if turn.mode == "team":
            on_interim = lambda text: self.send(ephemeral(  # noqa: E731
                "transcript.interim", {"participant_id": pid, "turn_id": turn.id, "text": text}))
        else:
            on_interim = lambda text: self.send(ephemeral(  # noqa: E731
                "transcript.interim", {"participant_id": pid, "turn_id": turn.id, "text": text,
                                       "mode": "ask"}), pid)
        turn.stt = stt.LiveTranscription(on_interim)

        async def start():
            try:
                await turn.stt.start()
                self.status(pid, "transcription", True)
            except Exception as e:  # noqa: BLE001
                log.warning("transcription unavailable: %s", e)
                turn.stt.error = str(e)
                self.status(pid, "transcription", False, str(e))
        turn.start_task = self.spawn(start())

    async def audio(self, pid: str, mode: str, pcm: bytes) -> None:
        turn = self.floor.active(pid, mode)
        if turn and turn.stt:
            await turn.stt.send(pcm)

    async def _finish(self, turn: Turn, process: bool) -> None:
        pid = turn.participant_id
        if turn.start_task:
            await turn.start_task
        if not process:
            await turn.stt.abort()
            return
        try:
            text = await turn.stt.finish()
        except Exception as e:  # noqa: BLE001
            self.status(pid, "transcription", False, str(e))
            self.notice(pid, "Transcription failed, so your update was not applied.")
            return
        self.send(ephemeral("transcript.final", {"participant_id": pid, "turn_id": turn.id,
                                                 "text": text, "mode": turn.mode}),
                  "shared" if turn.mode == "team" else pid)
        await handle_utterance(self.app, self.session_id, pid, turn.id, turn.mode, text)

    # ------------------------------------------------------------ connection lease
    def connected(self, pid: str) -> None:
        task = self.leases.pop(pid, None)
        if task:
            task.cancel()

    def disconnected(self, pid: str) -> None:
        if self.floor.active(pid, "team") or self.floor.active(pid, "ask") or pid in self.floor.queue:
            async def expire():
                await asyncio.sleep(config.TURN_LEASE_SECONDS)
                self.leases.pop(pid, None)
                self.floor.cancel(pid)
                self.release_turn(pid, process=False)
            self.leases[pid] = self.spawn(expire())

    # ------------------------------------------------------------ announcements
    def announce(self, notification: dict, attempts: int = 0) -> None:
        if not config.SPEAK_ANNOUNCEMENTS:
            return  # text-only: the change or question is already visible in the feed
        self.announcements.append((notification, attempts))
        if self.announcer is None or self.announcer.done():
            self.announcer = self.spawn(self._announce_loop())

    async def _announce_loop(self) -> None:
        while self.announcements:
            await self.floor_free.wait()  # shared announcements wait for a clear channel
            note, attempts = self.announcements.pop(0)
            if self._superseded(note):
                self._set_delivery(note["id"], "superseded")
                continue
            audio = None
            try:
                audio = base64.b64encode(await tts.synthesize(note["message"])).decode()
                self.status(None, "speech", True)
            except Exception as e:  # noqa: BLE001
                log.warning("speech failed: %s", e)
                self.status(None, "speech", False, str(e))
            if not self.floor_free.is_set():  # someone grabbed the floor meanwhile
                self.announcements.insert(0, (note, attempts))
                continue
            self._set_delivery(note["id"], "playing" if audio else "failed")
            self.send(ephemeral("announcement", {
                "notification_id": note["id"], "text": note["message"], "audio": audio,
                "attempts": attempts}))
            await asyncio.sleep(0.2)

    def _superseded(self, note: dict) -> bool:
        if not note.get("request_id"):
            return False
        row = self.app.db.one("SELECT instruction_revision, status FROM requests WHERE id = ?",
                              (note["request_id"],))
        return row is None or row["status"] in ("completed", "cancelled") or (
            note.get("instruction_revision") is not None
            and row["instruction_revision"] != note["instruction_revision"])

    def _set_delivery(self, nid: str, delivery: str) -> None:
        with self.app.db.tx() as tx:
            tx.execute("UPDATE notifications SET delivery_state = ? WHERE id = ? AND session_id = ?",
                       (delivery, nid, self.session_id))
            tx.emit(self.session_id, "notification.state", {"id": nid, "delivery_state": delivery})

    def announcement_feedback(self, nid: str, delivery: str) -> None:
        """Client reports played / interrupted. Interrupted ones are re-evaluated once."""
        if delivery not in ("played", "interrupted"):
            return
        row = self.app.db.one("SELECT * FROM notifications WHERE id = ? AND session_id = ?",
                              (nid, self.session_id))
        if row is None or row["delivery_state"] != "playing":
            return
        self._set_delivery(nid, delivery)
        if delivery == "interrupted" and nid not in self.repeated:
            self.repeated.add(nid)
            self.announce(dict(row), attempts=1)


class App:
    def __init__(self, db: Database, hub: Hub):
        self.db = db
        self.hub = hub
        self.rooms: dict[str, Room] = {}
        self.llm = llm  # swappable in tests
        self.tts = tts

    def room(self, session_id: str) -> Room:
        if session_id not in self.rooms:
            self.rooms[session_id] = Room(self, session_id)
        return self.rooms[session_id]


# ---------------------------------------------------------------- pipeline

def _speaker(db: Database, pid: str) -> sqlite3.Row:
    return db.one("SELECT * FROM participants WHERE id = ?", (pid,))


async def handle_utterance(app: App, session_id: str, pid: str, turn_id: str, mode: str,
                           transcript: str) -> None:
    room = app.room(session_id)
    transcript = transcript.strip()
    if not transcript:
        room.notice(pid, "I didn't catch anything, so nothing was applied.")
        return
    utterance_id = new_id("utt")
    try:
        with app.db.tx() as tx:
            tx.execute("INSERT INTO utterances (id, session_id, participant_id, turn_id, mode,"
                       " transcript, created_at) VALUES (?,?,?,?,?,?,?)",
                       (utterance_id, session_id, pid, turn_id, mode, transcript, now()))
    except sqlite3.IntegrityError:
        log.info("duplicate utterance %s/%s ignored", pid, turn_id)
        return

    async with room.lock:
        speaker = _speaker(app.db, pid)
        if speaker is None:
            return
        if mode == "ask":
            await answer_question(app, room, speaker, utterance_id, transcript)
            return
        for attempt in (1, 2):
            st = state.build_state(app.db, session_id, pid, app.hub.connected_ids(session_id))
            payload = app.llm.llm_input(dict(speaker), transcript, utterance_id, "team", st)
            try:
                result = await app.llm.interpret_team(payload)
                room.status(pid, "ai", True)
            except Exception as e:  # noqa: BLE001
                log.exception("interpretation failed")
                room.status(pid, "ai", False, str(e))
                room.notice(pid, "Relay couldn't interpret that, so your update was not applied.")
                return
            if result.classification == "bot_question":
                await answer_question(app, room, speaker, utterance_id, transcript)
                return
            try:
                failures, spoken = _commit_team(app, speaker, turn_id, utterance_id, transcript, result)
                break
            except VersionConflict:
                if attempt == 2:
                    room.notice(pid, "The request changed while you were speaking. Nothing was applied; please repeat.")
                    return
    if failures:
        room.notice(pid, "Not applied: " + "; ".join(failures))
    for note in spoken:
        room.announce(note)


def _commit_team(app: App, speaker, turn_id: str, utterance_id: str, transcript: str,
                 result) -> tuple[list[str], list[dict]]:
    session_id = speaker["session_id"]
    actor = Actor.from_row(speaker)
    failures: list[str] = []
    spoken: list[dict] = []
    with app.db.tx() as tx:
        message = {
            "id": new_id("msg"), "utterance_id": utterance_id, "session_id": session_id,
            "participant_id": speaker["id"], "turn_id": turn_id, "original_transcript": transcript,
            "summary": result.summary or transcript, "request_ids": [], "timestamp": now(),
            "kind": "team", "shared_answer_id": None,
        }
        tx.execute(
            "INSERT INTO messages (id, utterance_id, session_id, participant_id, turn_id,"
            " original_transcript, summary, request_ids, timestamp, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (message["id"], utterance_id, session_id, speaker["id"], turn_id, transcript,
             message["summary"], "[]", message["timestamp"], "team"))
        tx.emit(session_id, "message.created", {"message": message})
        ctx = Context(origin="ai", source_message_id=message["id"], utterance_id=utterance_id)

        for i, proposal in enumerate(result.proposals):
            if proposal.get("type") == "ask_clarification":
                spoken.append(_clarify(tx, speaker, proposal))
                continue
            mark = len(tx.events)
            tx.execute("SAVEPOINT proposal")
            try:
                out = execute(tx, actor, {**proposal, "idempotency_key": f"{utterance_id}:{i}"}, ctx)
                tx.execute("RELEASE proposal")
                spoken.extend(out.get("announcements", []))
            except VersionConflict:
                raise
            except ActionError as e:
                tx.execute("ROLLBACK TO proposal")
                tx.execute("RELEASE proposal")
                del tx.events[mark:]
                failures.append(f"{proposal.get('type', '?').replace('_', ' ')}: {e.message}")

        if result.classification == "clarification":
            resolved = [r["id"] for r in tx.all(
                "UPDATE clarifications SET resolved_at = ? WHERE session_id = ? AND participant_id = ?"
                " AND resolved_at IS NULL AND (id = ? OR ? IS NULL) RETURNING id",
                (now(), session_id, speaker["id"], result.clarification_id, result.clarification_id))]
            if resolved:
                tx.emit(session_id, "clarification.resolved", {"ids": resolved})
    return failures, spoken


def _clarify(tx: Tx, speaker, proposal: dict) -> dict:
    ts = datetime.now(UTC)
    question = str(proposal.get("question", "")).strip() or "Could you clarify that?"
    related = proposal.get("related_request_id")
    if related and not tx.one("SELECT 1 FROM requests WHERE id = ? AND session_id = ?",
                              (related, speaker["session_id"])):
        related = None
    cid = new_id("clr")
    tx.execute(
        "INSERT INTO clarifications (id, session_id, participant_id, related_request_id, question,"
        " expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (cid, speaker["session_id"], speaker["id"], related, question,
         (ts + timedelta(seconds=config.CLARIFICATION_TTL_SECONDS)).isoformat().replace("+00:00", "Z"),
         now()))
    text = f"{speaker['display_name']}, {question[0].lower() + question[1:]}" if question else question
    note = {"id": new_id("ntf"), "session_id": speaker["session_id"], "request_id": related,
            "instruction_revision": None, "recipient_id": speaker["id"], "message": text,
            "delivery_state": "queued", "created_at": now()}
    tx.execute(
        "INSERT INTO notifications (id, session_id, request_id, instruction_revision, recipient_id,"
        " message, delivery_state, created_at) VALUES (?,?,?,?,?,?,?,?)", tuple(note.values()))
    tx.emit(speaker["session_id"], "clarification.created",
            {"id": cid, "participant_id": speaker["id"], "question": question,
             "related_request_id": related, "created_at": now(),
             "expires_at": (ts + timedelta(seconds=config.CLARIFICATION_TTL_SECONDS)).isoformat().replace("+00:00", "Z")})
    return note


async def answer_question(app: App, room: Room, speaker, utterance_id: str, question: str) -> None:
    """Read-only route. The answer goes only to the asker."""
    session_id, pid = speaker["session_id"], speaker["id"]
    st = state.build_state(app.db, session_id, pid, app.hub.connected_ids(session_id))
    payload = app.llm.llm_input(dict(speaker), question, utterance_id, "question", st)

    async def run_tool(name: str, args: dict):
        fn = state.READ_ONLY_TOOLS.get(name)
        if fn is None:
            return {"error": f"unknown or forbidden tool {name}"}
        args.pop("asker_id", None)
        return fn(app.db, session_id, asker_id=pid, **args)

    try:
        ans = await app.llm.answer_question(payload, run_tool)
        room.status(pid, "ai", True)
    except Exception as e:  # noqa: BLE001
        log.exception("question failed")
        room.status(pid, "ai", False, str(e))
        room.notice(pid, "Relay couldn't answer that right now.")
        return
    valid = {r["id"] for r in app.db.all(
        f"SELECT id FROM messages WHERE session_id = ? AND id IN ({','.join('?' * len(ans.source_message_ids))})",
        (session_id, *ans.source_message_ids))} if ans.source_message_ids else set()
    exchange = {
        "id": new_id("bot"), "session_id": session_id, "asker_id": pid,
        "question_transcript": question, "answer_text": ans.text,
        "source_message_ids": [m for m in ans.source_message_ids if m in valid],
        "state_sequence": st["latest_sequence"], "created_at": now(), "shared_message_id": None,
    }
    with app.db.tx() as tx:
        tx.execute(
            "INSERT INTO bot_exchanges (id, session_id, asker_id, question_transcript, answer_text,"
            " source_message_ids, state_sequence, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (exchange["id"], session_id, pid, question, ans.text,
             dumps(exchange["source_message_ids"]), exchange["state_sequence"], exchange["created_at"]))
        tx.emit(session_id, "ask.answer", {"exchange": exchange}, visibility=pid)
    try:
        audio = base64.b64encode(await app.tts.synthesize(ans.text)).decode()
        room.send(ephemeral("ask.audio", {"exchange_id": exchange["id"], "audio": audio}), pid)
        room.status(pid, "speech", True)
    except Exception as e:  # noqa: BLE001
        room.status(pid, "speech", False, str(e))


def share_answer(app: App, session_id: str, pid: str, exchange_id: str) -> dict:
    row = app.db.one("SELECT * FROM bot_exchanges WHERE id = ? AND session_id = ? AND asker_id = ?",
                     (exchange_id, session_id, pid))
    if row is None:
        raise ActionError("not_found", "No such answer.")
    if row["shared_message_id"]:
        return app.db.one("SELECT * FROM messages WHERE id = ?", (row["shared_message_id"],))
    message = {
        "id": new_id("msg"), "utterance_id": None, "session_id": session_id, "participant_id": pid,
        "turn_id": None, "original_transcript": None,
        "summary": row["answer_text"], "request_ids": [], "timestamp": now(),
        "kind": "shared_answer", "shared_answer_id": row["id"],
        "sources": json.loads(row["source_message_ids"]), "generated_at": row["created_at"],
    }
    with app.db.tx() as tx:
        tx.execute(
            "INSERT INTO messages (id, session_id, participant_id, original_transcript, summary,"
            " request_ids, timestamp, kind, shared_answer_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (message["id"], session_id, pid, None, message["summary"],
             "[]", message["timestamp"], "shared_answer", row["id"]))
        tx.execute("UPDATE bot_exchanges SET shared_message_id = ? WHERE id = ?", (message["id"], row["id"]))
        tx.emit(session_id, "message.created", {"message": message})
        tx.emit(session_id, "ask.shared", {"exchange_id": row["id"], "message_id": message["id"]},
                visibility=pid)
    return message
