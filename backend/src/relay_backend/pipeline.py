"""Room runtime (turns + audio routing) and the utterance pipeline.

finalised utterance -> dedupe (participant, turn) -> LLM -> engine -> commit.
Questions go to a read-only route whose answer is spoken back to the asker only.
Any transcription or reasoning failure leaves state unchanged and tells the speaker privately.
"""

import asyncio
import base64
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from . import config, llm, state, stt, tts
from .db import Database, Tx, dumps, iso, new_id, now
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
        self.lock = asyncio.Lock()  # serialises team interpretation + commit for the room
        self.floor = Floor()
        self.leases: dict[str, asyncio.Task] = {}
        self.tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ helpers
    def send(self, msg: dict, to: str = "shared") -> None:
        self.app.hub.send(self.session_id, msg, to)

    def notice(self, pid: str, text: str) -> None:
        self.send(ephemeral("notice", {"text": text}), pid)

    def status(self, pid: str | None, component: str, ok: bool, detail: str = "") -> None:
        self.send(ephemeral("status", {"component": component, "state": "ok" if ok else "error",
                                       "detail": detail}), pid or "shared")

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def broadcast_floor(self) -> None:
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

    # ------------------------------------------------------------ shutdown
    async def close(self) -> None:
        """Room deleted: stop open transcriptions and any work still in flight."""
        for turn in (self.floor.speaker, *self.floor.private.values()):
            if turn and turn.stt:
                await turn.stt.abort()
        for task in (*self.tasks, *self.leases.values()):
            task.cancel()


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

def _speaker(db: Database, pid: str) -> sqlite3.Row | None:
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
    speaker = _speaker(app.db, pid)
    if speaker is None:
        return
    if mode == "team" and not await _team_turn(app, room, speaker, turn_id, utterance_id, transcript):
        return
    # A private Ask Relay turn, or a team utterance that was really a question for Relay.
    # Questions are read-only, so they run outside the room lock and never hold up the team.
    await answer_question(app, room, speaker, utterance_id, transcript)


async def _team_turn(app: App, room: Room, speaker: sqlite3.Row, turn_id: str, utterance_id: str,
                     transcript: str) -> bool:
    """Interpret and commit a team utterance. Returns True if it was a question for Relay instead."""
    pid, session_id = speaker["id"], speaker["session_id"]
    async with room.lock:
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
                return False
            if result.classification == "bot_question":
                return True
            try:
                failures = _commit_team(app, speaker, turn_id, utterance_id, transcript, result)
                break
            except VersionConflict:  # a manual action landed while the model was thinking
                if attempt == 2:
                    room.notice(pid, "The request changed while you were speaking. Nothing was applied; please repeat.")
                    return False
    if failures:
        room.notice(pid, "Not applied: " + "; ".join(failures))
    return False


def _commit_team(app: App, speaker: sqlite3.Row, turn_id: str, utterance_id: str, transcript: str,
                 result: llm.TeamResult) -> list[str]:
    """One transaction: the message plus every valid proposal. Returns why any proposal failed."""
    session_id = speaker["session_id"]
    actor = Actor.from_row(speaker)
    failures: list[str] = []
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
        ctx = Context(origin="ai", source_message_id=message["id"])

        for i, proposal in enumerate(result.proposals):
            if proposal.get("type") == "ask_clarification":
                _clarify(tx, speaker, proposal)
                continue
            mark = len(tx.events)
            tx.execute("SAVEPOINT proposal")
            try:
                execute(tx, actor, {**proposal, "idempotency_key": f"{utterance_id}:{i}"}, ctx)
                tx.execute("RELEASE proposal")
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
    return failures


def _clarify(tx: Tx, speaker: sqlite3.Row, proposal: dict) -> None:
    session_id = speaker["session_id"]
    question = str(proposal.get("question", "")).strip() or "Could you clarify that?"
    related = proposal.get("related_request_id")
    if related and not tx.one("SELECT 1 FROM requests WHERE id = ? AND session_id = ?",
                              (related, session_id)):
        related = None
    created = datetime.now(UTC)
    clarification = {
        "id": new_id("clr"), "participant_id": speaker["id"], "question": question,
        "related_request_id": related, "created_at": iso(created),
        "expires_at": iso(created + timedelta(seconds=config.CLARIFICATION_TTL_SECONDS)),
    }
    tx.execute(
        "INSERT INTO clarifications (id, session_id, participant_id, related_request_id, question,"
        " expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (clarification["id"], session_id, speaker["id"], related, question,
         clarification["expires_at"], clarification["created_at"]))
    tx.emit(session_id, "clarification.created", clarification)


async def answer_question(app: App, room: Room, speaker: sqlite3.Row, utterance_id: str,
                          question: str) -> None:
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
        return state.serialize_message(
            app.db, app.db.one("SELECT * FROM messages WHERE id = ?", (row["shared_message_id"],)))
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
