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

```bash
# 1. Keys
cp backend/.env.example backend/.env    # fill in the keys

# 2. Backend  → http://localhost:8000
cd backend && uv run uvicorn relay_backend.main:app --reload

# 3. Frontend → http://localhost:3000
cd frontend && npm install && npm run dev
```

## Try it

Open two browser windows, create a room in one and join with the code in
the other. Enable the microphone, then hold **Hold to talk** (or Space):

- "Alex, bring two wireless mics to stage B by six thirty" → a highlighted message with a request.
- As Alex: "On it, five minutes" → accepted, still highlighted. "Done" → highlight clears.
- As the requester: "Actually make that four" → the request returns to Open and Alex is asked to confirm.
- **Ask Relay** → "What's still outstanding?" → private answer with sources; **Share to room** posts a labelled copy.

## How it works

- **Turns.** The server grants one team speaker at a time (others queue). Only the granted speaker's
  LiveKit mic is unmuted, and only their PCM audio (sent over the app websocket) is transcribed by
  Deepgram. A disconnected speaker loses the turn after a 5 s lease.
- **Pipeline** (`pipeline.py`): finalized utterance → dedupe by (participant, turn) → Gemini with
  `name`, `role`, `content`, `mode`, `source_utterance_id` and a compact `current_state` → every
  proposal through the request engine → one atomic commit → broadcast → optional speech.
  Failures change nothing and tell the speaker privately.
- **Request engine** (`requests_engine.py`): the single path for AI proposals and manual actions —
  identity, permissions, lifecycle transitions, `expected_version`, idempotency keys. Material
  changes (description, quantity, destination, instructions, assignee) bump `instruction_revision`,
  clear acceptance and send active work back to Open.
- **Questions** use only read-only tools (`get_status`, `get_request`, `search_messages`,
  `get_updates_since`). Private exchanges never enter shared state, prompts, snapshots or replay.

Deliberate simplifications for a demo: single backend process with a per-room lock; mic audio reaches
the backend over the app websocket rather than a server-side LiveKit worker; announcements are played by
each client rather than published as a LiveKit track; data older than 7 days is purged at startup.

## Tests

```bash
cd backend && uv run pytest
```

Covers lifecycle transitions, permissions, version conflicts, idempotency, duplicate utterances,
invalid proposals, AI failure, clarifications, read-only questions and private-exchange isolation
(with a stubbed LLM).
