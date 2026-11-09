# Relay

A voice-first team chatroom. Relay turns each utterance into a concise
summary in one shared feed, tracks the requests in it, and keeps messages with outstanding
requests highlighted until they're completed or cancelled. **Ask Relay** answers read-only
questions about the room privately, with an option to share the answer.

```
backend/   Python · FastAPI · SQLite      (uv)
frontend/  Next.js · TypeScript · Tailwind (npm)
```

## Run it

Needs Python 3.13+ with [uv](https://docs.astral.sh/uv/), and Node 20+.

```bash
# 1. Keys
cp backend/.env.example backend/.env    # fill in the keys
cp frontend/.env.example frontend/.env.local   # optional: only if the backend isn't on localhost:8000

# 2. Backend  → http://localhost:8000
cd backend && uv run uvicorn relay_backend.main:app --reload

# 3. Frontend → http://localhost:3000
cd frontend && npm install && npm run dev
```

Open the app at **http://localhost:3000** (not 127.0.0.1: the backend's CORS setting allows
`localhost` only). The backend logs any missing provider keys at startup, and the room's status pills
show which providers are configured.

Optional backend settings in `backend/.env`:

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.6-flash` | Reasoning model |
| `RELAY_DB` | `backend/relay.db` | SQLite database file |
| `RELAY_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed frontend origins |
| `RELAY_RETENTION_DAYS` | `7` | Rooms older than this are purged at startup |
| `RELAY_CLARIFICATION_TTL` | `300` | Seconds a clarification question stays open |
| `RELAY_TURN_LEASE` | `5` | Seconds a disconnected speaker keeps their turn |

## Try it

Open two browser windows, create a room in one and join with the code in
the other. Enable the microphone, then hold **Hold to talk** (or Space):

- "Alex, bring two wireless mics to stage B by six thirty" → a highlighted message with a request.
- As Alex: "On it, five minutes" → accepted, still highlighted. "Done" → highlight clears.
- As the requester: "Actually make that four" → the message shows the new wording, and the request returns to Open until Alex confirms.
- **Ask Relay** → "What's still outstanding?" → private answer with sources; **Share to room** posts a labelled copy.

## How it works

- **Turns.** The server grants one team speaker at a time (others queue). Only the granted speaker's
  LiveKit mic is unmuted, and only their PCM audio (sent over the app websocket) is transcribed by
  Deepgram. A disconnected speaker loses the turn after a 5 s lease.
- **Pipeline** (`pipeline.py`): finalized utterance → dedupe by (participant, turn) → Gemini with
  `name`, `role`, `content`, `mode`, `source_utterance_id` and a compact `current_state` → every
  proposal through the request engine → one atomic commit → broadcast.
  Failures change nothing and tell the speaker privately.
- **Request engine** (`requests_engine.py`): the single path for AI proposals and manual actions —
  identity, permissions, lifecycle transitions, `expected_version`, idempotency keys. Material
  changes (description, quantity, destination, instructions, assignee) bump `instruction_revision`,
  clear acceptance and send active work back to Open.
- **Questions** use only read-only tools (`get_status`, `get_request`, `search_messages`,
  `get_updates_since`), and the answer is spoken to the asker through ElevenLabs. Private exchanges
  never enter shared state, prompts, snapshots or replay.

Deliberate simplifications for a demo: single backend process with a per-room lock; mic audio reaches
the backend over the app websocket rather than a server-side LiveKit worker; spoken answers are played by
the asker's browser rather than published as a LiveKit track; data older than 7 days is purged at startup.

## Tests

```bash
cd backend && uv run pytest
```

The AI providers are stubbed, so tests need no keys and never touch `relay.db`.

| File | Covers |
|---|---|
| `test_request_lifecycle.py` | Transitions, material changes and re-acceptance, reassignment, versions, idempotency |
| `test_permissions.py` | Who may act on a request, isolation between rooms |
| `test_pipeline.py` | Utterance → proposals → commit, duplicate turns, invalid proposals, AI failure, clarifications |
| `test_questions.py` | Ask Relay is read-only and private until shared |
| `test_turns.py` | One speaker at a time, the queue, private ask turns |
| `test_rooms.py` | Joining, websocket auth, deleting a room |
| `test_llm.py` | Internal ids are stripped from model text |
