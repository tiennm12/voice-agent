import array
import base64
import logging
import time

from .call_timers import GOODBYE_GRACE, CallTimers
from .ws_client import JsonWsClient

log = logging.getLogger(__name__)

LIVE_URL = 'wss://api.openai.com/v1/live/sessions'
BYTES_PER_SECOND = 24_000 * 2  # pcm16 mono at 24 kHz, both ways
# Asked of a small model after every word the agent says; a confident yes ends the call
# once the goodbye has played. GPT-Live has no tool the voice model could call instead.
CALL_OVER = {
    'type': 'predicate',
    'name': 'call_over',
    'instructions': 'Trợ lý vừa nói lời chào tạm biệt để kết thúc cuộc gọi (không tính lời chào mở đầu).',
}
CALL_OVER_PROBABILITY = 0.8
# Peak sample above which the caller's audio counts as them talking (for the latency figure only).
# ponytail: a fixed level; a noisy line reads as speech. Use a VAD if the number ever matters.
CALLER_VOICE_PEAK = 1000


class LiveAgent(JsonWsClient):
    """One call handled by a GPT-Live session: a full-duplex voice model that listens while it speaks.

    The model decides when to talk and when to yield; this class only relays audio, joins its
    transcript deltas into turns, and hangs up. Same surface as the other agents: start(),
    stop(), handle_audio_chunk().

    Learnt from real sessions: the output audio is a continuous real-time stream, exact zeros
    while the model is quiet; told at startup, it greets first, word for word, once caller
    audio flows; transcripts lag the audio by about a second.
    """

    def __init__(
        self, *, api_key, model, instructions, voice, ws_connect, send_control, send_audio, greeting=None,
        on_hangup=None, silence_timeout=10.0, max_call=180.0, session_id='', on_usage=None,
    ):
        super().__init__(ws_connect)
        self._api_key = api_key
        self._model = model
        self._instructions = instructions
        self._voice = voice
        self._send_control = send_control
        self._send_audio = send_audio
        self._greeting = greeting
        self._silence_timeout = silence_timeout
        self._tag = f'[{session_id}]'
        self._on_usage = on_usage  # called once, with the session's length in seconds
        self._timers = CallTimers(on_hangup, max_call, self._tag)
        self._started_at = None
        self._speaking = False
        self._heard_at = None  # the caller's last voiced audio since the agent last spoke
        self._goodbye = False  # the agent said goodbye: hang up once it has played
        self._turn_role = None
        self._turn_text = ''
        self.on('disconnected', self._on_disconnected)

    async def start(self):
        self._started_at = time.monotonic()
        self._timers.start()
        instructions = self._instructions
        if self._greeting:
            instructions += f'\nCuộc gọi bắt đầu thì bạn nói trước, đúng nguyên văn câu chào: {self._greeting}'
        await self._open(LIVE_URL, {'Authorization': f'Bearer {self._api_key}'}, {
            'type': 'session.start',
            'session': {
                'model': self._model,
                'instructions': instructions,
                'audio': {'format': {'type': 'audio/pcm', 'rate': 24000}, 'output': {'voice': self._voice}},
                'triggers': [{
                    'type': 'decisions',
                    'event_type': 'session.output_transcript.delta',
                    'decisions': {'questions': [CALL_OVER]},
                }],
            },
        })
        self._timers.arm(self._silence_timeout)

    def stop(self):
        self._flush_turn()
        self._timers.stop()
        self.close()
        if self._started_at is not None and self._on_usage:
            # ponytail: billed per second of session; our own clock stands in for the
            # cumulative session.usage.updated snapshots. Read those if the two ever drift apart.
            self._on_usage(time.monotonic() - self._started_at)
        self._started_at = None

    async def handle_audio_chunk(self, data):
        if not self._speaking and _peak(data) > CALLER_VOICE_PEAK:
            self._heard_at = time.monotonic()
        await self._send({'type': 'session.input_audio.append', 'audio': base64.b64encode(data).decode('ascii')})

    def _on_disconnected(self):
        log.error('%s live session disconnected unexpectedly', self._tag)
        self._send_control({'type': 'error', 'message': 'Mất kết nối dịch vụ giọng nói.'})
        self._timers.hang_up()

    # --- upstream events -------------------------------------------------

    def _handle(self, event):
        kind = event.get('type')
        if kind == 'session.output_audio.delta':
            data = base64.b64decode(event['delta'])
            if data.strip(b'\x00'):
                self._on_audio(data)
            elif self._speaking:
                self._speaking = False
                self._send_control({'type': 'state', 'state': 'listening'})
        elif kind == 'session.input_transcript.delta':
            self._add_words('user', event.get('delta') or '')
            self._goodbye = False  # talking over the goodbye keeps the call open
            self._timers.arm(self._silence_timeout)
        elif kind == 'session.output_transcript.delta':
            self._add_words('assistant', event.get('delta') or '')
        elif kind == 'decision.event':
            self._on_decision(event)
        elif kind == 'error':
            message = (event.get('error') or {}).get('message') or 'Live API error'
            self.emit('error', RuntimeError(message))
            self._send_control({'type': 'error', 'message': message})
        elif kind == 'session.closed' and event.get('reason') != 'close_requested':
            self._on_disconnected()

    def _on_audio(self, data):
        now = time.monotonic()
        if not self._speaking:
            self._speaking = True
            self._send_control({'type': 'state', 'state': 'speaking'})
            if self._heard_at:
                latency = {'response_ms': round((now - self._heard_at) * 1000)}
                log.info('%s [latency] response_ms=%d', self._tag, latency['response_ms'])
                self._send_control({'type': 'latency', **latency})
                self._heard_at = None
        self._timers.note_audio(len(data), BYTES_PER_SECOND)
        self._send_audio(data)
        self._timers.arm(GOODBYE_GRACE if self._goodbye else self._silence_timeout)

    def _on_decision(self, event):
        answers = ((event.get('event') or {}).get('decision') or {}).get('answers') or []
        if any(a.get('name') == 'call_over' and (a.get('probability') or 0) >= CALL_OVER_PROBABILITY for a in answers):
            if not self._goodbye:
                log.info('%s goodbye detected', self._tag)
            self._goodbye = True
            self._timers.arm(GOODBYE_GRACE)

    # --- transcript --------------------------------------------------------

    def _add_words(self, role, text):
        # Deltas carry their own leading spaces and have no turn boundaries: a turn ends when the other side speaks.
        if role != self._turn_role:
            self._flush_turn()
            self._turn_role = role
        self._turn_text += text

    def _flush_turn(self):
        text = self._turn_text.strip()
        if text:
            self._send_control({'type': 'transcript', 'role': self._turn_role, 'text': text})
        self._turn_role, self._turn_text = None, ''


def _peak(data):
    samples = array.array('h', data[: len(data) // 2 * 2])
    return max(map(abs, samples), default=0)
