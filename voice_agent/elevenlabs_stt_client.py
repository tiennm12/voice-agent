import base64
from urllib.parse import urlencode

from .ws_client import JsonWsClient

SAMPLE_RATE = 24000
BYTES_PER_SECOND = SAMPLE_RATE * 2
SILENCE = bytes(SAMPLE_RATE // 50 * 2)  # 20 ms; the socket needs a first message, and this says nothing


class ElevenLabsSttClient(JsonWsClient):
    """ElevenLabs Scribe v2 Realtime with its own VAD; same events as RealtimeSttClient.

    Emits: 'speech_started', 'speech_stopped', 'transcript_final', 'usage', 'error', 'disconnected'.
    Scribe has no speech events: the first partial transcript after a commit stands in for
    speech_started, and speech_stopped is emitted right before the committed transcript, so
    the stt_ms of the latency log reads ~0 (the silence wait is inside response_ms).
    """

    def __init__(self, *, api_key, ws_connect, language='vi', vad_silence_ms=None, vad_threshold=None):
        super().__init__(ws_connect)
        self._api_key = api_key
        self._query = {
            'model_id': 'scribe_v2_realtime',
            'audio_format': f'pcm_{SAMPLE_RATE}',
            'commit_strategy': 'vad',
        }
        if language:
            self._query['language_code'] = language
        if vad_silence_ms is not None:
            self._query['vad_silence_threshold_secs'] = vad_silence_ms / 1000
        if vad_threshold is not None:
            self._query['vad_threshold'] = vad_threshold
        self._speaking = False
        self._bytes_sent = 0

    async def connect(self):
        await self._open(
            f'wss://api.elevenlabs.io/v1/speech-to-text/realtime?{urlencode(self._query)}',
            {'xi-api-key': self._api_key},
            self._chunk(SILENCE),
        )

    async def send_audio_chunk(self, data):
        self._bytes_sent += len(data)
        await self._send(self._chunk(data))

    def close(self):
        # Scribe bills the seconds streamed, silence included: report them once, as the call ends.
        if self._bytes_sent:
            self.emit('usage', {'type': 'duration', 'seconds': self._bytes_sent / BYTES_PER_SECOND})
            self._bytes_sent = 0
        super().close()

    @staticmethod
    def _chunk(data):
        return {
            'message_type': 'input_audio_chunk', 'audio_base_64': base64.b64encode(data).decode('ascii'),
            'commit': False, 'sample_rate': SAMPLE_RATE,
        }

    def _handle(self, event):
        kind = event.get('message_type')
        if kind == 'partial_transcript':
            if event.get('text') and not self._speaking:
                self._speaking = True
                self.emit('speech_started')
        elif kind == 'committed_transcript':
            self._speaking = False
            self.emit('speech_stopped')
            self.emit('transcript_final', event.get('text') or None)
        elif event.get('error'):
            self.emit('error', RuntimeError(f"{kind}: {event['error']}"))
