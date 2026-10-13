# Smart Voicemail Agent (Vietnamese)

An AI assistant that answers the phone for someone who can't pick up: it greets
the caller, takes a message, hangs up on its own, then summarizes the call and
notifies the owner. Product requirements: `docs/PRD.md`.

Caller audio -> Python backend -> OpenAI Realtime API (speech-to-text + VAD) ->
GPT-4.1-mini (streaming) -> ElevenLabs (streaming TTS) -> caller. The caller is
either a browser microphone (`/`) or a real phone call bridged from an eTelecom
extension (`/bridge.html`).

## Setup

Needs Python 3.11+.

1. `python -m venv .venv`, then `.venv/bin/pip install -r requirements.txt`
   (Windows: `.venv\Scripts\pip`).
2. Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`,
   `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` and `OWNER_NAME`.
   `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are optional.
3. `.venv/bin/python -m voice_agent.server`, then open `http://localhost:3000`
   in Chrome.

`config/systemDoc.md` is the agent's persona (`{{OWNER_NAME}}` is filled in from
the env). `config/pricing.json` is the price table behind the per-call cost.

## What a call leaves behind

Each call gets a folder `data/calls/<start time>_<id>/`:

- `caller.wav` — the caller's side of the call.
- `call.json` — caller number, transcript, summary (`caller_name`, `summary`,
  `urgency`), usage counters and estimated cost.

The same summary and cost go to Telegram when it is configured, and the server
logs one `[cost]` line per call.

The cost is an estimate: usage reported by the APIs (LLM tokens, transcription
tokens, TTS characters) times `config/pricing.json`. Check that file against
your own plans; `usd_to_vnd` is a reference rate.

## Answering real calls through eTelecom

eTelecom extensions speak SIP over WebSocket with WebRTC audio, so the bridge
runs in a browser tab:

1. In cs.etelecom.vn, route a hotline to an extension and note its extension
   number, password and tenant domain.
2. Forward the owner's unanswered/busy calls to that hotline.
3. Open `https://<server>/bridge.html` in Chrome, log the extension in, and
   leave the tab open. Incoming calls are answered by the agent.

One tab handles one extension and one call at a time, and may sit in the
background. The page must be served over HTTPS (or from localhost). The
extension password stays in the browser.

## Automated tests

`.venv/bin/python -m pytest` — pure logic, client wrappers and the server
wired to fake sockets. No real API calls, no keys required.

## Manual verification checklist

Against the real APIs (valid keys in `.env`):

- [ ] Click "Bắt đầu gọi" — the fixed greeting plays right away.
- [ ] Leave a message with your name — the agent confirms it, says goodbye and
      the call ends by itself.
- [ ] Say nothing after the greeting — the call ends after
      `SILENCE_TIMEOUT_SECONDS`.
- [ ] While the agent is speaking, speak again (barge-in) — playback stops and
      a new turn starts; talking over the goodbye keeps the call open.
- [ ] `data/calls/…/call.json` has the transcript, summary and cost;
      `caller.wav` plays back.
- [ ] With Telegram configured, the summary arrives there.
- [ ] Server console `[latency]` lines (`first_llm_token_ms`,
      `first_tts_chunk_ms`) show no regressions across a few turns.
- [ ] Bridge: log an extension in on `/bridge.html`, call the hotline from a
      phone — the agent answers, and hanging up from either side ends both legs.
