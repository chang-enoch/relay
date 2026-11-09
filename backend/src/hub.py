"""Fan-out of committed events to connected websockets, respecting visibility."""

import asyncio
from collections import defaultdict


class Connection:
    def __init__(self, session_id: str, participant_id: str):
        self.session_id = session_id
        self.participant_id = participant_id
        # None is a sentinel telling the socket's sender to close (the room was deleted).
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()


class Hub:
    def __init__(self) -> None:
        self.rooms: dict[str, set[Connection]] = defaultdict(set)

    def add(self, conn: Connection) -> None:
        self.rooms[conn.session_id].add(conn)

    def remove(self, conn: Connection) -> None:
        conns = self.rooms.get(conn.session_id)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                del self.rooms[conn.session_id]

    def close_session(self, session_id: str) -> None:
        """Ask every socket in the room to close."""
        for conn in self.rooms.pop(session_id, ()):
            conn.queue.put_nowait(None)

    def connected_ids(self, session_id: str) -> set[str]:
        return {c.participant_id for c in self.rooms.get(session_id, ())}

    def publish(self, events: list[dict]) -> None:
        for event in events:
            self.send(event["session_id"], event, event.get("visibility", "shared"))

    def send(self, session_id: str, message: dict, visibility: str = "shared") -> None:
        """Send to everyone ('shared') or to one participant id. Not persisted."""
        for conn in list(self.rooms.get(session_id, ())):
            if visibility == "shared" or conn.participant_id == visibility:
                conn.queue.put_nowait(message)
