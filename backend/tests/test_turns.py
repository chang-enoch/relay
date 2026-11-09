"""Speaking turns: one team speaker at a time, a FIFO queue, and private ask turns."""

from relay_backend.turns import Floor


def test_first_request_gets_the_floor_and_others_queue():
    floor = Floor()
    ana = floor.request("ana", "team")
    assert ana is not None and floor.speaker is ana
    assert floor.request("ben", "team") is None
    assert floor.request("cy", "team") is None
    assert floor.public()["queue"] == ["ben", "cy"]
    # Asking again while holding the floor or already queued changes nothing.
    assert floor.request("ana", "team") is None and floor.request("ben", "team") is None
    assert floor.queue == ["ben", "cy"]


def test_release_hands_the_floor_to_the_next_in_line():
    floor = Floor()
    floor.request("ana", "team")
    floor.request("ben", "team")
    ended = floor.release("ana")
    assert ended is not None and ended.participant_id == "ana" and floor.speaker is None
    nxt = floor.grant_next()
    assert nxt is not None and nxt.participant_id == "ben" and floor.queue == []
    assert floor.grant_next() is None  # someone already holds it


def test_cancel_leaves_the_queue():
    floor = Floor()
    floor.request("ana", "team")
    floor.request("ben", "team")
    floor.cancel("ben")
    floor.release("ana")
    assert floor.grant_next() is None and floor.speaker is None


def test_only_the_speaker_can_release_the_floor():
    floor = Floor()
    floor.request("ana", "team")
    assert floor.release("ben") is None
    assert floor.speaker is not None and floor.speaker.participant_id == "ana"


def test_ask_turns_are_private_and_never_take_the_floor():
    floor = Floor()
    floor.request("ana", "team")
    ask = floor.request("ben", "ask")
    assert ask is not None and ask.mode == "ask"
    assert floor.speaker.participant_id == "ana" and floor.queue == []
    assert floor.active("ben", "ask") is ask and floor.active("ben", "team") is None
    assert floor.request("ben", "ask") is None  # one ask at a time
    assert floor.release("ben", "ask") is ask and floor.active("ben", "ask") is None
