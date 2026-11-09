# How AI was used to build Relay

I built Relay with Claude Code, Anthropic's coding agent, running Claude Opus 5.5. Claude wrote most of the code. My role was writing the spec, making the product decisions, testing the app, and directing each change.

## Spec-driven development

I followed spec-driven development: write a detailed specification first, then let the AI build against it. I wrote the spec myself (`Voice_Coordination_Assistant_Specification.docx`) before any code existed. It covered:

- **Requirements:** what Relay must do: hold-to-talk, summarised messages, request tracking, corrections, Ask Relay, and what counts as done.
- **Tech stack:** Next.js, TypeScript and Tailwind for the frontend; FastAPI and SQLite for the backend; Gemini, LiveKit, Deepgram and ElevenLabs as providers.
- **Limitations:** what is out of scope for this version, such as translation and password-protected rooms.
- **API design:** REST routes, the websocket event stream, request lifecycle and validation rules.
- **Frontend UI preview:** mockups of the join page, chat feed and Ask Relay panel.

The spec gave Claude a single source of truth. Instead of describing features one prompt at a time, I pointed Claude at the spec. Claude proposed a build plan, I approved it, and it built the backend, tests and frontend in that order. When I changed the spec (for example, switching the LLM provider), Claude updated the plan to match.

## What I kept control of

- The product idea, requirements and design, through the spec.
- API keys and billing, which I set up myself. Keys stay in `backend/.env`, which is not committed.
- Product decisions such as which AI model to use, trading speed against quality.
- Final review of the app's behaviour and of the write-up.
