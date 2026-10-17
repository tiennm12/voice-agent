import asyncio
import base64
import logging
import time

from .call_timers import GOODBYE_GRACE, CallTimers
from .text_chunker import TextChunker
from .ws_client import JsonWsClient

log = logging.getLogger(__name__)

BYTES_PER_SECOND = 24_000 * 2  # pcm16 mono at 24 kHz, the same format the browser plays for 'pcm'

END_CALL_TOOL = {
    'type': 'function',
    'name': 'end_call',
    'description': 'Kết thúc cuộc gọi. Gọi sau khi đã ghi nhận lời nhắn và nói lời chào tạm biệt.',
    'parameters': {'type': 'object', 'properties': {}},
}


def _ms(start, end):
    return round((end - start) * 1000)


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
        self._silence_timeout = silence_timeout
        self._tag = f'[{session_id}]'
        self._on_usage = on_usage
        self._reasoning_effort = reasoning_effort
        self._turn_detection = {'create_response': True, 'interrupt_response': True} | (
            turn_detection or {'type': 'server_vad'}
        )
        self._transcription = {'model': transcribe_model} | ({'language': language} if language else {})
        self._timers = CallTimers(on_hangup, max_call, self._tag)
        self._bytes_per_second = BYTES_PER_SECOND
        self._speech_stopped_at = None  # until the first audio of the reply arrives
        self._speaking = False
        self._hangup_after_response = False
        self.on('disconnected', self._on_disconnected)

    async def start(self):
        self._timers.start()
        await self._open(
            f'wss://api.openai.com/v1/realtime?model={self._model}',
            {'Authorization': f'Bearer {self._api_key}'},
            {'type': 'session.update', 'session': self._session()},
        )
        if self._greeting:
            await self._greet()

    def stop(self):
        self._timers.stop()
        self.close()

    async def handle_audio_chunk(self, data):
        await self._send({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(data).decode('ascii')})

    def _session(self):
        return {
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
        }

    async def _greet(self):
        await self._send({
            'type': 'response.create',
            'response': {'instructions': f'Hãy nói đúng nguyên văn câu sau, không thêm gì: {self._greeting}'},
        })

    def _on_disconnected(self):
        # The one socket that hears (and maybe speaks) is gone: nothing more can happen on this call.
        log.error('%s realtime session disconnected unexpectedly', self._tag)
        self._send_control({'type': 'error', 'message': 'Mất kết nối dịch vụ giọng nói.'})
        self._timers.hang_up()

    # --- upstream events -------------------------------------------------

    def _handle(self, event):
        kind = event.get('type')
        if kind == 'input_audio_buffer.speech_started':
            self._on_speech_started()
        elif kind == 'input_audio_buffer.speech_stopped':
            self._speech_stopped_at = time.monotonic()
            self._timers.arm(self._silence_timeout)
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

    def _on_speech_started(self):
        # The server already cancelled its response; flush what the browser still holds.
        self._send_control({'type': 'interrupt'})
        self._timers.reset_playback()
        self._hangup_after_response = False
        self._speaking = False
        self._timers.disarm()

    def _on_audio(self, data):
        now = time.monotonic()
        if not self._speaking:
            self._speaking = True
            self._timers.disarm()
            self._send_control({'type': 'state', 'state': 'speaking'})
            if self._speech_stopped_at:
                latency = self._latency(now)
                log.info('%s [latency] %s', self._tag, ' '.join(f'{name}={value}' for name, value in latency.items()))
                self._send_control({'type': 'latency', **latency})
                self._speech_stopped_at = None
        self._timers.note_audio(len(data), self._bytes_per_second)
        self._send_audio(data)

    def _latency(self, audio_at):
        return {'response_ms': _ms(self._speech_stopped_at, audio_at)}

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
        self._finish_response()

    def _finish_response(self):
        """The reply has been sent in full: the caller's move, or the goodbye's grace period."""
        self._speaking = False
        self._send_control({'type': 'state', 'state': 'listening'})
        self._timers.arm(GOODBYE_GRACE if self._hangup_after_response else self._silence_timeout)


class HybridAgent(RealtimeAgent):
    """A Realtime session that listens to the caller and answers in text, spoken by ElevenLabs.

    The model hears the audio itself, so no separate transcription step sits in the wait
    (unlike the classic pipeline), and the agent keeps the owner's ElevenLabs voice (unlike
    the speech-to-speech one). One TTS socket per reply, opened as the reply starts.

    ponytail: a reply cut off by the caller stays whole in the model's conversation (text
    has no conversation.item.truncate). Fine for message-taking; revisit if replies get long.
    """

    def __init__(self, *, tts_client, **kwargs):
        super().__init__(voice=None, **kwargs)
        self._tts = tts_client
        self._bytes_per_second = tts_client.bytes_per_second
        self._chunks = None  # queue of the current reply's text chunks for TTS; None ends it
        self._chunker = None
        self._speaker = None  # task speaking the current reply
        self._first_text_at = None
        tts_client.on('audio_chunk', self._on_audio)
        tts_client.on('turn_complete', self._on_tts_turn_complete)
        tts_client.on('error', lambda err: self._send_control({'type': 'error', 'message': str(err)}))
        tts_client.on('disconnected', self._on_tts_disconnected)

    def stop(self):
        self._abort_reply()
        self._tts.abort()
        super().stop()

    def _session(self):
        session = super()._session() | {'output_modalities': ['text']}
        session['audio'] = {'input': session['audio']['input']}
        return session

    async def _greet(self):
        # Spoken straight away, no model round trip; the model is told it was said.
        self._open_reply()
        self._chunker.push(self._greeting)
        self._end_reply()
        self._send_control({'type': 'transcript', 'role': 'assistant', 'text': self._greeting})
        await self._send({
            'type': 'conversation.item.create',
            'item': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': self._greeting}]},
        })

    # --- one reply = one TTS turn ------------------------------------------

    def _open_reply(self):
        self._abort_reply()
        self._chunks = asyncio.Queue()
        self._chunker = TextChunker(on_flush=self._chunks.put_nowait)
        self._first_text_at = None
        self._speaker = asyncio.create_task(self._speak(self._chunks))
        self._speaker.add_done_callback(self._on_speaker_done)

    async def _speak(self, chunks):
        await self._tts.connect()  # in parallel with the model writing its first words
        while (text := await chunks.get()) is not None:
            await self._tts.send_text(text)
        await self._tts.end_turn()

    def _end_reply(self):
        self._chunker.end()
        self._chunks.put_nowait(None)
        self._chunker = None

    def _abort_reply(self):
        # _speaker stays set until TTS reports the turn complete: its text may all be sent
        # while audio is still streaming back, and that has to stop too.
        speaker, self._speaker = self._speaker, None
        if speaker:
            speaker.cancel()
            self._tts.abort()
        self._chunks = self._chunker = None

    def _on_speaker_done(self, task):
        if task.cancelled() or task.exception() is None or task is not self._speaker:
            return
        log.error('%s tts failed', self._tag, exc_info=task.exception())
        self._send_control({'type': 'error', 'message': str(task.exception())})
        self._speaker = None
        super()._finish_response()

    def _on_tts_turn_complete(self):
        self._speaker = None
        super()._finish_response()  # now the caller has heard it all (bar playback, which timers count)

    def _on_tts_disconnected(self):
        log.error('%s tts client disconnected unexpectedly', self._tag)
        self._abort_reply()
        self._send_control({'type': 'error', 'message': 'Mất kết nối dịch vụ giọng nói.'})
        super()._finish_response()

    # --- upstream events -------------------------------------------------

    def _handle(self, event):
        kind = event.get('type')
        if kind == 'response.created':
            self._open_reply()
        elif kind == 'response.output_text.delta':
            if self._chunker is None:
                self._open_reply()
            if self._first_text_at is None:
                self._first_text_at = time.monotonic()
            self._chunker.push(event.get('delta') or '')
        elif kind == 'response.output_text.done':
            if event.get('text'):
                self._send_control({'type': 'transcript', 'role': 'assistant', 'text': event['text']})
        else:
            super()._handle(event)

    def _on_speech_started(self):
        self._abort_reply()
        super()._on_speech_started()

    def _finish_response(self):
        if self._first_text_at is not None and self._chunker is not None:
            self._end_reply()  # the hangup timer is armed once TTS has finished: _on_tts_turn_complete
            return
        self._abort_reply()  # nothing to say (only end_call, or an empty reply)
        super()._finish_response()

    def _latency(self, audio_at):
        latency = super()._latency(audio_at)
        if self._first_text_at:
            latency = {
                'llm_ms': _ms(self._speech_stopped_at, self._first_text_at),
                'tts_ms': _ms(self._first_text_at, audio_at),
                **latency,
            }
        return latency
