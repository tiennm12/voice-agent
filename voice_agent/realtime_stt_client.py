import base64
import logging

from .ws_client import JsonWsClient

log = logging.getLogger(__name__)


class RealtimeSttClient(JsonWsClient):
    """OpenAI Realtime API used for speech-to-text + server VAD only (no response generation).

    Emits: 'speech_started', 'speech_stopped', 'transcript_final' (text; None when the
    transcript is too unsure to trust), 'usage' (the transcription's billing usage dict),
    'error' (exception), 'disconnected'.
    """

    def __init__(
        self, *, api_key, model, transcribe_model, ws_connect, language='vi', vad_silence_ms=None, vad_threshold=None,
        min_logprob=None,
    ):
        super().__init__(ws_connect)
        self._api_key = api_key
        self._model = model
        # Noise that slips past VAD is transcribed as confident-looking nonsense, but the
        # model is unsure of it: a transcript whose mean token log-probability is below this is dropped.
        self._min_logprob = min_logprob
        self._turn_detection = {'type': 'server_vad', 'create_response': False, 'interrupt_response': False}
        # Both stay at the API defaults unless tuned. A shorter silence answers sooner
        # but cuts in on pauses; a higher threshold ignores more line noise and echo.
        if vad_silence_ms is not None:
            self._turn_detection['silence_duration_ms'] = vad_silence_ms
        if vad_threshold is not None:
            self._turn_detection['threshold'] = vad_threshold
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
                    'include': ['item.input_audio_transcription.logprobs'],
                    'audio': {
                        'input': {
                            'format': {'type': 'audio/pcm', 'rate': 24000},
                            'turn_detection': self._turn_detection,
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

    def _trusted(self, event):
        text = event.get('transcript')
        logprobs = [token['logprob'] for token in event.get('logprobs') or []]
        if not text or not logprobs:
            return text
        mean = sum(logprobs) / len(logprobs)
        dropped = self._min_logprob is not None and mean < self._min_logprob
        log.info('[stt] confidence=%.2f %s %r', mean, 'DROPPED' if dropped else 'kept', text)
        return None if dropped else text

    def _handle(self, event):
        event_type = event.get('type')
        if event_type == 'input_audio_buffer.speech_started':
            self.emit('speech_started')
        elif event_type == 'input_audio_buffer.speech_stopped':
            self.emit('speech_stopped')
        elif event_type == 'conversation.item.input_audio_transcription.completed':
            if event.get('usage'):
                self.emit('usage', event['usage'])
            self.emit('transcript_final', self._trusted(event))
        elif event_type == 'error':
            self.emit('error', RuntimeError((event.get('error') or {}).get('message') or 'Realtime API error'))
