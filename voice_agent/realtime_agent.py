import asyncio
import base64
import logging
import time

from .ws_client import JsonWsClient

log = logging.getLogger(__name__)

BYTES_PER_SECOND = 24_000 * 2  # pcm16 mono at 24 kHz, the same format the browser plays for 'pcm'
GOODBYE_GRACE = 0.5  # seconds between the goodbye finishing on the caller's end and the hangup

END_CALL_TOOL = {
    'type': 'function',
    'name': 'end_call',
    'description': 'Kết thúc cuộc gọi. Gọi sau khi đã ghi nhận lời nhắn và nói lời chào tạm biệt.',
    'parameters': {'type': 'object', 'properties': {}},
}


class RealtimeAgent(JsonWsClient):
    """One call handled by a single speech-to-speech OpenAI Realtime session.

    The caller's audio goes in and the agent's voice comes out of the same socket: no
    separate transcription, chat model or TTS step to wait for. Mirrors the surface
    server.py uses on SessionOrchestrator: start(), stop(), handle_audio_chunk().
    """

    def __init__(
        self, *, api_key, model, instructions, voice, ws_connect, send_control, send_audio,
        language='vi', turn_detection=None, transcribe_model='gpt-4o-transcribe', greeting=None,
        on_hangup=None, silence_timeout=10.0, max_call=180.0, session_id='', on_usage=None,
        reasoning_effort=None,
    ):
        super().__init__(ws_connect)
        self._api_key = api_key
        self._model = model
        self._instructions = instructions
        self._voice = voice
        self._send_control = send_control
        self._send_audio = send_audio
        self._greeting = greeting
        self._on_hangup = on_hangup
        self._silence_timeout = silence_timeout
        self._max_call = max_call
        self._tag = f'[{session_id}]'
        self._on_usage = on_usage
        self._reasoning_effort = reasoning_effort
        self._turn_detection = {'create_response': True, 'interrupt_response': True} | (
            turn_detection or {'type': 'server_vad'}
        )
        self._transcription = {'model': transcribe_model} | ({'language': language} if language else {})
        self._speech_stopped_at = None  # until the first audio of the reply arrives
        self._speaking = False
        self._hangup_after_response = False
        self._playback_ends_at = 0.0
        self._hangup_timer = None
        self._max_call_timer = None
        self._hung_up = False
        self.on('disconnected', self._on_disconnected)

    async def start(self):
        self._max_call_timer = asyncio.get_running_loop().call_later(self._max_call, self._hang_up)
        await self._open(
            f'wss://api.openai.com/v1/realtime?model={self._model}',
            {'Authorization': f'Bearer {self._api_key}'},
            {
                'type': 'session.update',
                'session': {
                    'type': 'realtime',
                    'model': self._model,
                    'output_modalities': ['audio'],
                    'instructions': self._instructions,
                    'tools': [END_CALL_TOOL],
                    **({'reasoning': {'effort': self._reasoning_effort}} if self._reasoning_effort else {}),
                    'audio': {
                        'input': {
                            'format': {'type': 'audio/pcm', 'rate': 24000},
                            'turn_detection': self._turn_detection,
                            'transcription': self._transcription,
                            'noise_reduction': {'type': 'near_field'},
                        },
                        'output': {'format': {'type': 'audio/pcm', 'rate': 24000}, 'voice': self._voice},
                    },
                },
            },
        )
        if self._greeting:
            await self._send({
                'type': 'response.create',
                'response': {'instructions': f'Hãy nói đúng nguyên văn câu sau, không thêm gì: {self._greeting}'},
            })

    def stop(self):
        if self._max_call_timer:
            self._max_call_timer.cancel()
        self._disarm_hangup()
        self.close()

    async def handle_audio_chunk(self, data):
        await self._send({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(data).decode('ascii')})

    # --- hanging up --------------------------------------------------------

    def _arm_hangup(self, delay):
        self._disarm_hangup()
        self._hangup_timer = asyncio.get_running_loop().call_later(delay, self._hang_up)

    def _disarm_hangup(self):
        if self._hangup_timer:
            self._hangup_timer.cancel()
            self._hangup_timer = None

    def _hang_up(self):
        self._disarm_hangup()
        if self._max_call_timer:
            self._max_call_timer.cancel()
        if self._hung_up:
            return
        self._hung_up = True
        log.info('%s hanging up', self._tag)
        if self._on_hangup:
            self._on_hangup()

    def _on_disconnected(self):
        # The one socket that hears and speaks is gone: nothing more can happen on this call.
        log.error('%s realtime session disconnected unexpectedly', self._tag)
        self._send_control({'type': 'error', 'message': 'Mất kết nối dịch vụ giọng nói.'})
        self._hang_up()

    # --- upstream events -------------------------------------------------

    def _handle(self, event):
        kind = event.get('type')
        if kind == 'input_audio_buffer.speech_started':
            # The server already cancelled its response; flush what the browser still holds.
            self._send_control({'type': 'interrupt'})
            self._playback_ends_at = 0.0
            self._hangup_after_response = False
            self._speaking = False
            self._disarm_hangup()
        elif kind == 'input_audio_buffer.speech_stopped':
            self._speech_stopped_at = time.monotonic()
            self._arm_hangup(self._silence_timeout)
        elif kind == 'response.output_audio.delta':
            self._on_audio(base64.b64decode(event['delta']))
        elif kind == 'conversation.item.input_audio_transcription.completed':
            text = (event.get('transcript') or '').strip()
            if text:
                self._send_control({'type': 'transcript', 'role': 'user', 'text': text})
            if event.get('usage') and self._on_usage:
                self._on_usage('stt', event['usage'])
        elif kind == 'response.output_audio_transcript.done':
            if event.get('transcript'):
                self._send_control({'type': 'transcript', 'role': 'assistant', 'text': event['transcript']})
        elif kind == 'response.done':
            self._on_response_done(event.get('response') or {})
        elif kind == 'error':
            self.emit('error', RuntimeError((event.get('error') or {}).get('message') or 'Realtime API error'))
            self._send_control({'type': 'error', 'message': (event.get('error') or {}).get('message', '')})

    def _on_audio(self, data):
        now = time.monotonic()
        if not self._speaking:
            self._speaking = True
            self._disarm_hangup()
            self._send_control({'type': 'state', 'state': 'speaking'})
            if self._speech_stopped_at:
                ms = round((now - self._speech_stopped_at) * 1000)
                log.info('%s [latency] response_ms=%d', self._tag, ms)
                self._send_control({'type': 'latency', 'response_ms': ms})
                self._speech_stopped_at = None
        self._playback_ends_at = max(self._playback_ends_at, now) + len(data) / BYTES_PER_SECOND
        self._send_audio(data)

    def _on_response_done(self, response):
        usage = response.get('usage')
        if usage and self._on_usage:
            self._on_usage('realtime', usage)
        outputs = response.get('output') or []
        self._hangup_after_response |= any(
            item.get('type') == 'function_call' and item.get('name') == 'end_call' for item in outputs
        )
        if response.get('status') == 'cancelled':
            return  # superseded by the caller talking over it: speech_started already reset things
        self._speaking = False
        self._send_control({'type': 'state', 'state': 'listening'})
        remaining = max(0.0, self._playback_ends_at - time.monotonic())
        self._arm_hangup(remaining + (GOODBYE_GRACE if self._hangup_after_response else self._silence_timeout))
