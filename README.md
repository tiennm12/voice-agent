# Voice Agent (Vietnamese)

Low-latency Vietnamese voice agent. Browser mic -> Node.js backend -> OpenAI
Realtime API (speech-to-text) -> GPT-4.1-mini (streaming) -> ElevenLabs
(streaming TTS) -> browser playback. See
`docs/superpowers/specs/2026-08-03-voice-agent-design.md` for the full design.

## Setup

1. `npm install`
2. Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`,
   `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID`.
3. Edit `config/systemDoc.md` with the real reference document for the agent
   (it is embedded directly into the system prompt).
4. `npm start`, then open `http://localhost:3000` in Chrome.

## Automated tests

`npm test` runs the unit tests (pure logic + client wrappers against fakes —
no real API calls, no keys required).

## Manual verification checklist

Run these against the real APIs (requires valid keys in `.env`):

- [ ] Click "Bắt đầu gọi", speak a short question — transcript appears, agent
      responds with Vietnamese audio.
- [ ] Ask a question answerable from `config/systemDoc.md` — the response
      reflects the document content.
- [ ] Ask something outside the document — the agent says it doesn't know
      rather than inventing an answer.
- [ ] While the agent is speaking, speak again (barge-in) — playback stops
      within roughly one audio chunk and a new turn starts.
- [ ] Click "Kết thúc" mid-call — the WebSocket, Realtime session, and any
      open ElevenLabs stream all close (check server logs for no dangling
      connections/errors).
- [ ] Check the server console `[latency]` log lines for
      `first_llm_token_ms` and `first_tts_chunk_ms` across a few turns to
      spot regressions or outliers.
