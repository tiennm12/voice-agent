"""pipeline=script: no chat model, no live TTS. Each caller turn moves one line down the owner's script,
and every line is spoken from audio ElevenLabs rendered once and kept on disk."""
import asyncio
import hashlib
import logging
import os

import aiohttp

from .config import HANGUP_MARKER
from .tts_client import AUDIO_FORMATS, VOICE_SETTINGS

log = logging.getLogger(__name__)

# Said at any point, these skip the remaining questions: the caller is done.
GOODBYE_WORDS = ('tạm biệt', 'chào nhé', 'không cần', 'không có gì', 'vậy thôi', 'thế thôi', 'hết rồi')
RENDER_TIMEOUT = 10  # seconds for ElevenLabs to render one line


def script_replies(lines):
    """An llm_stream for SessionOrchestrator: the next line of the script, the last one ending the call."""
    async def reply(messages):
        heard = [message['content'] for message in messages if message['role'] == 'user']
        # ponytail: one transcript = one step, so VAD splitting one answer in two skips a question.
        # Upgrade: a longer vad_silence_ms for this pipeline, or merge transcripts that arrive close together.
        step = len(heard) - 1
        if step >= len(lines) - 1 or any(word in heard[-1].lower() for word in GOODBYE_WORDS):
            yield lines[-1] + HANGUP_MARKER
        else:
            yield lines[step]

    return reply


class CachedTts:
    """A TtsClient stand-in that speaks whole lines from a disk cache, rendering a missing one over REST.

    Emits: 'audio_chunk' (bytes), 'turn_complete'.
    """

    def __init__(self, *, http, cache_dir, api_key, voice_id, model_id, audio='mp3'):
        self._http = http
        self._cache_dir = cache_dir
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._audio = audio
        self._output_format, self.bytes_per_second = AUDIO_FORMATS[audio]
        self.characters_sent = 0  # what ElevenLabs bills on: lines rendered during this call only
        self._handlers = {}
        self._text = []
        self._loads = {}  # path -> task with that line's audio, so a line is fetched once per call

    def on(self, name, handler):
        self._handlers[name] = handler

    def emit(self, name, *args):
        handler = self._handlers.get(name)
        if handler:
            handler(*args)

    async def connect(self):
        pass

    async def send_text(self, text):
        self._text.append(text)

    async def end_turn(self):
        text, self._text = ' '.join(self._text), []
        # Shielded: a barge-in cancels the turn, not the render, which the next call can still use.
        audio = await asyncio.shield(self._load(text))
        self.emit('audio_chunk', audio)
        self.emit('turn_complete')

    def abort(self):
        self._text = []

    def prefetch(self, lines):
        """Start rendering, in the background, every line not on disk yet."""
        for line in lines:
            self._load(line)

    def _load(self, text):
        text = ' '.join(text.split())  # the chunker splits on whitespace; the key must not care
        key = hashlib.sha256(f'{self._voice_id}|{self._model_id}|{self._output_format}|{text}'.encode()).hexdigest()
        path = self._cache_dir / f'{key}.{self._audio}'
        if path not in self._loads:
            task = asyncio.ensure_future(self._read_or_render(path, text))
            task.add_done_callback(lambda task: self._forget_failed(path, task))
            self._loads[path] = task
        return self._loads[path]

    def _forget_failed(self, path, task):
        if not task.cancelled() and task.exception():
            log.error('rendering a script line failed', exc_info=task.exception())
            self._loads.pop(path, None)  # the next turn tries again

    async def _read_or_render(self, path, text):
        if path.exists():
            return path.read_bytes()
        async with self._http.post(
            f'https://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}?output_format={self._output_format}',
            json={'text': text, 'model_id': self._model_id, 'voice_settings': VOICE_SETTINGS},
            headers={'xi-api-key': self._api_key},
            timeout=aiohttp.ClientTimeout(total=RENDER_TIMEOUT),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f'ElevenLabs answered {response.status}: {(await response.text())[:200]}')
            audio = await response.read()
        self.characters_sent += len(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(f'.{os.getpid()}-{id(self)}.part')
        partial.write_bytes(audio)
        os.replace(partial, path)  # another call reading the same line never sees half a file
        return audio
