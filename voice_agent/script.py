"""The owner's fixed script, spoken from audio ElevenLabs rendered once and kept on disk.

pipeline=script walks the script line by line and hands the turn to the chat model only when the caller
asks something; pipeline=hybrid with hybrid_script lets the Realtime model follow it. Either way a line
said word for word plays from the cache, anything else is read by ElevenLabs live.
"""
import asyncio
import hashlib
import logging
import os
import re
from functools import partial

import aiohttp

from .config import HANGUP_MARKER
from .tts_client import AUDIO_FORMATS, VOICE_SETTINGS

log = logging.getLogger(__name__)

# Said at any point, these skip the remaining questions: the caller is done.
GOODBYE_WORDS = ('tạm biệt', 'chào nhé', 'không cần', 'không có gì', 'vậy thôi', 'thế thôi', 'hết rồi')
# A caller turn with one of these asks something the script cannot answer: the chat model does.
QUESTION = re.compile(r'\?\s*$|\b(ai|gì|đâu|sao|bao giờ|khi nào|mấy giờ|thế nào|được không|có phải|bao nhiêu)\b')
# Appended to the chat model's system prompt in pipeline=script.
SCRIPT_LLM_RULE = (
    'Các câu hỏi lấy lời nhắn do hệ thống tự hỏi ngay sau câu trả lời của bạn: '
    'bạn chỉ trả lời thật ngắn điều người gọi vừa hỏi, không tự hỏi thêm câu nào.'
)
RENDER_TIMEOUT = 10  # seconds for ElevenLabs to render one line


def _normalize(text):
    return ' '.join(text.split())  # the chunker splits on whitespace: comparisons must not care


def off_script(text):
    """ponytail: keywords only. A false alarm just makes that turn slower (the chat model still answers);
    a miss reads the next question over the caller's. Upgrade: a small classifier."""
    return bool(QUESTION.search(text.lower()))


def script_replies(lines, llm_stream=None):
    """An llm_stream for SessionOrchestrator: the next line not said yet, the last one ending the call.

    With llm_stream, a caller who asks something gets the chat model's answer first, then the next line.
    """
    questions, closing = lines[:-1], lines[-1] + HANGUP_MARKER

    async def reply(messages):
        # Everything the caller said since the agent last spoke: a noise that cut off the answer to a
        # question ("mà") must not hide the question.
        unanswered = []
        for message in reversed(messages):
            if message['role'] != 'user':
                break
            unanswered.insert(0, message['content'])
        heard = ' '.join(unanswered)
        said = [_normalize(message['content']) for message in messages if message['role'] == 'assistant']
        # ponytail: a turn cut off by a VAD split still counts its line as said. Upgrade: a longer
        # vad_silence_ms for this pipeline, or merge transcripts that arrive close together.
        pending = [line for line in questions if not any(_normalize(line) in text for text in said)]
        if any(word in heard.lower() for word in GOODBYE_WORDS):
            yield closing
            return
        if llm_stream and off_script(heard):
            answer = ''
            async for token in llm_stream(messages):
                answer += token
                yield token
            if '[' in answer:  # the chat model said goodbye itself
                return
            yield ' '
        yield pending[0] if pending else closing

    return reply


def script_instructions(lines):
    """Appended to the Realtime model's instructions in pipeline=hybrid with hybrid_script."""
    questions = '\n'.join(f'{number}. {line}' for number, line in enumerate(lines[:-1], 1))
    return (
        'Kịch bản: sau câu chào, lần lượt hỏi các câu dưới đây, mỗi lượt đúng một câu, đọc đúng nguyên văn, '
        'không thêm bớt chữ nào, kể cả "Dạ":\n'
        f'{questions}\n'
        'Người gọi đã tự trả lời câu nào thì bỏ qua câu đó. Người gọi hỏi điều khác thì trả lời thật ngắn '
        'rồi hỏi tiếp câu kế tiếp. Hỏi xong thì nói đúng nguyên văn câu: ' + lines[-1]
    )


class CachedTts:
    """A TtsClient stand-in: a turn that is exactly one of the `known` lines plays from the disk cache
    (rendered over REST the first time); anything else goes to the `live` TtsClient as it arrives.
    Without `live`, every turn is rendered and cached.

    Emits: 'audio_chunk' (bytes), 'turn_complete', and the live client's 'error' / 'disconnected'.
    """

    def __init__(self, *, http, cache_dir, api_key, voice_id, model_id, audio='mp3', live=None, known=()):
        self._http = http
        self._cache_dir = cache_dir
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._audio = audio
        self._output_format, self.bytes_per_second = AUDIO_FORMATS[audio]
        self._rendered = 0
        self._handlers = {}
        self._text = []
        self._loads = {}  # path -> task with that line's audio, so a line is fetched once per call
        self._live = live
        self._known = {_normalize(line) for line in known}
        self._connecting = None
        self._live_turn = False  # this turn's text is going to the live client
        if live:
            for name in ('audio_chunk', 'turn_complete', 'error', 'disconnected'):
                live.on(name, partial(self.emit, name))

    @property
    def characters_sent(self):
        """What ElevenLabs bills on: lines rendered during this call plus everything read live."""
        return self._rendered + (self._live.characters_sent if self._live else 0)

    def on(self, name, handler):
        self._handlers[name] = handler

    def emit(self, name, *args):
        handler = self._handlers.get(name)
        if handler:
            handler(*args)

    async def connect(self):
        # Not awaited here: a turn that plays from the cache must not wait for an ElevenLabs handshake.
        if self._live:
            self._connecting = asyncio.ensure_future(self._live.connect())
            self._connecting.add_done_callback(lambda task: task.cancelled() or task.exception())

    async def send_text(self, text):
        self._text.append(text)
        if self._live_turn:
            await self._live.send_text(text)
        elif self._live and not any(line.startswith(_normalize(' '.join(self._text))) for line in self._known):
            await self._go_live()

    async def end_turn(self):
        text = _normalize(' '.join(self._text))
        if self._live and not self._live_turn and text not in self._known:
            await self._go_live()
        if self._live_turn:
            log.info('[tts] cache miss, read live: %s', text[:60])
            self._text, self._live_turn = [], False
            await self._live.end_turn()
            return
        self._text = []
        self._stop_live()
        # Shielded: a barge-in cancels the turn, not the render, which the next call can still use.
        audio = await asyncio.shield(self._load(text))
        log.info('[tts] cache hit: %s', text[:60])
        self.emit('audio_chunk', audio)
        self.emit('turn_complete')

    def abort(self):
        self._text, self._live_turn = [], False
        self._stop_live()

    def prefetch(self, lines):
        """Start rendering, in the background, every line not on disk yet."""
        for line in lines:
            self._load(line)

    async def _go_live(self):
        self._live_turn = True
        await self._connecting
        await self._live.send_text(' '.join(self._text))

    def _stop_live(self):
        if self._live:
            if self._connecting:
                self._connecting.cancel()
            self._live.abort()

    def _load(self, text):
        text = _normalize(text)
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
        self._rendered += len(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial_file = path.with_suffix(f'.{os.getpid()}-{id(self)}.part')
        partial_file.write_bytes(audio)
        os.replace(partial_file, path)  # another call reading the same line never sees half a file
        return audio
