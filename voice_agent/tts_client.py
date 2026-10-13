import base64

from .ws_client import JsonWsClient

# name -> (ElevenLabs output_format, bytes per second). Both are constant bitrate, so
# bytes received convert straight to seconds of playback.
AUDIO_FORMATS = {
    'mp3': ('mp3_44100_128', 128_000 // 8),  # played by an <audio> element (MediaSource)
    'pcm': ('pcm_24000', 24_000 * 2),  # 16-bit mono, played through Web Audio
}


class TtsClient(JsonWsClient):
    """ElevenLabs streaming-input TTS: one WebSocket per spoken turn.

    Emits: 'audio_chunk' (bytes), 'turn_complete', 'error' (exception), 'disconnected'.
    """

    def __init__(self, *, api_key, voice_id, model_id, ws_connect, audio='mp3'):
        super().__init__(ws_connect)
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._output_format, self.bytes_per_second = AUDIO_FORMATS[audio]
        self.characters_sent = 0  # what ElevenLabs bills on

    async def connect(self):
        await self._open(
            f'wss://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}/stream-input'
            f'?model_id={self._model_id}&output_format={self._output_format}',
            {'xi-api-key': self._api_key},
            {'text': ' ', 'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}},
        )

    async def send_text(self, text):
        payload = f'{text} '
        self.characters_sent += len(payload)
        await self._send({'text': payload})

    async def end_turn(self):
        await self._send({'text': ''})

    abort = JsonWsClient.close

    def _handle(self, event):
        if event.get('audio'):
            self.emit('audio_chunk', base64.b64decode(event['audio']))
        if event.get('isFinal'):
            # ElevenLabs closes the socket on its own right after this: expected, not a drop.
            self._expect_close = True
            self.emit('turn_complete')
