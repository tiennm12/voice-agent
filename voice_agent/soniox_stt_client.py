from .ws_client import JsonWsClient

URL = 'wss://stt-rt.soniox.com/transcribe-websocket'
SAMPLE_RATE = 24000
BYTES_PER_SECOND = SAMPLE_RATE * 2
END = '<end>'  # the token Soniox sends once it judges the caller has finished a turn


class SonioxSttClient(JsonWsClient):
    """Soniox real-time STT with semantic endpointing; same events as RealtimeSttClient.

    Emits: 'speech_started', 'speech_stopped', 'transcript_final', 'usage', 'error', 'disconnected'.
    Soniox decides the end of a turn from the words and the intonation, not from silence alone,
    and marks it with an <end> token. Like Scribe it has no speech events: the first word of a
    turn stands in for speech_started, and speech_stopped is emitted right before the transcript.
    """

    def __init__(self, *, api_key, ws_connect, language='vi', vad_silence_ms=None, endpoint_level=None):
        super().__init__(ws_connect)
        self._api_key = api_key
        self._config = {
            'model': 'stt-rt-v5',
            'audio_format': 'pcm_s16le',
            'sample_rate': SAMPLE_RATE,
            'num_channels': 1,
            'enable_endpoint_detection': True,
        }
        if language:
            self._config['language_hints'] = [language]
        if vad_silence_ms is not None:
            # The longest Soniox waits after speech before ending the turn; it only takes 500-3000.
            self._config['max_endpoint_delay_ms'] = min(3000, max(500, vad_silence_ms))
        if endpoint_level is not None:
            self._config['endpoint_latency_adjustment_level'] = endpoint_level  # 0-3: higher ends turns sooner
        self._speaking = False
        self._final = ''  # final text of the turn so far
        self._bytes_sent = 0

    async def connect(self):
        if not self._api_key:
            raise RuntimeError('SONIOX_API_KEY is not set')
        await self._open(URL, {'Authorization': f'Bearer {self._api_key}'}, self._config)

    async def send_audio_chunk(self, data):
        self._bytes_sent += len(data)
        await self._send_bytes(data)

    def close(self):
        # Soniox bills the seconds streamed: report them once, as the call ends.
        # ponytail: priced at pricing.json stt.per_minute (OpenAI's rate), above Soniox's own
        # ~$0.002/min. Give duration-billed providers their own rate if the estimate matters.
        if self._bytes_sent:
            self.emit('usage', {'type': 'duration', 'seconds': self._bytes_sent / BYTES_PER_SECOND})
            self._bytes_sent = 0
        super().close()

    def _handle(self, event):
        if event.get('error_code'):
            self.emit('error', RuntimeError(f"Soniox {event['error_code']}: {event.get('error_message', '')}"))
            return
        for token in event.get('tokens') or []:
            text = token.get('text') or ''
            if token.get('is_final') and text == END:
                self._speaking = False
                final, self._final = self._final.strip(), ''
                self.emit('speech_stopped')
                self.emit('transcript_final', final or None)
                continue
            if text.strip() and not self._speaking:
                self._speaking = True
                self.emit('speech_started')
            if token.get('is_final'):
                self._final += text  # non-final text is a guess Soniox replaces: never kept
