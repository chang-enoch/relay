"""Server-managed speaking turns. One shared team speaker at a time with a FIFO
next-turn queue; private Ask Relay captures are per participant and never take the floor."""

from dataclasses import dataclass, field
from typing import Any

from .db import new_id


@dataclass
class Turn:
    id: str
    participant_id: str
    mode: str  # 'team' or 'ask'
    stt: Any = None
    start_task: Any = None


@dataclass
class Floor:
    speaker: Turn | None = None
    queue: list[str] = field(default_factory=list)
    private: dict[str, Turn] = field(default_factory=dict)

    def request(self, pid: str, mode: str) -> Turn | None:
        """Grant a turn now, or queue for the shared floor. Returns the granted turn."""
        if mode == "ask":
            if pid in self.private:
                return None
            turn = self.private[pid] = Turn(new_id("turn"), pid, "ask")
            return turn
        if self.speaker and self.speaker.participant_id == pid:
            return None
        if self.speaker is None and (not self.queue or self.queue[0] == pid):
            if self.queue and self.queue[0] == pid:
                self.queue.pop(0)
            self.speaker = Turn(new_id("turn"), pid, "team")
            return self.speaker
        if pid not in self.queue:
            self.queue.append(pid)
        return None

    def cancel(self, pid: str) -> None:
        if pid in self.queue:
            self.queue.remove(pid)

    def release(self, pid: str, mode: str | None = None) -> Turn | None:
        """End the participant's turn (team or ask). Returns the ended turn."""
        if mode in (None, "team") and self.speaker and self.speaker.participant_id == pid:
            ended, self.speaker = self.speaker, None
            return ended
        if mode in (None, "ask") and pid in self.private:
            return self.private.pop(pid)
        return None

    def grant_next(self) -> Turn | None:
        if self.speaker is None and self.queue:
            pid = self.queue.pop(0)
            self.speaker = Turn(new_id("turn"), pid, "team")
            return self.speaker
        return None

    def active(self, pid: str, mode: str) -> Turn | None:
        if mode == "team":
            return self.speaker if self.speaker and self.speaker.participant_id == pid else None
        return self.private.get(pid)

    def public(self) -> dict:
        return {
            "speaker": {"participant_id": self.speaker.participant_id, "turn_id": self.speaker.id}
            if self.speaker else None,
            "queue": list(self.queue),
        }
