# Smart Voicemail Agent (Vietnamese)

An AI assistant that answers the phone for someone who can't pick up: it greets
the caller, takes a message, hangs up on its own, then summarizes the call,
notifies the owner on Telegram and files it in a web dashboard. Product
requirements: `docs/PRD.md`.

Caller audio -> Python backend -> OpenAI Realtime API (speech-to-text + VAD) ->
gpt-6-luna with reasoning off (streaming) -> ElevenLabs (streaming TTS) -> caller. That is the
default `classic` pipeline; `hybrid` and `realtime` (below) answer faster. The caller is
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

`data/secret.key` signs login sessions and call links. It is created on first
start; deleting it logs everyone out and voids links already sent.

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
the caller's last word to the first audio of the answer), with gpt-4.1-mini as
the chat model:

| Setup | Median |
|---|---|
| No knobs | 2.7 s |
| `VAD_SILENCE_MS=300` | 2.15 s |
| … + `ELEVENLABS_AUTO_MODE=1` | 2.1 s (TTS step 340-550 ms -> 130-210 ms) |
| … + `OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe` | 2.0 s (no gain in the transcription step) |
| … + `OPENAI_CHAT_MODEL=gpt-4.1-nano` | 1.9 s |

The chat model itself, no knobs, same method:

| Chat model | Median reply | LLM first token | Price per 1M tokens (in / out) |
|---|---|---|---|
| gpt-4.1-mini | 2.4 s | 0.7 s | $0.40 / $1.60 |
| gpt-4o-mini | 2.5 s | 0.75 s | $0.15 / $0.60 |
| gpt-6-luna, reasoning off (the default) | 3.0 s | 0.9 s | $0.10 / $0.50 |

gpt-6-luna reasons unless told not to: left at its own default its first token
took 4.4 s. Hence `reasoning_effort=none`, which is applied automatically with
the default model. Another model is one line in `.env` (`OPENAI_CHAT_MODEL`)
plus its two prices in `config/pricing.json`.

## Realtime and hybrid pipelines

`PIPELINE=realtime` (or `?pipeline=realtime` on `/call.html`, which has a drop-down for it) runs
one OpenAI Realtime session per call: the caller's audio goes in and the agent's voice comes out of the
same socket, with OpenAI voices instead of ElevenLabs.

`PIPELINE=hybrid` keeps the Realtime session for listening but has it answer in text, which
ElevenLabs speaks: the model hears the audio itself, so no separate transcription sits in the wait,
and the call keeps the owner's ElevenLabs voice (and a cloned one). Its latency line splits
`llm_ms` (caller stopped -> first word of text) and `tts_ms` (first word -> first audio).

All three, 2026-10-17, same sentence, median of 5 calls each (`scripts/bench.py`):

| Pipeline | Median | Runs |
|---|---|---|
| classic (gpt-6-luna) | 2.72 s | 2.57-2.83 s |
| hybrid, gpt-realtime-2.1-mini | 1.47 s | 1.34-1.66 s |
| realtime, gpt-realtime-2.1-mini | 1.28 s | 1.22-1.36 s |
| hybrid + `elevenlabs_auto_mode=1` | 1.34 s | 1.16-2.20 s (one outlier) |

Earlier realtime measurements, same method, median of 5-6 calls:

| Setup | Median |
|---|---|
| classic, gpt-4.1-mini | 2.4-2.7 s |
| realtime gpt-realtime-2.1 (1 call) | 1.8 s |
| realtime gpt-realtime-2.1-mini, server_vad | 1.26 s |
| ... + `agent_reasoning=minimal` | 1.32 s (no gain) |
| ... + `vad_silence_ms=300` | 1.18 s (one 2.4 s outlier) |
| ... + `vad_type=semantic_vad` | 1.4-1.5 s, more spread (3.3 s outlier) |
| ... + semantic_vad, eagerness high | 1.5 s, more spread |

semantic_vad did not help on a synthesized caller. The cost of a realtime call is priced from
`pricing.json` -> `realtime` (per model) into the call's cost; the hangup is the `end_call` tool.

The fixed greeting starts in 0.3-0.4 s. Getting replies under a second
needs more than knobs: transcription and the LLM's first token cost about 0.6 s
each. The knobs are off by default because each trades something (cutting in on
pauses, choppier speech, a weaker model) that should be judged by ear.

## Trying settings

`/call.html` has a "Cài đặt thử" panel: speech-to-text service (OpenAI or ElevenLabs
Scribe), silence before a turn ends, VAD threshold, chat model, TTS model and auto
mode. They apply to that one call and are remembered in the browser; blank means the
server's default. Each reply shows its wait split into stt / llm / tts. `scripts/probe.py
... --query 'stt_provider=elevenlabs'` does the same from the command line, and
`scripts/stt_bench.py` compares the two speech-to-text services on synthesized Vietnamese.
First numbers: end of speech to final transcript, median over 5 sentences — OpenAI
1.2 s, Scribe 0.77 s; judge accuracy by ear on your own line.

## Deploy behind nginx

The prod server serves the app under a path prefix, `https://chatbot2.vietnamobile.ai/voiceagent/`;
the frontend only uses page-relative URLs, so any prefix works. `deploy/` holds that server's
systemd unit and nginx location. The prod `.env` adds:

- `HOST=127.0.0.1`: only nginx can reach the app.
- `DATA_DIR=/var/lib/voiceagent`: the database, recordings and `secret.key` live outside the
  code, so a redeploy never touches them.
- `PUBLIC_BASE_URL=https://chatbot2.vietnamobile.ai/voiceagent`: for the Telegram "listen" button.

Redeploy (from the repo root; never ships `.env` or `data/`):

    tar --exclude=.venv --exclude=.git --exclude=data --exclude=.env --exclude=__pycache__ \
        --exclude=.pytest_cache --exclude=.superpowers --exclude=.code-review-graph --exclude=.claude \
        -czf ../voiceagent.tgz .
    scp ../voiceagent.tgz dev2@SERVER:/tmp/
    ssh dev2@SERVER 'cd /home/dev/tiennm/voiceagent && tar xzf /tmp/voiceagent.tgz &&
        .venv/bin/pip install -q -r requirements.txt && sudo systemctl restart voiceagent'

Logs: `sudo journalctl -u voiceagent -f`. Telegram chat id: send the bot a message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and take `message.chat.id`.

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
