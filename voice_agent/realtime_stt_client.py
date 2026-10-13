import base64

from .ws_client import JsonWsClient


class RealtimeSttClient(JsonWsClient):
    """OpenAI Realtime API used for speech-to-text + server VAD only (no response generation).

    Emits: 'speech_started', 'speech_stopped', 'transcript_final' (text), 'usage' (the
    transcription's billing usage dict), 'error' (exception), 'disconnected'.
    """

    def __init__(self, *, api_key, model, transcribe_model, ws_connect, language='vi'):
        super().__init__(ws_connect)
        self._api_key = api_key
        self._model = model
        # A language constrains/guides the transcription to it; None lets the model
        # detect it per utterance (less accurate for Vietnamese, hence the default).
        self._transcription = {'model': transcribe_model} | ({'language': language} if language else {})

    async def connect(self):
        await self._open(
            f'wss://api.openai.com/v1/realtime?model={self._model}',
            {'Authorization': f'Bearer {self._api_key}'},
            {
                'type': 'session.update',
                'session': {
                    'type': 'realtime',
                    'model': self._model,
                    'output_modalities': ['text'],
                    'audio': {
                        'input': {
                            'format': {'type': 'audio/pcm', 'rate': 24000},
                            'turn_detection': {
                                'type': 'server_vad',
                                'create_response': False,
                                'interrupt_response': False,
                            },
                            'transcription': self._transcription,
                            # 'near_field' suits a phone mic held close to the mouth (vs.
                            # 'far_field' for laptop/conference-room mics) and filters ambient
                            # noise from the input before it reaches VAD and the model.
                            'noise_reduction': {'type': 'near_field'},
                        },
                    },
                },
            },
        )

    async def send_audio_chunk(self, data):
        await self._send({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(data).decode('ascii')})

    def _handle(self, event):
        event_type = event.get('type')
        if event_type == 'input_audio_buffer.speech_started':
            self.emit('speech_started')
        elif event_type == 'input_audio_buffer.speech_stopped':
            self.emit('speech_stopped')
        elif event_type == 'conversation.item.input_audio_transcription.completed':
            if event.get('usage'):
                self.emit('usage', event['usage'])
            self.emit('transcript_final', event.get('transcript'))
        elif event_type == 'error':
            self.emit('error', RuntimeError((event.get('error') or {}).get('message') or 'Realtime API error'))
