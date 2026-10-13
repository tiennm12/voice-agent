# Smart Voicemail Agent (Vietnamese)

An AI assistant that answers the phone for someone who can't pick up: it greets
the caller, takes a message, hangs up on its own, then summarizes the call,
notifies the owner on Telegram and files it in a web dashboard. Product
requirements: `docs/PRD.md`.

Caller audio -> Python backend -> OpenAI Realtime API (speech-to-text + VAD) ->
GPT-4.1-mini (streaming) -> ElevenLabs (streaming TTS) -> caller. The caller is
either a browser microphone (`/call.html`) or a real phone call bridged from an
eTelecom extension (`/bridge.html`).

## Setup

Needs Python 3.11+.

1. `python -m venv .venv`, then `.venv/bin/pip install -r requirements.txt`
   (Windows: `.venv\Scripts\pip`).
2. Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`,
   `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `DASHBOARD_PASSWORD` and
   `OWNER_NAME`. Everything else in that file is optional and explained there.
3. `.venv/bin/python -m voice_agent.server`, then open `http://localhost:3000`
   in Chrome and log in with `DASHBOARD_PASSWORD`.

If the browser shows an older page after an upgrade, reload it once: builds
before the dashboard were served without cache headers.

## The dashboard

- **Hộp thư** — missed calls, newest first, with unread / urgent / spam
  filters. Opening a call shows the summary, the caller's recording, the
  transcript as a chat, and what the call cost.
- **Thống kê** — calls, urgent calls, spam and cost per day, week or month.
- **Cài đặt** — owner name, greeting, the assistant's instructions, a
  "in a meeting" status, working hours with their own greeting, the agent's
  voice (and creating one from your own recordings), language, blocked numbers.
  Changes apply from the next call.

Listing or cloning voices needs an ElevenLabs API key with the `voices_read` /
`voices_write` permissions; a text-to-speech-only key still works for calls.

## What a call leaves behind

- A row in `data/voicemail.db` (SQLite): caller number, transcript, summary
  (`caller_name`, `summary`, `urgency`, `category`), usage and estimated cost.
- `data/calls/<id>/caller.wav` — the caller's side of the call.
- Calls older than `RETENTION_DAYS` (default 30) are deleted, recording
  included.

The cost is an estimate: usage reported by the APIs (LLM tokens, transcription
tokens, TTS characters) times `config/pricing.json`. Check that file against
your own plans; `usd_to_vnd` is a reference rate.

## Telegram

With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set, every finished call is
announced with its summary and cost.

- With `PUBLIC_BASE_URL` set too, the message carries a "Nghe ghi âm" button
  that opens that one call for a week without logging in.
- An urgent call is flagged and repeated every `URGENT_REMINDER_MINUTES` (three
  times at most) until it is opened.
- A call the summary files as spam is not announced.

## Answering real calls through eTelecom

eTelecom extensions speak SIP over WebSocket with WebRTC audio, so the bridge
runs in a browser tab:

1. In cs.etelecom.vn, route a hotline to an extension and note its extension
   number, password and tenant domain.
2. Forward the owner's unanswered/busy calls to that hotline.
3. Open `https://<server>/bridge.html` in Chrome, log the extension in, and
   leave the tab open. Incoming calls are answered by the agent; numbers on the
   blocked list are rejected without being picked up.

One tab handles one extension and one call at a time, and may sit in the
background. The page must be served over HTTPS (or from localhost). The
extension password stays in the browser.

## Response time

The server logs one line per reply:

    [latency] stt_ms=591 llm_ms=734 tts_ms=337 response_ms=1669

`stt_ms` is from the caller going quiet to the transcript, `llm_ms` from the
transcript to the first token, `tts_ms` from the first text sent to the first
audio back, `response_ms` the whole wait. The caller waits a little longer
still: the silence it takes to decide they have stopped (`VAD_SILENCE_MS`).

Measured end to end with `scripts/probe.py` (median of 5 real calls each, from
the caller's last word to the first audio of the answer):

| Setup | Median |
|---|---|
| Defaults | 2.7 s |
| `VAD_SILENCE_MS=300` | 2.15 s |
| … + `ELEVENLABS_AUTO_MODE=1` | 2.1 s (TTS step 340-550 ms -> 130-210 ms) |
| … + `OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe` | 2.0 s (no gain in the transcription step) |
| … + `OPENAI_CHAT_MODEL=gpt-4.1-nano` | 1.9 s |

The fixed greeting starts in about 0.15 s. Getting replies under a second
needs more than knobs: transcription and the LLM's first token cost about 0.6 s
each. The knobs are off by default because each trades something (cutting in on
pauses, choppier speech, a weaker model) that should be judged by ear.

## Tests and checks

`.venv/bin/python -m pytest` — logic, client wrappers, and the server wired to
fake sockets. No real API calls, no keys required.

`python scripts/probe.py PORT SECONDS --say "…"` places a real call against a
running server, with a synthesized caller voice (see the file's header).

## Manual verification checklist

Against the real APIs (valid keys in `.env`):

- [ ] `/call.html`: click "Bắt đầu gọi" — the greeting plays right away.
- [ ] Leave a message with your name — the agent confirms it, says goodbye and
      the call ends by itself.
- [ ] Say nothing after the greeting — the call ends after
      `SILENCE_TIMEOUT_SECONDS`.
- [ ] While the agent is speaking, speak again (barge-in) — playback stops and
      a new turn starts; talking over the goodbye keeps the call open.
- [ ] The call shows up in the dashboard with summary, transcript, cost and a
      recording that plays.
- [ ] With Telegram configured, the summary arrives; the button opens the call
      on a phone; an urgent message is repeated until opened.
- [ ] Change the greeting or the status in Cài đặt — the next call uses it.
- [ ] Bridge: log an extension in on `/bridge.html`, call the hotline from a
      phone — the agent answers, and hanging up from either side ends both legs.
